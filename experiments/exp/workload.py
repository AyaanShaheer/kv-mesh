"""Seeded workload generation and bootstrap statistics.

All randomness is driven by explicitly recorded seeds so that every result in
the manuscript can be reproduced from the published configuration files.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------


@dataclass
class WorkloadSpec:
    """A synthetic request trace.

    Attributes:
        kind: ``exact_repeat``, ``zipf`` or ``uniform``.
        n_requests: number of requests in the trace.
        rate: mean arrival rate in requests per second (Poisson arrivals).
        n_keys: size of the prompt key space.
        zipf_s: skew parameter for the ``zipf`` kind.
        seed: seed of the trace generator.
    """

    kind: str = "exact_repeat"
    n_requests: int = 10000
    rate: float = 200.0
    n_keys: int = 64
    zipf_s: float = 1.1
    seed: int = 0

    def to_dict(self) -> Dict:
        return asdict(self)


def generate_arrivals(spec: WorkloadSpec) -> List[Tuple[float, str]]:
    """Generate a request trace as a list of ``(arrival_time, prompt_key)``.

    Arrivals follow a Poisson process with the configured rate. Prompt keys are
    drawn according to the workload kind:

    ``exact_repeat``
        Every key in the key space is used the same number of times. This
        isolates the effect of *exact* key locality with a controlled reuse
        distance.
    ``zipf``
        Keys are drawn from a Zipf distribution, approximating a hot set of
        recurring system prompts or shared context identifiers.
    ``uniform``
        Keys are drawn uniformly from a large key space, approximating a
        workload with little reuse.
    """
    rng = np.random.default_rng(spec.seed)

    if spec.kind == "exact_repeat":
        keys = np.arange(spec.n_keys, dtype=np.int64)
        reps = int(np.ceil(spec.n_requests / max(1, spec.n_keys)))
        pool = np.tile(keys, reps)[: spec.n_requests]
        rng.shuffle(pool)
    elif spec.kind == "zipf":
        pool = rng.zipf(spec.zipf_s, size=spec.n_requests)
        pool = np.clip(pool, 1, spec.n_keys) - 1
    elif spec.kind == "uniform":
        pool = rng.integers(0, spec.n_keys, size=spec.n_requests)
    else:
        raise ValueError(f"unknown workload kind: {spec.kind}")

    interarrival = rng.exponential(1.0 / spec.rate, size=spec.n_requests)
    times = np.cumsum(interarrival)

    return [(float(t), f"k{int(k):05d}") for t, k in zip(times, pool)]


# ---------------------------------------------------------------------------
# Worker churn
# ---------------------------------------------------------------------------


@dataclass
class ChurnEvent:
    time: float
    action: str  # "stop" | "restart"
    worker_id: str


def generate_churn(
    n_workers: int,
    duration: float,
    interval: float,
    seed: int,
    downtime: float = 0.0,
) -> List[ChurnEvent]:
    """Generate worker stop/restart events.

    ``interval`` is the mean time between churn events and ``downtime`` is the
    time a worker stays down before it is restarted. A ``downtime`` of zero
    means the worker is restarted immediately, which models a fast crash-restart
    cycle.
    """
    rng = np.random.default_rng(seed)
    events: List[ChurnEvent] = []
    t = float(rng.uniform(interval * 0.5, interval * 1.5))
    while t < duration:
        wid = f"w{int(rng.integers(0, n_workers)):03d}"
        events.append(ChurnEvent(time=t, action="stop", worker_id=wid))
        if downtime > 0:
            events.append(ChurnEvent(time=t + downtime, action="restart", worker_id=wid))
        else:
            events.append(ChurnEvent(time=t, action="restart", worker_id=wid))
        t += float(rng.exponential(interval))
    events.sort(key=lambda e: e.time)
    return events


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def bootstrap_ci(
    values: Sequence[float],
    statistic=np.mean,
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Tuple[float, float, float]:
    """Return ``(point_estimate, low, high)`` for a bootstrap confidence interval.

    The interval is computed by resampling the sample with replacement
    ``n_boot`` times. The seed is recorded so that the interval is reproducible.
    """
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    point = float(statistic(arr))
    if arr.size == 1:
        return (point, point, point)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_boot, arr.size))
    stats = statistic(arr[idx], axis=1)
    low = float(np.quantile(stats, alpha / 2.0))
    high = float(np.quantile(stats, 1.0 - alpha / 2.0))
    return (point, low, high)


def paired_bootstrap_diff(
    a: Sequence[float],
    b: Sequence[float],
    n_boot: int = 10000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Dict[str, float]:
    """Bootstrap the paired difference ``mean(a) - mean(b)``.

    The samples are paired element-wise (same repetition index), which is the
    appropriate comparison for repeated runs of the same configuration under
    different policies.
    """
    arr_a = np.asarray(list(a), dtype=float)
    arr_b = np.asarray(list(b), dtype=float)
    if arr_a.size == 0 or arr_a.size != arr_b.size:
        return {"diff": float("nan"), "low": float("nan"), "high": float("nan"), "p_gt_0": float("nan")}
    point, low, high = bootstrap_ci(arr_a - arr_b, n_boot=n_boot, alpha=alpha, seed=seed)
    rng = np.random.default_rng(seed + 1)
    diffs = arr_a - arr_b
    idx = rng.integers(0, diffs.size, size=(n_boot, diffs.size))
    boot = diffs[idx].mean(axis=1)
    return {
        "diff": point,
        "low": low,
        "high": high,
        "p_gt_0": float((boot > 0).mean()),
    }


def cohens_d(a: Sequence[float], b: Sequence[float]) -> float:
    """Cohen's d for two samples (pooled standard deviation).

    Returns NaN when the pooled standard deviation is zero, which happens when
    every replicate produces the same value. In that case the effect is
    perfectly separated but its standardised size is undefined; the paired
    bootstrap interval carries the usable information.
    """
    arr_a = np.asarray(list(a), dtype=float)
    arr_b = np.asarray(list(b), dtype=float)
    if arr_a.size < 2 or arr_b.size < 2:
        return float("nan")
    na, nb = arr_a.size, arr_b.size
    pooled = math.sqrt(
        ((na - 1) * arr_a.var(ddof=1) + (nb - 1) * arr_b.var(ddof=1)) / (na + nb - 2)
    )
    if pooled == 0:
        return float("nan")
    # Floating-point noise can leave a pooled standard deviation that is
    # non-zero but many orders of magnitude below the effect size. Treat that
    # as an undefined standardised effect rather than reporting a number that
    # is an artefact of rounding.
    scale = max(
        float(np.abs(arr_a).max()),
        float(np.abs(arr_b).max()),
        1e-30,
    )
    if pooled < 1e-9 * scale:
        return float("nan")
    return float((arr_a.mean() - arr_b.mean()) / pooled)


def percentile(values: Sequence[float], p: float) -> float:
    if not len(values):
        return float("nan")
    return float(np.quantile(np.asarray(list(values), dtype=float), p))


def fmt_ci(point: float, low: float, high: float, digits: int = 3) -> str:
    """Format a point estimate with its bootstrap interval."""
    if any(math.isnan(x) for x in (point, low, high)):
        return "n/a"
    return f"{point:.{digits}f} [{low:.{digits}f}, {high:.{digits}f}]"
