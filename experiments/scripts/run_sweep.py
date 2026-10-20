"""Run a factorial experiment sweep and archive the results.

Usage::

    python experiments/scripts/run_sweep.py --config experiments/configs/main.yaml \
        --out experiments/results/main

Every run writes a ``summary.json`` containing the run configuration and its
aggregate metrics. Runs selected by the configuration's ``keep_records`` rule
additionally write a gzipped ``requests.jsonl`` with one raw record per
request, which is the artifact required by the manuscript's reproducibility
checklist.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402

from exp.runner import RunConfig, environment_info, run_simulation, write_run  # noqa: E402
from exp.workload import WorkloadSpec  # noqa: E402


def _matches_rule(cfg: RunConfig, rule: Dict) -> bool:
    """Check whether a run configuration matches a ``keep_records`` rule."""
    for key, value in rule.items():
        if key == "workload_kind":
            actual = cfg.workload.kind
        elif key == "workload_name":
            actual = cfg.label.split("-")[0]
        else:
            actual = getattr(cfg, key, None)
        if actual != value:
            return False
    return True


def build_runs(spec: Dict) -> List[RunConfig]:
    """Expand a sweep specification into a list of run configurations."""
    factors = spec["factors"]
    policies = spec["policies"]
    workloads = spec["workloads"]
    reps = int(spec.get("repetitions", 5))
    n_requests = int(spec.get("n_requests", 10000))
    base = spec.get("base", {})

    keep_records = spec.get("keep_records", [])

    runs: List[RunConfig] = []
    factor_items = list(factors.items())

    # Cartesian product over the experimental factors.
    def expand(idx: int, current: Dict):
        if idx == len(factor_items):
            yield dict(current)
            return
        name, values = factor_items[idx]
        for v in values:
            current[name] = v
            yield from expand(idx + 1, current)

    for combo in expand(0, {}):
        for policy in policies:
            for wl_name, wl_spec in workloads.items():
                for rep in range(reps):
                    cfg = RunConfig(
                        n_workers=combo.get("n_workers", 4),
                        capacity=combo.get("capacity", 8),
                        host_capacity=combo.get("host_capacity", base.get("host_capacity", 64)),
                        heartbeat_interval=combo.get("heartbeat_interval", 3.0),
                        ttl=combo.get("ttl", base.get("ttl", 10.0)),
                        policy=policy,
                        service_time=base.get("service_time", 0.05),
                        target_utilization=wl_spec.get("target_utilization"),
                        heartbeat_delay=combo.get("heartbeat_delay", 0.0),
                        heartbeat_jitter=combo.get("heartbeat_jitter", 0.0),
                        heartbeat_drop=combo.get("heartbeat_drop", 0.0),
                        churn_interval=combo.get("churn_interval", 0.0),
                        churn_downtime=combo.get("churn_downtime", 0.0),
                        workload=WorkloadSpec(
                            kind=wl_spec.get("kind", wl_name),
                            n_requests=wl_spec.get("n_requests", n_requests),
                            rate=wl_spec.get("rate", 200.0),
                            n_keys=wl_spec.get("n_keys", 64),
                            zipf_s=wl_spec.get("zipf_s", 1.1),
                            # Each repetition replays a *different* trace. If
                            # the workload seed were constant across
                            # repetitions the replicates would be identical
                            # and the confidence intervals would be degenerate.
                            seed=wl_spec.get("seed", 0) + rep,
                        ),
                        seed=rep,
                        label=f"{wl_name}-{policy}-rep{rep}",
                    )
                    # Decide whether to keep the raw per-request records.
                    for rule in keep_records:
                        if _matches_rule(cfg, rule):
                            cfg.label = cfg.label + "-kept"
                            break
                    runs.append(cfg)
    return runs


def _worker(args):
    cfg, out_dir = args
    t0 = time.perf_counter()
    result = run_simulation(cfg)
    keep = cfg.label.endswith("-kept")
    run_dir = write_run(result, out_dir, keep_records=False)

    if keep:
        # The raw event stream is written directly in compressed form; the
        # uncompressed intermediate is never created, because deleting files on
        # the shared working tree is not reliably supported on every
        # filesystem the repository is used from.
        path = os.path.join(run_dir, "requests.jsonl.gz")
        with gzip.open(path, "wt") as fh:
            for rec in result.request_records:
                fh.write(json.dumps(rec) + "\n")

    return {
        "run_key": cfg.key(),
        "run_dir": os.path.relpath(run_dir, out_dir),
        "wall_clock_s": time.perf_counter() - t0,
        **{k: v for k, v in result.summary.items() if not isinstance(v, dict)},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--limit", type=int, default=0, help="run at most N runs (debugging)")
    ap.add_argument(
        "--clean",
        action="store_true",
        help="remove the output directory first (stale runs from an earlier "
        "configuration would otherwise be mixed into the aggregation)",
    )
    args = ap.parse_args()

    with open(args.config) as fh:
        spec = yaml.safe_load(fh)

    runs = build_runs(spec)
    if args.limit:
        runs = runs[: args.limit]

    if args.clean and os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out, exist_ok=True)
    print(f"[sweep] {len(runs)} runs -> {args.out}")

    t0 = time.time()
    if args.workers > 1 and len(runs) > 1:
        import multiprocessing as mp

        ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
        with ctx.Pool(args.workers) as pool:
            index = []
            for i, row in enumerate(pool.imap_unordered(_worker, [(c, args.out) for c in runs], chunksize=1)):
                index.append(row)
                if (i + 1) % 50 == 0 or (i + 1) == len(runs):
                    print(f"[sweep] {i + 1}/{len(runs)} runs complete ({time.time() - t0:.1f}s)")
    else:
        index = [_worker((c, args.out)) for c in runs]

    index.sort(key=lambda r: r["run_key"])
    with open(os.path.join(args.out, "index.json"), "w") as fh:
        json.dump(index, fh, indent=2)

    with open(os.path.join(args.out, "index.csv"), "w", newline="") as fh:
        if index:
            writer = csv.DictWriter(fh, fieldnames=list(index[0].keys()))
            writer.writeheader()
            writer.writerows(index)

    with open(os.path.join(args.out, "environment.json"), "w") as fh:
        json.dump(environment_info(), fh, indent=2)

    with open(os.path.join(args.out, "sweep_config.yaml"), "w") as fh:
        yaml.safe_dump(spec, fh, sort_keys=False)

    print(f"[sweep] finished in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
