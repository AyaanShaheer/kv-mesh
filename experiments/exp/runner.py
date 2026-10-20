"""Experiment runner for the KV-Mesh manuscript.

A *run* is one (configuration, workload, policy, repetition) cell. Every run
writes

* ``requests.jsonl`` -- one raw record per request (the primary artifact), and
* ``summary.json``   -- the aggregate metrics for the cell.

Runs are fully determined by their configuration and seed, so the whole
manuscript can be regenerated from the published YAML files.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import sim
from .workload import WorkloadSpec, generate_arrivals, generate_churn


@dataclass
class RunConfig:
    """Everything needed to reproduce one run."""

    n_workers: int = 4
    capacity: int = 8
    host_capacity: int = 64
    heartbeat_interval: float = 3.0
    ttl: float = 10.0
    policy: str = sim.AFFINITY
    service_time: float = 0.05
    target_utilization: Optional[float] = None
    heartbeat_delay: float = 0.0
    heartbeat_jitter: float = 0.0
    heartbeat_drop: float = 0.0
    churn_interval: float = 0.0     # 0 disables churn
    churn_downtime: float = 0.0
    workload: WorkloadSpec = field(default_factory=WorkloadSpec)
    seed: int = 0
    label: str = ""

    def arrival_rate(self) -> float:
        """Effective request arrival rate in requests per second.

        When ``target_utilization`` is set, the arrival rate is derived from
        it so that every worker count is compared at the *same* offered load
        per worker: ``rate = rho * n_workers / service_time``. This keeps the
        comparison between one, two, four and eight workers attributable to
        the routing policy rather than to a difference in load.
        """
        if self.target_utilization is not None:
            return self.target_utilization * self.n_workers / self.service_time
        return self.workload.rate

    def key(self) -> str:
        """A stable identifier for this configuration."""
        payload = json.dumps(
            {k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v) for k, v in asdict(self).items()},
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
        return f"{self.label or 'run'}-{digest}"


@dataclass
class RunResult:
    config: RunConfig
    summary: Dict[str, float]
    duration_s: float
    request_records: List[Dict]


def run_simulation(config: RunConfig) -> RunResult:
    """Execute one simulated run and return its raw and aggregate results."""
    effective_rate = config.arrival_rate()
    workload = WorkloadSpec(
        kind=config.workload.kind,
        n_requests=config.workload.n_requests,
        rate=effective_rate,
        n_keys=config.workload.n_keys,
        zipf_s=config.workload.zipf_s,
        seed=config.workload.seed,
    )
    arrivals = generate_arrivals(workload)

    rng = random.Random(config.seed * 7919 + 13)
    simulator = sim.Simulator(
        n_workers=config.n_workers,
        capacity=config.capacity,
        heartbeat_interval=config.heartbeat_interval,
        ttl=config.ttl,
        policy=config.policy,
        service_time=config.service_time,
        host_capacity=config.host_capacity,
        heartbeat_delay=config.heartbeat_delay,
        heartbeat_jitter=config.heartbeat_jitter,
        heartbeat_drop=config.heartbeat_drop,
        rng=rng,
    )

    # Churn events are interleaved with the request stream.
    churn = []
    if config.churn_interval > 0:
        duration = arrivals[-1][0] if arrivals else 0.0
        churn = generate_churn(
            config.n_workers,
            duration=duration,
            interval=config.churn_interval,
            seed=config.seed + 999,
            downtime=config.churn_downtime,
        )

    start = time.perf_counter()
    churn_events = None
    if config.churn_interval > 0:
        duration = arrivals[-1][0] if arrivals else 0.0
        churn = generate_churn(
            config.n_workers,
            duration=duration,
            interval=config.churn_interval,
            seed=config.seed + 999,
            downtime=config.churn_downtime,
        )
        churn_events = [(c.time, c.action, c.worker_id) for c in churn]
    decisions = simulator.run(arrivals, churn_events=churn_events)
    duration = time.perf_counter() - start

    summary = simulator.summary()
    summary["wall_clock_s"] = duration
    summary["requests_per_second_simulated"] = (
        len(arrivals) / duration if duration > 0 else float("nan")
    )

    records = [
        {
            "request_id": d.request_id,
            "t": round(d.time, 6),
            "prompt_key": d.prompt_key,
            "prompt_hash": d.fingerprint,
            "worker": d.selected_worker,
            "advertised_hit": d.advertised_hit,
            "true_hit": d.true_hit,
            "cache_status": d.cache_status,
            "active_workers": d.active_workers,
            "match_count": d.match_count,
            "policy": d.policy,
            "decision_us": round(d.decision_us, 4),
            "queueing_delay_ms": round(d.queueing_delay_ms, 4),
            "service_ms": round(d.service_ms, 4),
            "failed": d.failed,
            "error": d.error,
        }
        for d in decisions
    ]

    return RunResult(config=config, summary=summary, duration_s=duration, request_records=records)


def _run_with_churn(simulator: sim.Simulator, arrivals, churn):
    """Deprecated helper kept for backwards compatibility."""
    raise NotImplementedError("churn is handled inline by Simulator.run")


def write_run(result: RunResult, out_dir: str, keep_records: bool = False) -> str:
    """Persist a run's summary and (optionally) its raw request records.

    Returns the run directory.
    """
    run_dir = os.path.join(out_dir, result.config.key())
    os.makedirs(run_dir, exist_ok=True)

    if keep_records:
        with open(os.path.join(run_dir, "requests.jsonl"), "w") as fh:
            for rec in result.request_records:
                fh.write(json.dumps(rec) + "\n")

    summary = dict(result.summary)
    summary["config"] = {
        k: (asdict(v) if hasattr(v, "__dataclass_fields__") else v)
        for k, v in asdict(result.config).items()
    }
    summary["run_key"] = result.config.key()
    with open(os.path.join(run_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    return run_dir


# ---------------------------------------------------------------------------
# Environment capture
# ---------------------------------------------------------------------------


def environment_info() -> Dict:
    """Capture the environment needed for the reproducibility appendix."""
    info = {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "numpy": _module_version("numpy"),
        "grpc": _module_version("grpc"),
        "pybind11": _module_version("pybind11"),
    }
    try:
        out = subprocess.run(
            ["go", "version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        if out:
            info["go_version"] = out
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        if out:
            info["git_commit"] = out
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    info["mem_total_kb"] = int(line.split()[1])
                    break
    except Exception:
        pass
    return info


def _module_version(name: str) -> Optional[str]:
    try:
        mod = __import__(name)
        return getattr(mod, "__version__", "unknown")
    except Exception:
        return None
