"""Aggregation and statistical analysis of experiment results.

Every run writes a ``summary.json`` that contains both its configuration and
its aggregate metrics. This module loads those files, groups runs into
experimental cells and computes means with bootstrap confidence intervals,
plus paired bootstrap differences between policies.
"""

from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .workload import bootstrap_ci, cohens_d, paired_bootstrap_diff

# Metric name in the run summary -> short name used in the manuscript tables.
METRIC_NAMES = {
    "advertised_hits": "advertised_hits",
    "true_hits": "true_hits",
    "vram_hits": "fast_tier_hits",
    "host_hits": "host_tier_hits",
    "stale_affinity": "stale_affinity",
    "degraded_affinity": "degraded_affinity",
    "routing_failures": "routing_failures",
    "worker_imbalance_cv": "worker_imbalance_cv",
    "bytes_offloaded": "bytes_offloaded",
    "host_evictions": "host_evictions",
    "mean_decision_us": "mean_decision_us",
    "queueing_delay_p95_ms": "queueing_delay_p95_ms",
    "mean_active_workers": "mean_active_workers",
    "requests_served": "requests_served",
    "requests_total": "requests_total",
    "heartbeats_dropped": "heartbeats_dropped",
    "heartbeats_rejected": "heartbeats_rejected",
}


def load_runs(result_dir: str) -> List[Dict]:
    """Load every ``summary.json`` under ``result_dir`` into flat records."""
    runs: List[Dict] = []
    for path in sorted(glob.glob(os.path.join(result_dir, "**", "summary.json"), recursive=True)):
        with open(path) as fh:
            payload = json.load(fh)
        cfg = payload.get("config", {})
        workload = cfg.get("workload", {})
        record = {
            "run_key": payload.get("run_key"),
            "run_dir": os.path.dirname(path),
            "policy": cfg.get("policy"),
            "n_workers": cfg.get("n_workers"),
            "capacity": cfg.get("capacity"),
            "host_capacity": cfg.get("host_capacity"),
            "heartbeat_interval": cfg.get("heartbeat_interval"),
            "ttl": cfg.get("ttl"),
            "heartbeat_delay": cfg.get("heartbeat_delay"),
            "heartbeat_jitter": cfg.get("heartbeat_jitter"),
            "heartbeat_drop": cfg.get("heartbeat_drop"),
            "churn_interval": cfg.get("churn_interval"),
            "churn_downtime": cfg.get("churn_downtime"),
            "service_time": cfg.get("service_time"),
            "target_utilization": cfg.get("target_utilization"),
            "workload_kind": workload.get("kind"),
            "workload_seed": workload.get("seed"),
            "workload_name": _workload_name(cfg.get("label", "")),
            "seed": cfg.get("seed"),
            "n_requests": workload.get("n_requests"),
        }
        for key, value in payload.items():
            if key in ("config",):
                continue
            record[key] = value
        runs.append(record)
    return runs


def _workload_name(label: str) -> str:
    """Recover the workload name from a run label (``<workload>-<policy>-...``)."""
    parts = label.split("-")
    for name in ("exact_repeat", "zipf", "uniform", "low_load", "medium_load",
                 "high_load", "saturated_load", "overloaded_load"):
        if label.startswith(name + "-"):
            return name
    return parts[0] if parts else "unknown"


def group_by(runs: Iterable[Dict], dims: Sequence[str]) -> Dict[Tuple, List[Dict]]:
    """Group runs by the given configuration dimensions."""
    groups: Dict[Tuple, List[Dict]] = defaultdict(list)
    for run in runs:
        key = tuple(run.get(d) for d in dims)
        groups[key].append(run)
    return dict(groups)


def cell_stats(runs: Sequence[Dict]) -> Dict[str, float]:
    """Compute the aggregate statistics of one experimental cell."""
    if not runs:
        return {}
    served = sum(r.get("requests_served", 0) for r in runs)
    total = sum(r.get("requests_total", 0) for r in runs)
    out: Dict[str, float] = {
        "repetitions": len(runs),
        "requests_total": total,
        "requests_served": served,
    }

    def ratio(numerator_key: str, denominator: float) -> float:
        num = sum(r.get(numerator_key, 0) for r in runs)
        return (num / denominator) if denominator else float("nan")

    out["advertised_hit_rate"] = ratio("advertised_hits", served)
    out["true_hit_rate"] = ratio("true_hits", served)
    out["fast_tier_hit_rate"] = ratio("vram_hits", served)
    out["host_tier_hit_rate"] = ratio("host_hits", served)
    out["failure_rate"] = ratio("routing_failures", total)
    advertised = sum(r.get("advertised_hits", 0) for r in runs)
    out["stale_affinity_rate"] = (
        sum(r.get("stale_affinity", 0) for r in runs) / advertised if advertised else 0.0
    )
    out["degraded_affinity_rate"] = (
        sum(r.get("degraded_affinity", 0) for r in runs) / advertised if advertised else 0.0
    )
    out["bytes_offloaded_total"] = sum(r.get("bytes_offloaded", 0) for r in runs)
    out["host_evictions_total"] = sum(r.get("host_evictions", 0) for r in runs)

    for key in ("worker_imbalance_cv", "mean_decision_us", "queueing_delay_p95_ms", "mean_active_workers"):
        values = [r.get(key, float("nan")) for r in runs if key in r]
        if values:
            out[key] = sum(values) / len(values)
    return out


# Mapping from a rate metric's base name to the run-summary field it is
# computed from.
_RATE_SOURCE = {
    "advertised_hit": "advertised_hits",
    "true_hit": "true_hits",
    "fast_tier_hit": "vram_hits",
    "host_tier_hit": "host_hits",
}

# Rates whose denominator is the number of advertised hits rather than the
# number of requests.
_ADVERTISED_DENOMINATOR = {"stale_affinity", "degraded_affinity"}


def _rate_value(run: Dict, base: str) -> Optional[float]:
    """Compute one rate for a single run, or None if it is not defined."""
    if base in _ADVERTISED_DENOMINATOR:
        denominator = run.get("advertised_hits", 0) or 0
        if not denominator:
            return None
        return (run.get(base, 0) or 0) / denominator
    served = run.get("requests_served", 0) or 0
    if not served:
        return None
    key = _RATE_SOURCE.get(base)
    if key is None:
        return None
    return (run.get(key, 0) or 0) / served


def bootstrap_cell(
    runs: Sequence[Dict],
    metric: str,
    n_boot: int = 10000,
    seed: int = 0,
) -> Dict[str, float]:
    """Bootstrap CI for a per-run metric across repetitions of one cell."""
    values = []
    for r in runs:
        if metric == "failure_rate":
            total = r.get("requests_total", 0) or 0
            values.append(r.get("routing_failures", 0) / total if total else float("nan"))
        elif metric.endswith("_rate"):
            base = metric[: -len("_rate")]
            value = _rate_value(r, base)
            if value is not None:
                values.append(value)
        else:
            if metric in r:
                values.append(r[metric])
    point, low, high = bootstrap_ci(values, n_boot=n_boot, seed=seed)
    return {"mean": point, "low": low, "high": high, "n": len(values)}


def compare_policies(
    runs: Sequence[Dict],
    dims: Sequence[str],
    metric: str,
    baseline: str = "roundrobin",
    n_boot: int = 10000,
    seed: int = 0,
) -> List[Dict]:
    """Paired bootstrap comparison of every policy against a baseline.

    Runs are paired by repetition index within each cell, which is the
    appropriate pairing for repeated measurements of the same configuration.
    """
    cells = group_by(runs, list(dims) + ["policy"])
    by_cell: Dict[Tuple, Dict[str, List[Dict]]] = defaultdict(dict)
    for key, group in cells.items():
        cell = key[:-1]
        policy = key[-1]
        by_cell[cell][policy] = sorted(group, key=lambda r: (r.get("seed", 0), r.get("run_key", "")))

    results: List[Dict] = []
    for cell, policies in sorted(by_cell.items(), key=lambda kv: str(kv[0])):
        if baseline not in policies:
            continue
        base_values = _metric_values(policies[baseline], metric)
        for policy, group in sorted(policies.items()):
            if policy == baseline:
                continue
            values = _metric_values(group, metric)
            n = min(len(values), len(base_values))
            if n == 0:
                continue
            diff = paired_bootstrap_diff(values[:n], base_values[:n], n_boot=n_boot, seed=seed)
            results.append(
                {
                    "cell": dict(zip(dims, cell)),
                    "policy": policy,
                    "baseline": baseline,
                    "metric": metric,
                    "n": n,
                    "policy_mean": sum(values[:n]) / n,
                    "baseline_mean": sum(base_values[:n]) / n,
                    "diff": diff["diff"],
                    "diff_low": diff["low"],
                    "diff_high": diff["high"],
                    "p_gt_0": diff["p_gt_0"],
                    "cohens_d": cohens_d(values[:n], base_values[:n]),
                }
            )
    return results


def _metric_values(runs: Sequence[Dict], metric: str) -> List[float]:
    values = []
    for r in runs:
        if metric == "failure_rate":
            total = r.get("requests_total", 0) or 0
            values.append(r.get("routing_failures", 0) / total if total else float("nan"))
        elif metric.endswith("_rate"):
            base = metric[: -len("_rate")]
            value = _rate_value(r, base)
            if value is not None:
                values.append(value)
        elif metric in r:
            values.append(r[metric])
    return values


def table(
    runs: Sequence[Dict],
    dims: Sequence[str],
    metrics: Sequence[str],
    labels: Optional[Dict[str, str]] = None,
    n_boot: int = 10000,
    seed: int = 0,
) -> List[Dict]:
    """Build a table: one row per cell, with bootstrap intervals per metric."""
    groups = group_by(runs, dims)
    rows: List[Dict] = []
    for key, group in sorted(groups.items(), key=lambda kv: str(kv[0])):
        row = dict(zip(dims, key))
        row["repetitions"] = len(group)
        for metric in metrics:
            stats = bootstrap_cell(group, metric, n_boot=n_boot, seed=seed)
            row[f"{metric}_mean"] = stats["mean"]
            row[f"{metric}_low"] = stats["low"]
            row[f"{metric}_high"] = stats["high"]
        rows.append(row)
    return rows


def _column_spec(columns: Sequence, default_percent: bool):
    """Normalise a column specification.

    A column is either ``(label, key)`` or ``(label, key, percent)``, where
    the third element overrides the table's default percent formatting.
    """
    out = []
    for col in columns:
        label, key = col[0], col[1]
        pct = col[2] if len(col) > 2 else default_percent
        out.append((label, key, pct))
    return out


def to_latex(
    rows: Sequence[Dict],
    columns: Sequence,
    caption: str = "",
    label: str = "",
    digits: int = 3,
    percent: bool = False,
    span: bool = False,
    font: str = "\\scriptsize",
    body_only: bool = False,
    headers: Optional[Sequence[str]] = None,
) -> str:
    """Render a table as LaTeX ``tabular`` source.

    A metric that carries a bootstrap interval is rendered as
    ``mean [low, high]``; the bounds are printed with one extra decimal place
    so that a narrow interval is still visible.

    ``span`` places the table in a ``table*`` float, which spans both columns
    of a two-column document. It is used for the tables that carry too many
    columns to fit in a single column.
    """
    specs = _column_spec(columns, percent)

    def fmt(value, d: int, pct: bool) -> str:
        if value is None:
            return "--"
        if isinstance(value, str):
            # Escape the characters that are not valid in LaTeX text mode.
            return value.replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")
        if isinstance(value, int) and not pct:
            return str(value)
        try:
            f = float(value)
        except (TypeError, ValueError):
            return str(value)
        if f != f:  # NaN
            return "--"
        if pct:
            f *= 100.0
        return f"{f:.{d}f}"

    float_name = "table*" if span else "table"
    lines = []
    if body_only:
        # Only the rows, so that the manuscript can supply its own caption and
        # column headers.
        for row in rows:
            cells = []
            for _, key, pct in specs:
                mean = row.get(f"{key}_mean")
                if mean is None:
                    if key not in row:
                        cells.append("--")
                        continue
                    cells.append(fmt(row[key], digits, pct))
                    continue
                low = row.get(f"{key}_low")
                high = row.get(f"{key}_high")
                if low is None or high is None:
                    cells.append(fmt(mean, digits, pct))
                else:
                    cells.append(
                        f"{fmt(mean, digits, pct)} \\tiny "
                        f"[{fmt(low, digits + 1, pct)}, {fmt(high, digits + 1, pct)}]"
                    )
            lines.append(" & ".join(cells) + " \\\\")
        return "\n".join(lines)
    lines.append(f"\\begin{{{float_name}}}[t]")
    lines.append("\\centering")
    if font:
        lines.append(font)
    lines.append("\\setlength{\\tabcolsep}{2.5pt}")
    if caption:
        lines.append(f"\\caption{{{caption}}}")
    if label:
        lines.append(f"\\label{{{label}}}")
    lines.append("\\begin{tabular}{" + "l" * 1 + "c" * len(specs) + "}")
    lines.append("\\toprule")
    if headers is not None and len(headers) == len(specs):
        header = " & ".join(headers)
    else:
        header = " & ".join([c[0] for c in specs])
    lines.append(header + " \\\\")
    lines.append("\\midrule")
    for row in rows:
        cells = []
        for _, key, pct in specs:
            mean = row.get(f"{key}_mean")
            if mean is None:
                if key not in row:
                    cells.append("--")
                    continue
                cells.append(fmt(row[key], digits, pct))
                continue
            low = row.get(f"{key}_low")
            high = row.get(f"{key}_high")
            if low is None or high is None:
                cells.append(fmt(mean, digits, pct))
            else:
                cells.append(
                    f"{fmt(mean, digits, pct)} \\tiny "
                    f"[{fmt(low, digits + 1, pct)}, {fmt(high, digits + 1, pct)}]"
                )
        lines.append(" & ".join(cells) + " \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    lines.append(f"\\end{{{float_name}}}")
    return "\n".join(lines)


def to_markdown(
    rows: Sequence[Dict],
    columns: Sequence,
    digits: int = 3,
    percent: bool = False,
) -> str:
    """Render a table as GitHub-flavoured Markdown."""
    specs = _column_spec(columns, percent)

    def fmt(value, d: int, pct: bool) -> str:
        if value is None:
            return "--"
        if isinstance(value, str):
            # Escape the characters that are not valid in LaTeX text mode.
            return value.replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")
        if isinstance(value, int) and not pct:
            return str(value)
        try:
            f = float(value)
        except (TypeError, ValueError):
            return str(value)
        if f != f:
            return "--"
        if pct:
            f *= 100.0
        return f"{f:.{d}f}"

    out = ["| " + " | ".join(c[0] for c in specs) + " |"]
    out.append("|" + "|".join(["---"] * len(specs)) + "|")
    for row in rows:
        cells = []
        for _, key, pct in specs:
            mean = row.get(f"{key}_mean")
            if mean is None:
                if key not in row:
                    cells.append("--")
                    continue
                cells.append(fmt(row[key], digits, pct))
                continue
            low = row.get(f"{key}_low")
            high = row.get(f"{key}_high")
            if low is None or high is None:
                cells.append(fmt(mean, digits, pct))
            else:
                cells.append(
                    f"{fmt(mean, digits, pct)} [{fmt(low, digits + 1, pct)}, {fmt(high, digits + 1, pct)}]"
                )
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)
