"""Turn the archived experiment results into manuscript tables and figures.

Usage::

    python experiments/scripts/make_report.py

Reads ``experiments/results/{main,staleness,churn,capacity,saturation,live}``
and writes

* ``paper/tables/*.tex``   -- LaTeX fragments included by the manuscript,
* ``paper/figures/*.pdf``  -- vector figures for the manuscript,
* ``experiments/results/tables/*.md`` -- Markdown mirrors of every table,
* ``experiments/results/aggregate.csv`` -- every aggregated cell.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from exp.analysis import (  # noqa: E402
    bootstrap_cell,
    cell_stats,
    compare_policies,
    group_by,
    load_runs,
    table,
    to_latex,
    to_markdown,
)

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESULTS = os.path.join(REPO, "experiments", "results")
PAPER = os.path.join(REPO, "paper")
TABLES_OUT = os.path.join(PAPER, "tables")
FIGURES_OUT = os.path.join(PAPER, "figures")
MD_OUT = os.path.join(RESULTS, "tables")

POLICY_LABEL = {
    "affinity": "KV-Mesh (affinity)",
    "roundrobin": "Round-robin",
    "leastloaded": "Least-loaded",
    "oracle": "Oracle (upper bound)",
}
POLICY_SHORT = {
    "affinity": "affinity",
    "roundrobin": "round-robin",
    "leastloaded": "least-loaded",
    "oracle": "oracle",
}
WORKLOAD_LABEL = {
    "exact_repeat": "Exact repeats",
    "zipf": "Hot set (Zipf)",
    "uniform": "Uniform (low reuse)",
}
POLICY_ORDER = ["affinity", "roundrobin", "leastloaded", "oracle"]

plt.rcParams.update(
    {
        "font.size": 9,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "figure.dpi": 150,
        "savefig.bbox": "tight",
        "legend.frameon": False,
    }
)


def ensure_dirs():
    for d in (TABLES_OUT, FIGURES_OUT, MD_OUT):
        os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_all() -> Dict[str, List[Dict]]:
    runs = {}
    for name in ("main", "staleness", "churn", "capacity", "saturation"):
        path = os.path.join(RESULTS, name)
        if os.path.isdir(path):
            runs[name] = load_runs(path)
        else:
            runs[name] = []
    return runs


def filter_runs(runs: Sequence[Dict], **conditions) -> List[Dict]:
    out = []
    for r in runs:
        ok = True
        for key, value in conditions.items():
            actual = r.get(key)
            if isinstance(value, (list, tuple, set)):
                if actual not in value:
                    ok = False
                    break
            elif actual != value:
                ok = False
                break
        if ok:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Table builders
# ---------------------------------------------------------------------------


def table_policy_comparison(main_runs: List[Dict]) -> List[Dict]:
    """Main table: policy x workload at the reference configuration."""
    rows = []
    ref = filter_runs(
        main_runs,
        n_workers=4,
        capacity=8,
        heartbeat_interval=3.0,
        ttl=10.0,
        host_capacity=64,
    )
    for workload in ("exact_repeat", "zipf", "uniform"):
        sub = [r for r in ref if r.get("workload_kind") == workload]
        if not sub:
            continue
        for policy in POLICY_ORDER:
            group = [r for r in sub if r.get("policy") == policy]
            if not group:
                continue
            stats = cell_stats(group)
            row = {
                "workload": WORKLOAD_LABEL.get(workload, workload),
                "policy": POLICY_SHORT.get(policy, policy),
                "repetitions": stats["repetitions"],
            }
            for metric in (
                "advertised_hit_rate",
                "true_hit_rate",
                "fast_tier_hit_rate",
                "host_tier_hit_rate",
                "stale_affinity_rate",
                "degraded_affinity_rate",
                "worker_imbalance_cv",
                "failure_rate",
            ):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            rows.append(row)
    return rows


def table_worker_scaling(main_runs: List[Dict]) -> List[Dict]:
    """Worker-count scaling at the reference capacity and heartbeat."""
    rows = []
    sub = filter_runs(
        main_runs, capacity=8, heartbeat_interval=3.0, ttl=10.0, host_capacity=64, workload_kind="zipf"
    )
    for n_workers in (1, 2, 4, 8):
        for policy in POLICY_ORDER:
            group = [r for r in sub if r.get("n_workers") == n_workers and r.get("policy") == policy]
            if not group:
                continue
            row = {"workers": n_workers, "policy": POLICY_SHORT.get(policy, policy)}
            for metric in (
                "advertised_hit_rate",
                "true_hit_rate",
                "fast_tier_hit_rate",
                "worker_imbalance_cv",
                "queueing_delay_p95_ms",
            ):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            rows.append(row)
    return rows


def table_capacity(cap_runs: List[Dict]) -> List[Dict]:
    """Fast-tier capacity sweep at a fixed host-tier capacity."""
    rows = []
    sub = filter_runs(cap_runs, host_capacity=64, workload_kind="zipf")
    for capacity in (2, 4, 8, 16, 32, 64, 128):
        for policy in ("affinity", "roundrobin"):
            group = [r for r in sub if r.get("capacity") == capacity and r.get("policy") == policy]
            if not group:
                continue
            stats = cell_stats(group)
            row = {
                "capacity": capacity,
                "policy": POLICY_SHORT.get(policy, policy),
                "repetitions": stats["repetitions"],
            }
            for metric in ("true_hit_rate", "fast_tier_hit_rate", "host_tier_hit_rate"):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            row["bytes_offloaded_total_mean"] = stats["bytes_offloaded_total"] / max(1, stats["repetitions"])
            rows.append(row)
    return rows


def table_host_capacity(cap_runs: List[Dict]) -> List[Dict]:
    """Host-tier capacity sweep at a fixed fast-tier capacity."""
    rows = []
    sub = filter_runs(cap_runs, capacity=8, workload_kind="zipf")
    for host_capacity in (0, 8, 32, 128, 512):
        for policy in ("affinity", "roundrobin"):
            group = [
                r for r in sub if r.get("host_capacity") == host_capacity and r.get("policy") == policy
            ]
            if not group:
                continue
            row = {
                "host_capacity": "unbounded" if host_capacity == 0 else host_capacity,
                "policy": POLICY_SHORT.get(policy, policy),
            }
            for metric in ("true_hit_rate", "fast_tier_hit_rate", "host_tier_hit_rate"):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            rows.append(row)
    return rows


def table_staleness(stale_runs: List[Dict]) -> List[Dict]:
    """Effect of heartbeat loss and of the staleness window."""
    rows = []
    sub = filter_runs(stale_runs, heartbeat_interval=3.0, heartbeat_delay=0.0, heartbeat_jitter=0.0)
    for ttl in (5.0, 10.0, 20.0):
        for drop in (0.0, 0.1, 0.3):
            group = [
                r
                for r in sub
                if r.get("ttl") == ttl and r.get("heartbeat_drop") == drop and r.get("policy") == "affinity"
            ]
            if not group:
                continue
            row = {"ttl_s": ttl, "drop_rate": drop}
            for metric in ("advertised_hit_rate", "true_hit_rate", "stale_affinity_rate", "degraded_affinity_rate"):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            rows.append(row)
    return rows


def table_staleness_delay(stale_runs: List[Dict]) -> List[Dict]:
    """Effect of heartbeat delay and jitter at the default TTL."""
    rows = []
    sub = filter_runs(stale_runs, ttl=10.0, heartbeat_drop=0.0)
    for delay in (0.0, 1.0, 3.0):
        for jitter in (0.0, 1.0):
            group = [
                r
                for r in sub
                if r.get("heartbeat_delay") == delay
                and r.get("heartbeat_jitter") == jitter
                and r.get("policy") == "affinity"
            ]
            if not group:
                continue
            row = {"delay_s": delay, "jitter_s": jitter}
            for metric in ("advertised_hit_rate", "true_hit_rate", "degraded_affinity_rate"):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            rows.append(row)
    return rows


def table_churn(churn_runs: List[Dict]) -> List[Dict]:
    """Effect of worker churn."""
    rows = []
    sub = filter_runs(churn_runs, workload_kind="zipf")
    for n_workers in (4, 8):
        for churn_interval in (5.0, 15.0, 30.0):
            for downtime in (0.0, 5.0):
                for policy in POLICY_ORDER:
                    group = [
                        r
                        for r in sub
                        if r.get("n_workers") == n_workers
                        and r.get("churn_interval") == churn_interval
                        and r.get("churn_downtime") == downtime
                        and r.get("policy") == policy
                    ]
                    if not group:
                        continue
                    row = {
                        "workers": n_workers,
                        "churn_interval_s": churn_interval,
                        "downtime_s": downtime,
                        "policy": POLICY_SHORT.get(policy, policy),
                    }
                    for metric in ("advertised_hit_rate", "true_hit_rate", "failure_rate"):
                        ci = bootstrap_cell(group, metric)
                        row[f"{metric}_mean"] = ci["mean"]
                        row[f"{metric}_low"] = ci["low"]
                        row[f"{metric}_high"] = ci["high"]
                    rows.append(row)
    return rows


def table_saturation(sat_runs: List[Dict]) -> List[Dict]:
    """Effect of the offered load."""
    rows = []
    load_order = ["low_load", "medium_load", "high_load", "saturated_load", "overloaded_load"]
    sub = filter_runs(sat_runs)
    for workload in load_order:
        group_all = [r for r in sub if r.get("workload_name") == workload]
        if not group_all:
            continue
        rho = group_all[0].get("target_utilization")
        for policy in POLICY_ORDER:
            group = [r for r in group_all if r.get("policy") == policy]
            if not group:
                continue
            row = {
                "offered_load": f"rho={rho:.2f}" if rho is not None else workload,
                "policy": POLICY_SHORT.get(policy, policy),
            }
            for metric in ("true_hit_rate", "fast_tier_hit_rate", "queueing_delay_p95_ms", "worker_imbalance_cv"):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            rows.append(row)
    return rows


def table_heartbeat_interval(main_runs: List[Dict]) -> List[Dict]:
    """Effect of the heartbeat period on the freshness of the cache index."""
    rows = []
    sub = filter_runs(
        main_runs, n_workers=4, capacity=8, ttl=10.0, host_capacity=64, workload_kind="zipf"
    )
    for hb in (1.0, 3.0, 5.0):
        for policy in ("affinity", "roundrobin"):
            group = [
                r for r in sub if r.get("heartbeat_interval") == hb and r.get("policy") == policy
            ]
            if not group:
                continue
            row = {"heartbeat_s": hb, "policy": POLICY_SHORT.get(policy, policy)}
            for metric in (
                "advertised_hit_rate",
                "true_hit_rate",
                "fast_tier_hit_rate",
                "degraded_affinity_rate",
                "stale_affinity_rate",
            ):
                ci = bootstrap_cell(group, metric)
                row[f"{metric}_mean"] = ci["mean"]
                row[f"{metric}_low"] = ci["low"]
                row[f"{metric}_high"] = ci["high"]
            rows.append(row)
    return rows


def table_significance(main_runs: List[Dict]) -> List[Dict]:
    """Paired bootstrap comparison of each policy against round-robin."""
    rows = []
    sub = filter_runs(
        main_runs, n_workers=4, capacity=8, heartbeat_interval=3.0, ttl=10.0, host_capacity=64
    )
    for workload in ("exact_repeat", "zipf", "uniform"):
        subset = [r for r in sub if r.get("workload_kind") == workload]
        if not subset:
            continue
        for metric in ("true_hit_rate", "fast_tier_hit_rate"):
            comparisons = compare_policies(
                subset,
                dims=["workload_kind", "n_workers", "capacity", "heartbeat_interval"],
                metric=metric,
                baseline="roundrobin",
            )
            for cmp in comparisons:
                if cmp["policy"] not in ("affinity", "leastloaded", "oracle"):
                    continue
                rows.append(
                    {
                        "workload": WORKLOAD_LABEL.get(workload, workload),
                        "metric": metric,
                        "policy_name": POLICY_SHORT.get(cmp["policy"], cmp["policy"]),
                        "baseline_name": POLICY_SHORT.get(cmp["baseline"], cmp["baseline"]),
                        "n": cmp["n"],
                        "arm_mean": cmp["policy_mean"],
                        "base_mean": cmp["baseline_mean"],
                        "diff": cmp["diff"],
                        "diff_low": cmp["diff_low"],
                        "diff_high": cmp["diff_high"],
                        "p_gt_0": cmp["p_gt_0"],
                        "cohens_d": cmp["cohens_d"],
                    }
                )
    return rows


def table_live() -> List[Dict]:
    """Live-system measurements from the real gateway and workers."""
    path = os.path.join(RESULTS, "live", "live_results.json")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        raw = json.load(fh)

    groups: Dict[Tuple, List[Dict]] = {}
    for entry in raw:
        cfg = entry["config"]
        a = entry["analysis"]
        key = (cfg["policy"], cfg["n_workers"], cfg["heartbeat_interval"])
        groups.setdefault(key, []).append(a)

    rows = []
    for (policy, workers, hb), items in sorted(
        groups.items(), key=lambda kv: (kv[0][1], kv[0][2], POLICY_ORDER.index(kv[0][0]) if kv[0][0] in POLICY_ORDER else 9)
    ):
        n = len(items)
        row = {
            "policy": POLICY_SHORT.get(policy, policy),
            "workers": workers,
            "heartbeat_s": hb,
            "repetitions": n,
        }
        for metric, label in (
            ("advertised_hit_rate", "advertised_hit_rate"),
            ("true_hit_rate", "true_hit_rate"),
            ("stale_affinity_rate", "stale_affinity_rate"),
            ("failure_rate", "failure_rate"),
        ):
            values = []
            for it in items:
                if metric == "failure_rate":
                    total = it.get("requests", 0) or 0
                    values.append(it.get("errors", 0) / total if total else 0.0)
                else:
                    values.append(it.get(metric, 0.0))
            row[f"{label}_mean"] = sum(values) / len(values)
        for metric in ("routing_latency_us_p50", "routing_latency_us_p95", "routing_latency_us_p99"):
            values = [it.get(metric, 0.0) for it in items]
            row[f"{metric}_mean"] = sum(values) / len(values)
        rows.append(row)
    return rows


def table_pool() -> List[Dict]:
    """C++ byte-pool microbenchmark."""
    path = os.path.join(RESULTS, "microbench", "pool_benchmark.json")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        report = json.load(fh)
    rows = []
    for r in report["rows"]:
        rows.append(
            {
                "operation": r["operation"],
                "payload": f"{r['payload_bytes'] // 1024} KiB",
                "pool_capacity": "unbounded" if r["pool_capacity"] == 0 else r["pool_capacity"],
                "p50_us_mean": r["p50_us"],
                "p95_us_mean": r["p95_us"],
                "p99_us_mean": r["p99_us"],
                "throughput_mib_s_mean": r.get("throughput_mib_s", float("nan")),
            }
        )
    return rows


def table_router_bench() -> List[Dict]:
    """Go routing-decision microbenchmark, parsed from ``go test -bench``."""
    path = os.path.join(RESULTS, "microbench", "router_bench.txt")
    if not os.path.exists(path):
        return []
    rows = []
    pattern = re.compile(
        r"^(Benchmark\S+?)(?:/workers=(\d+))?-?\d*\s+(\d+)\s+([\d.]+) ns/op(?:\s+(\d+) B/op\s+(\d+) allocs/op)?"
    )
    with open(path) as fh:
        for line in fh:
            m = pattern.match(line.strip())
            if not m:
                continue
            name, workers, _iters, ns, b_op, allocs = m.groups()
            rows.append(
                {
                    "benchmark": name,
                    "workers": int(workers) if workers else "-",
                    "ns_per_op": float(ns),
                    "b_per_op": int(b_op) if b_op else 0,
                    "allocs_per_op": int(allocs) if allocs else 0,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def fig_hitrate_vs_workers(main_runs: List[Dict]):
    sub = filter_runs(
        main_runs, capacity=8, heartbeat_interval=3.0, ttl=10.0, host_capacity=64, workload_kind="zipf"
    )
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0), sharex=True)
    for ax, metric, title in (
        (axes[0], "true_hit_rate", "True cache-hit rate"),
        (axes[1], "fast_tier_hit_rate", "Fast-tier hit rate"),
    ):
        for policy, marker, style in (
            ("affinity", "o", "-"),
            ("roundrobin", "s", "--"),
            ("leastloaded", "^", "-."),
            ("oracle", "D", "-"),
        ):
            xs, ys, los, his = [], [], [], []
            for w in (1, 2, 4, 8):
                group = [r for r in sub if r.get("n_workers") == w and r.get("policy") == policy]
                if not group:
                    continue
                ci = bootstrap_cell(group, metric)
                xs.append(w)
                ys.append(ci["mean"])
                los.append(ci["mean"] - ci["low"])
                his.append(ci["high"] - ci["mean"])
            if not xs:
                continue
            ax.errorbar(
                xs, ys, yerr=[los, his], marker=marker, capsize=3, linestyle=style,
                label=POLICY_SHORT[policy],
            )
        ax.set_xscale("log", base=2)
        ax.set_xticks([1, 2, 4, 8])
        ax.set_xticklabels(["1", "2", "4", "8"])
        ax.set_xlabel("Number of workers")
        ax.set_title(title)
    axes[0].set_ylabel("Rate")
    axes[0].legend(fontsize=8)
    fig.savefig(os.path.join(FIGURES_OUT, "hitrate_vs_workers.pdf"))
    plt.close(fig)


def fig_hitrate_vs_capacity(cap_runs: List[Dict]):
    sub = filter_runs(cap_runs, host_capacity=64, workload_kind="zipf")
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0), sharey=True)
    for ax, metric, title in (
        (axes[0], "true_hit_rate", "True cache-hit rate"),
        (axes[1], "fast_tier_hit_rate", "Fast-tier hit rate"),
    ):
        for policy in ("affinity", "roundrobin"):
            xs, ys, los, his = [], [], [], []
            for cap in (2, 4, 8, 16, 32, 64, 128):
                group = [r for r in sub if r.get("capacity") == cap and r.get("policy") == policy]
                if not group:
                    continue
                ci = bootstrap_cell(group, metric)
                xs.append(cap)
                ys.append(ci["mean"])
                los.append(ci["mean"] - ci["low"])
                his.append(ci["high"] - ci["mean"])
            if xs:
                ax.errorbar(xs, ys, yerr=[los, his], marker="o", capsize=3, label=POLICY_SHORT[policy])
        ax.set_xscale("log", base=2)
        ax.set_xticks([2, 8, 32, 128])
        ax.set_xticklabels(["2", "8", "32", "128"])
        ax.set_xlabel("Fast-tier capacity (entries)")
        ax.set_title(title)
    axes[0].set_ylabel("Rate")
    axes[0].legend(fontsize=8)
    fig.savefig(os.path.join(FIGURES_OUT, "hitrate_vs_capacity.pdf"))
    plt.close(fig)


def fig_staleness(stale_runs: List[Dict]):
    sub = filter_runs(stale_runs, heartbeat_interval=3.0, heartbeat_delay=0.0, heartbeat_jitter=0.0)
    fig, ax = plt.subplots(figsize=(4.4, 3.0))
    for drop, marker in ((0.0, "o"), (0.1, "s"), (0.3, "^")):
        xs, ys, los, his = [], [], [], []
        for ttl in (5.0, 10.0, 20.0):
            group = [
                r
                for r in sub
                if r.get("ttl") == ttl and r.get("heartbeat_drop") == drop and r.get("policy") == "affinity"
            ]
            if not group:
                continue
            ci = bootstrap_cell(group, "true_hit_rate")
            xs.append(ttl)
            ys.append(ci["mean"])
            los.append(ci["mean"] - ci["low"])
            his.append(ci["high"] - ci["mean"])
        if xs:
            ax.errorbar(xs, ys, yerr=[los, his], marker=marker, capsize=3, label=f"drop={drop:g}")
    ax.set_xlabel("Staleness window (TTL, s)")
    ax.set_ylabel("True cache-hit rate")
    ax.set_title("Effect of heartbeat loss and TTL")
    ax.legend(fontsize=8)
    fig.savefig(os.path.join(FIGURES_OUT, "staleness.pdf"))
    plt.close(fig)


def fig_queueing_vs_load(sat_runs: List[Dict]):
    load_order = ["low_load", "medium_load", "high_load", "saturated_load", "overloaded_load"]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.0))
    for policy in POLICY_ORDER:
        xs, ys, los, his = [], [], [], []
        for workload in load_order:
            group = [
                r
                for r in sat_runs
                if r.get("workload_name") == workload and r.get("policy") == policy
            ]
            if not group:
                continue
            ci = bootstrap_cell(group, "queueing_delay_p95_ms")
            xs.append(len(xs))
            # A zero delay cannot be shown on a log axis; it is plotted at the
            # axis floor and the caption says so.
            ys.append(max(ci["mean"], 0.05))
            los.append(ci["mean"] - ci["low"])
            his.append(ci["high"] - ci["mean"])
        if xs:
            axes[0].errorbar(xs, ys, yerr=[los, his], marker="o", capsize=3, label=POLICY_SHORT[policy])
    axes[0].set_xticks(range(len(load_order)))
    axes[0].set_xticklabels([r"$\rho$=0.25", "0.50", "0.75", "0.90", "1.05"], fontsize=8)
    axes[0].set_xlabel("Offered load per worker")
    axes[0].set_ylabel("p95 queueing delay (ms, log scale)")
    axes[0].set_yscale("log")
    axes[0].set_ylim(0.03, 1e5)
    axes[0].set_title("Queueing delay vs. offered load")
    axes[0].legend(fontsize=8)

    for policy, marker, style in (
        ("affinity", "o", "-"),
        ("roundrobin", "s", "--"),
        ("leastloaded", "^", "-."),
        ("oracle", "D", "-"),
    ):
        xs, ys, los, his = [], [], [], []
        for workload in load_order:
            group = [
                r
                for r in sat_runs
                if r.get("workload_name") == workload and r.get("policy") == policy
            ]
            if not group:
                continue
            ci = bootstrap_cell(group, "fast_tier_hit_rate")
            xs.append(len(xs))
            ys.append(ci["mean"])
            los.append(ci["mean"] - ci["low"])
            his.append(ci["high"] - ci["mean"])
        if xs:
            axes[1].errorbar(
                xs, ys, yerr=[los, his], marker=marker, capsize=3, linestyle=style,
                label=POLICY_SHORT[policy],
            )
    axes[1].set_xticks(range(len(load_order)))
    axes[1].set_xticklabels([r"$\rho$=0.25", "0.50", "0.75", "0.90", "1.05"], fontsize=8)
    axes[1].set_xlabel("Offered load per worker")
    axes[1].set_ylabel("Fast-tier hit rate")
    axes[1].set_title("Cache locality vs. offered load")
    axes[1].legend(fontsize=8, loc="center right")
    fig.savefig(os.path.join(FIGURES_OUT, "queueing_vs_load.pdf"))
    plt.close(fig)


def fig_router_overhead():
    path = os.path.join(RESULTS, "microbench", "router_bench.txt")
    if not os.path.exists(path):
        return
    rows = table_router_bench()
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(4.4, 3.0))
    for bench, label, marker in (
        ("BenchmarkSelectAffinityHit", "affinity (hit)", "o"),
        ("BenchmarkSelectAffinityMiss", "affinity (miss)", "s"),
        ("BenchmarkSelectRoundRobin", "round-robin", "^"),
        ("BenchmarkSelectLeastLoaded", "least-loaded", "D"),
    ):
        xs, ys = [], []
        for r in rows:
            if r["benchmark"] == bench:
                xs.append(int(r["workers"]))
                ys.append(r["ns_per_op"])
        if xs:
            ax.plot(xs, ys, marker=marker, label=label)
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xticks([1, 2, 4, 8, 16, 32, 64, 128])
    ax.set_xticklabels(["1", "2", "4", "8", "16", "32", "64", "128"])
    ax.set_xlabel("Number of registered workers")
    ax.set_ylabel("Routing decision (ns/op, log scale)")
    ax.set_title("Gateway routing-decision cost")
    ax.legend(fontsize=8)
    fig.savefig(os.path.join(FIGURES_OUT, "router_overhead.pdf"))
    plt.close(fig)


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def emit(name: str, rows: List[Dict], columns: Sequence[Tuple[str, str]], caption: str, label: str,
         percent: bool = True, digits: int = 1, span: bool = False,
         headers: Optional[Sequence[str]] = None) -> None:
    if not rows:
        return
    tex = to_latex(rows, columns, caption=caption, label=label, digits=digits, percent=percent,
                   span=span, headers=headers)
    with open(os.path.join(TABLES_OUT, f"{name}.tex"), "w") as fh:
        fh.write(tex + "\n")
    # A body-only fragment, for the tables whose caption and column headers
    # the manuscript supplies itself.
    body = to_latex(rows, columns, digits=digits, percent=percent, body_only=True)
    with open(os.path.join(TABLES_OUT, f"{name}_body.tex"), "w") as fh:
        fh.write(body + "\n")
    md = to_markdown(rows, columns, digits=digits, percent=percent)
    with open(os.path.join(MD_OUT, f"{name}.md"), "w") as fh:
        fh.write(f"### {caption}\n\n{md}\n")


def write_aggregate_csv(all_runs: Dict[str, List[Dict]]) -> None:
    path = os.path.join(RESULTS, "aggregate.csv")
    rows = []
    for name, runs in all_runs.items():
        for key, group in group_by(runs, ["workload_kind", "policy", "n_workers", "capacity", "host_capacity",
                                          "heartbeat_interval", "ttl", "heartbeat_delay", "heartbeat_jitter",
                                          "heartbeat_drop", "churn_interval", "churn_downtime"]).items():
            stats = cell_stats(group)
            row = dict(zip(["workload_kind", "policy", "n_workers", "capacity", "host_capacity",
                            "heartbeat_interval", "ttl", "heartbeat_delay", "heartbeat_jitter",
                            "heartbeat_drop", "churn_interval", "churn_downtime"], key))
            row["sweep"] = name
            row.update({k: v for k, v in stats.items()})
            rows.append(row)
    if not rows:
        return
    fieldnames = sorted({k for r in rows for k in r})
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ensure_dirs()
    all_runs = load_all()
    main_runs = all_runs["main"]

    emit(
        "policy_comparison",
        table_policy_comparison(main_runs),
        [
            ("Workload", "workload"),
            ("Policy", "policy"),
            ("Adv. hit \\%", "advertised_hit_rate"),
            ("True hit \\%", "true_hit_rate"),
            ("Fast-tier \\%", "fast_tier_hit_rate"),
            ("Host-tier \\%", "host_tier_hit_rate"),
            ("Stale \\%", "stale_affinity_rate"),
            ("Degraded \\%", "degraded_affinity_rate"),
            ("CV", "worker_imbalance_cv", False),
            ("Fail \\%", "failure_rate"),
        ],
        "Policy comparison at the reference configuration (4 workers, fast tier of 8 entries, "
        "3 s heartbeat, 10 s TTL). Brackets give bootstrap 95\\% confidence intervals over 5 repetitions "
        "of 10,000 requests each. ``Stale'' is the fraction of advertised hits whose state was absent "
        "from both tiers at execution time; ``Degraded'' is the fraction that had to be served from the "
        "host tier instead of the fast tier. A dash means the quantity is undefined because the policy "
        "produced no advertised hits.",
        "tab:policy-comparison",
        percent=True,
        digits=1,
        span=True,
        headers=[
            "Workload", "Policy", "Adv. hit", "True hit", "Fast tier", "Host tier",
            "Stale", "Degraded", "CV", "Fail",
        ],
    )

    emit(
        "worker_scaling",
        table_worker_scaling(main_runs),
        [
            ("Workers", "workers", False),
            ("Policy", "policy"),
            ("Adv. hit \\%", "advertised_hit_rate"),
            ("True hit \\%", "true_hit_rate"),
            ("Fast-tier \\%", "fast_tier_hit_rate"),
            ("CV", "worker_imbalance_cv", False),
            ("p95 queue (ms)", "queueing_delay_p95_ms", False),
        ],
        "Effect of the mesh size on cache locality under the hot-set workload (fast tier of 8 entries, "
        "3 s heartbeat, 10 s TTL). The offered load is held at a constant utilisation per worker.",
        "tab:worker-scaling",
        percent=True,
        digits=3,
            span=True,
    )

    emit(
        "capacity",
        table_capacity(all_runs["capacity"]),
        [
            ("Fast tier", "capacity", False),
            ("Policy", "policy"),
            ("True hit \\%", "true_hit_rate"),
            ("Fast-tier \\%", "fast_tier_hit_rate"),
            ("Host-tier \\%", "host_tier_hit_rate"),
        ],
        "Effect of the fast-tier capacity on cache locality (4 workers, host tier of 64 entries).",
        "tab:capacity",
        percent=True,
        digits=1,
            span=True,
    )

    emit(
        "host_capacity",
        table_host_capacity(all_runs["capacity"]),
        [
            ("Host tier", "host_capacity", False),
            ("Policy", "policy"),
            ("True hit \\%", "true_hit_rate"),
            ("Fast-tier \\%", "fast_tier_hit_rate"),
            ("Host-tier \\%", "host_tier_hit_rate"),
        ],
        "Effect of the host-tier (byte pool) capacity on recoverability of evicted prefixes "
        "(4 workers, fast tier of 8 entries).",
        "tab:host-capacity",
        percent=True,
        digits=1,
            span=True,
    )

    emit(
        "staleness",
        table_staleness(all_runs["staleness"]),
        [
            ("TTL (s)", "ttl_s", False),
            ("Drop rate", "drop_rate", False),
            ("Adv. hit \\%", "advertised_hit_rate"),
            ("True hit \\%", "true_hit_rate"),
            ("Stale \\%", "stale_affinity_rate"),
            ("Degraded \\%", "degraded_affinity_rate"),
        ],
        "Effect of heartbeat loss and of the staleness window on the affinity policy "
        "(4 workers, 3 s heartbeat).",
        "tab:staleness",
        percent=True,
        digits=1,
            span=True,
    )

    emit(
        "staleness_delay",
        table_staleness_delay(all_runs["staleness"]),
        [
            ("Delay (s)", "delay_s", False),
            ("Jitter (s)", "jitter_s", False),
            ("Adv. hit \\%", "advertised_hit_rate"),
            ("True hit \\%", "true_hit_rate"),
            ("Degraded \\%", "degraded_affinity_rate"),
        ],
        "Effect of heartbeat delay and jitter on the affinity policy (4 workers, 10 s TTL, no loss).",
        "tab:staleness-delay",
        percent=True,
        digits=1,
            span=True,
    )

    emit(
        "churn",
        table_churn(all_runs["churn"]),
        [
            ("Workers", "workers", False),
            ("Churn (s)", "churn_interval_s", False),
            ("Down (s)", "downtime_s", False),
            ("Policy", "policy"),
            ("Adv. hit \\%", "advertised_hit_rate"),
            ("True hit \\%", "true_hit_rate"),
            ("Fail \\%", "failure_rate"),
        ],
        "Effect of worker churn on routing accuracy and request failures "
        "(hot-set workload, fast tier of 8 entries).",
        "tab:churn",
        percent=True,
        digits=1,
            span=True,
    )

    emit(
        "saturation",
        table_saturation(all_runs["saturation"]),
        [
            ("Offered load", "offered_load"),
            ("Policy", "policy"),
            ("True hit \\%", "true_hit_rate"),
            ("Fast-tier \\%", "fast_tier_hit_rate"),
            ("p95 queue (ms)", "queueing_delay_p95_ms", False),
            ("CV", "worker_imbalance_cv", False),
        ],
        "Effect of the offered load on cache locality and on the queueing delay "
        "(4 workers, fast tier of 8 entries).",
        "tab:saturation",
        percent=True,
        digits=3,
            span=True,
    )

    emit(
        "heartbeat_interval",
        table_heartbeat_interval(main_runs),
        [
            ("Heartbeat (s)", "heartbeat_s", False),
            ("Policy", "policy"),
            ("Adv. hit \\%", "advertised_hit_rate"),
            ("True hit \\%", "true_hit_rate"),
            ("Fast-tier \\%", "fast_tier_hit_rate"),
            ("Degraded \\%", "degraded_affinity_rate"),
            ("Stale \\%", "stale_affinity_rate"),
        ],
        "Effect of the heartbeat period on the freshness of the gateway's cache index "
        "(4 workers, fast tier of 8 entries, 10 s TTL, hot-set workload).",
        "tab:heartbeat-interval",
        percent=True,
        digits=1,
            span=True,
    )

    emit(
        "significance",
        table_significance(main_runs),
        [
            ("Workload", "workload"),
            ("Metric", "metric"),
            ("Policy", "policy_name"),
            ("Baseline", "baseline_name"),
            ("n", "n", False),
            ("Diff", "diff"),
            ("95\\% CI low", "diff_low", False),
            ("95\\% CI high", "diff_high", False),
            ("P(diff>0)", "p_gt_0", False),
            ("Cohen's $d$", "cohens_d", False),
        ],
        "Paired bootstrap comparison of each policy against round-robin at the reference "
        "configuration. Differences are reported as fractions, so a difference of 0.064 is "
        "6.4 percentage points. A dash in the Cohen's $d$ column means the effect is perfectly "
        "separated but its standardised size is undefined because the replicates had zero "
        "between-run variance.",
        "tab:significance",
        percent=False,
        digits=4,
    )

    live_rows = table_live()
    emit(
        "live",
        live_rows,
        [
            ("Policy", "policy"),
            ("Workers", "workers", False),
            ("Heartbeat (s)", "heartbeat_s", False),
            ("Adv. hit \\%", "advertised_hit_rate"),
            ("True hit \\%", "true_hit_rate"),
            ("Stale \\%", "stale_affinity_rate"),
            ("Fail \\%", "failure_rate"),
            ("p50 ($\\mu$s)", "routing_latency_us_p50", False),
            ("p95 ($\\mu$s)", "routing_latency_us_p95", False),
            ("p99 ($\\mu$s)", "routing_latency_us_p99", False),
        ],
        "Live-system measurements taken from the real Go gateway and the real Python workers over gRPC. "
        "Routing latencies are the gateway's own decision timings.",
        "tab:live",
        percent=True,
        digits=1,
        span=True,
    )

    pool_rows = table_pool()
    emit(
        "pool",
        pool_rows,
        [
            ("Operation", "operation"),
            ("Payload", "payload"),
            ("Pool capacity", "pool_capacity"),
            ("p50 ($\\mu$s)", "p50_us_mean", False),
            ("p95 ($\\mu$s)", "p95_us_mean", False),
            ("p99 ($\\mu$s)", "p99_us_mean", False),
            ("MiB/s", "throughput_mib_s_mean", False),
        ],
        "Microbenchmark of the C++/pybind11 host-memory byte pool. These are host-memory copy "
        "latencies; they are not PCIe or GPU transfer latencies.",
        "tab:pool",
        percent=False,
        digits=2,
        span=True,
    )

    bench_rows = table_router_bench()
    if bench_rows:
        with open(os.path.join(TABLES_OUT, "router_bench.tex"), "w") as fh:
            fh.write(
                to_latex(
                    bench_rows,
                    [
                        ("Benchmark", "benchmark"),
                        ("Workers", "workers", False),
                        ("ns/op", "ns_per_op"),
                        ("B/op", "b_per_op"),
                        ("allocs/op", "allocs_per_op"),
                    ],
                    caption="Microbenchmark of the gateway's routing decision in the Go implementation "
                    "(Intel Core i7-12700H, Go 1.25.0).",
                    label="tab:router-bench",
                    digits=0,
                    percent=False,
                )
                + "\n"
            )
        with open(os.path.join(MD_OUT, "router_bench.md"), "w") as fh:
            fh.write(
                "### Gateway routing-decision microbenchmark\n\n"
                + to_markdown(
                    bench_rows,
                    [
                        ("Benchmark", "benchmark"),
                        ("Workers", "workers", False),
                        ("ns/op", "ns_per_op"),
                        ("B/op", "b_per_op"),
                        ("allocs/op", "allocs_per_op"),
                    ],
                    digits=0,
                )
                + "\n"
            )

    # Figures
    fig_hitrate_vs_workers(main_runs)
    fig_hitrate_vs_capacity(all_runs["capacity"])
    fig_staleness(all_runs["staleness"])
    fig_queueing_vs_load(all_runs["saturation"])
    fig_router_overhead()

    write_aggregate_csv(all_runs)

    # Machine-readable copy of every table.
    tables = {
        "policy_comparison": table_policy_comparison(main_runs),
        "worker_scaling": table_worker_scaling(main_runs),
        "capacity": table_capacity(all_runs["capacity"]),
        "host_capacity": table_host_capacity(all_runs["capacity"]),
        "staleness": table_staleness(all_runs["staleness"]),
        "staleness_delay": table_staleness_delay(all_runs["staleness"]),
        "churn": table_churn(all_runs["churn"]),
        "heartbeat_interval": table_heartbeat_interval(main_runs),
        "significance": table_significance(main_runs),
        "saturation": table_saturation(all_runs["saturation"]),
        "live": live_rows,
        "pool": pool_rows,
        "router_bench": bench_rows,
    }
    with open(os.path.join(RESULTS, "tables.json"), "w") as fh:
        json.dump(tables, fh, indent=2)

    print("[report] tables written to", TABLES_OUT)
    print("[report] figures written to", FIGURES_OUT)
    for name, rows in tables.items():
        print(f"  {name:22s} {len(rows):4d} rows")


if __name__ == "__main__":
    main()
