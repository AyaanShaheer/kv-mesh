"""Run the live-system arm of the evaluation.

Starts the real Go gateway and the real Python workers, replays a seeded
request trace through them, and computes the routing metrics from the
gateway's own decision log correlated with the workers' ground-truth logs.

Usage::

    python experiments/scripts/run_live.py --out experiments/results/live
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from exp.live import LiveCluster, LiveConfig, drive_requests  # noqa: E402
from exp.workload import generate_arrivals  # noqa: E402

# The prompt string must be byte-identical to the one the simulator hashes, so
# that the live and simulated runs address the same cache keys.
PROMPT_TEMPLATE = "SYSTEM CONTEXT v1 :: session={key} :: request payload follows <<{key}>>"


def build_keys(kind: str, n_requests: int, n_keys: int, zipf_s: float, seed: int) -> List[str]:
    rng = np.random.default_rng(seed)
    if kind == "exact_repeat":
        keys = np.arange(n_keys, dtype=np.int64)
        reps = int(np.ceil(n_requests / max(1, n_keys)))
        pool = np.tile(keys, reps)[:n_requests]
        rng.shuffle(pool)
    elif kind == "zipf":
        pool = np.clip(rng.zipf(zipf_s, size=n_requests), 1, n_keys) - 1
    else:
        pool = rng.integers(0, n_keys, size=n_requests)
    return [f"k{int(k):05d}" for k in pool]


def analyze(
    gateway_decisions: List[Dict],
    worker_decisions: List[Dict],
) -> Dict:
    """Correlate the gateway's decisions with the workers' ground truth."""
    by_request = {d.get("request_id"): d for d in worker_decisions if d.get("request_id")}

    total = len(gateway_decisions)
    errors = [d for d in gateway_decisions if d.get("error")]
    advertised_hits = sum(1 for d in gateway_decisions if d.get("advertised_hit"))

    true_hits = 0
    stale = 0
    degraded = 0
    matched = 0
    fast_tier = 0
    host_tier = 0
    misses = 0

    for gd in gateway_decisions:
        wd = by_request.get(gd.get("request_id"))
        if wd is None:
            continue
        matched += 1
        status = wd.get("cache_status")
        if status == "vram_hit":
            fast_tier += 1
            true_hits += 1
        elif status == "host_hit":
            host_tier += 1
            true_hits += 1
        elif status == "miss":
            misses += 1
        if gd.get("advertised_hit"):
            if status == "miss":
                stale += 1
            elif status == "host_hit":
                degraded += 1

    latencies = sorted(d.get("routing_latency_us", 0.0) for d in gateway_decisions if not d.get("error"))

    def q(p: float) -> float:
        if not latencies:
            return float("nan")
        idx = min(int(p * len(latencies)), len(latencies) - 1)
        return latencies[idx]

    return {
        "requests": total,
        "errors": len(errors),
        "matched_records": matched,
        "advertised_hits": advertised_hits,
        "advertised_hit_rate": advertised_hits / total if total else 0.0,
        "true_hits": true_hits,
        "true_hit_rate": true_hits / matched if matched else 0.0,
        "fast_tier_hits": fast_tier,
        "host_tier_hits": host_tier,
        "misses": misses,
        "stale_affinity": stale,
        "stale_affinity_rate": stale / advertised_hits if advertised_hits else 0.0,
        "degraded_affinity": degraded,
        "degraded_affinity_rate": degraded / advertised_hits if advertised_hits else 0.0,
        "routing_latency_us_p50": q(0.50),
        "routing_latency_us_p95": q(0.95),
        "routing_latency_us_p99": q(0.99),
        "routing_latency_us_max": latencies[-1] if latencies else float("nan"),
    }


def run_one(config: LiveConfig) -> Dict:
    cluster = LiveCluster(config)
    try:
        cluster.start_gateway()
        cluster.start_workers()
        # Give the workers time to register with the gateway before the first
        # request, so that the measurement is not dominated by the startup
        # transient.
        time.sleep(max(1.5, config.heartbeat_interval * 2))

        keys = build_keys(
            config.workload_kind, config.n_requests, config.n_keys, config.zipf_s, config.seed
        )

        def prompt_for_key(i: int) -> str:
            return PROMPT_TEMPLATE.format(key=keys[i])

        client = drive_requests(
            cluster.gateway_port,
            n_requests=config.n_requests,
            rate=config.request_rate,
            prompt_for_key=prompt_for_key,
        )

        time.sleep(config.heartbeat_interval + 0.5)
        metrics = cluster.metrics()
        gw = cluster.gateway_decisions()
        wk = cluster.worker_decisions()

        result = {
            "config": {k: v for k, v in asdict(config).items() if k != "out_dir"},
            "client": client,
            "gateway_metrics": metrics,
            "analysis": analyze(gw, wk),
        }
        return result
    finally:
        cluster.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="experiments/results/live")
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--rate", type=float, default=200.0)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    results = []

    policies = ["affinity", "roundrobin", "leastloaded"]
    worker_counts = [1, 2, 4]
    heartbeat_intervals = [1.0, 3.0]
    reps = int(os.environ.get("KVMESH_LIVE_REPS", "3"))

    for policy in policies:
        for n_workers in worker_counts:
            for hb in heartbeat_intervals:
                for rep in range(reps):
                    cfg = LiveConfig(
                        n_workers=n_workers,
                        policy=policy,
                        ttl=10.0,
                        heartbeat_interval=hb,
                        capacity=8,
                        host_capacity=64,
                        token_delay=0.002,
                        n_requests=args.requests,
                        request_rate=args.rate,
                        workload_kind="zipf",
                        n_keys=256,
                        zipf_s=1.1,
                        seed=rep,
                        out_dir=os.path.join(args.out, "raw"),
                    )
                    t0 = time.time()
                    result = run_one(cfg)
                    result["wall_clock_s"] = time.time() - t0
                    results.append(result)
                    print(
                        f"[live] policy={policy} workers={n_workers} hb={hb} rep={rep} "
                        f"adv={result['analysis']['advertised_hit_rate']:.3f} "
                        f"true={result['analysis']['true_hit_rate']:.3f} "
                        f"p95={result['analysis']['routing_latency_us_p95']:.0f}us "
                        f"({time.time() - t0:.1f}s)",
                        flush=True,
                    )

    with open(os.path.join(args.out, "live_results.json"), "w") as fh:
        json.dump(results, fh, indent=2)

    # The raw per-request event streams are archived compressed.
    raw_dir = os.path.join(args.out, "raw")
    if os.path.isdir(raw_dir):
        import gzip

        for name in os.listdir(raw_dir):
            if name.endswith(".jsonl"):
                path = os.path.join(raw_dir, name)
                with open(path, "rb") as src, gzip.open(path + ".gz", "wb", compresslevel=9) as dst:
                    dst.write(src.read())
                os.remove(path)
    print(f"[live] wrote {len(results)} live runs to {args.out}/live_results.json")


if __name__ == "__main__":
    main()
