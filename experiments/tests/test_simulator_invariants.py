"""Invariants of the control-plane simulator.

The simulator is a model of the Go router, so the properties that the router
guarantees must also hold in the model. These tests check the properties the
manuscript's results depend on.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from exp.runner import RunConfig, run_simulation  # noqa: E402
from exp.workload import WorkloadSpec, bootstrap_ci, paired_bootstrap_diff  # noqa: E402


def _config(policy="affinity", **kwargs):
    defaults = dict(
        n_workers=4,
        capacity=8,
        host_capacity=64,
        heartbeat_interval=1.0,
        ttl=10.0,
        policy=policy,
        service_time=0.005,
        target_utilization=0.5,
        workload=WorkloadSpec(kind="zipf", n_requests=2000, n_keys=64, zipf_s=1.1, seed=0),
        seed=0,
    )
    defaults.update(kwargs)
    return RunConfig(**defaults)


def test_every_request_is_accounted_for():
    result = run_simulation(_config())
    summary = result.summary
    assert summary["requests_total"] == 2000
    assert summary["requests_served"] + summary["routing_failures"] == 2000
    assert len(result.request_records) == 2000


def test_no_failures_when_no_worker_is_stopped():
    result = run_simulation(_config())
    assert result.summary["routing_failures"] == 0


def test_a_stopped_worker_causes_failures_until_it_ages_out():
    """The gateway does not learn that a worker has died until the TTL.

    The trace has to be long enough for a churn event to fall inside it.
    """
    long_workload = WorkloadSpec(kind="zipf", n_requests=10000, n_keys=64, zipf_s=1.1, seed=0)
    result = run_simulation(
        _config(churn_interval=5.0, churn_downtime=5.0, workload=long_workload)
    )
    assert result.summary["routing_failures"] > 0


def test_affinity_never_reports_an_advertised_hit_without_one():
    """Every advertised hit in the affinity policy is backed by an advertisement."""
    result = run_simulation(_config(policy="affinity"))
    for record in result.request_records:
        if record["advertised_hit"]:
            assert record["match_count"] >= 1


def test_the_baselines_never_report_an_advertised_hit():
    for policy in ("roundrobin", "leastloaded"):
        result = run_simulation(_config(policy=policy))
        assert result.summary["advertised_hits"] == 0


def test_round_robin_is_perfectly_balanced():
    result = run_simulation(_config(policy="roundrobin", n_workers=4))
    assert result.summary["worker_imbalance_cv"] < 1e-9


def test_a_larger_fast_tier_never_hurts_the_fast_tier_hit_rate():
    previous = 0.0
    for capacity in (2, 8, 32, 128):
        result = run_simulation(_config(capacity=capacity))
        served = result.summary["requests_served"]
        rate = result.summary["vram_hits"] / served
        assert rate >= previous - 1e-9
        previous = rate


def test_repeated_runs_of_the_same_configuration_agree():
    """The simulator is deterministic given a configuration."""
    first = run_simulation(_config())
    second = run_simulation(_config())
    assert first.summary["advertised_hits"] == second.summary["advertised_hits"]
    assert first.summary["true_hits"] == second.summary["true_hits"]


def test_different_seeds_produce_different_traces():
    """Repetitions are independent samples rather than replays."""
    first = run_simulation(_config(seed=0, workload=WorkloadSpec(
        kind="zipf", n_requests=2000, n_keys=64, zipf_s=1.1, seed=0)))
    second = run_simulation(_config(seed=1, workload=WorkloadSpec(
        kind="zipf", n_requests=2000, n_keys=64, zipf_s=1.1, seed=1)))
    assert first.summary["advertised_hits"] != second.summary["advertised_hits"]


def test_bootstrap_interval_brackets_the_point_estimate():
    values = [0.1, 0.2, 0.3, 0.25, 0.15]
    point, low, high = bootstrap_ci(values, n_boot=2000, seed=0)
    assert low <= point <= high


def test_paired_bootstrap_detects_a_consistent_difference():
    a = [0.5, 0.52, 0.49, 0.51, 0.50]
    b = [0.4, 0.42, 0.39, 0.41, 0.40]
    result = paired_bootstrap_diff(a, b, n_boot=2000, seed=0)
    assert result["diff"] > 0
    assert result["low"] > 0
    assert result["p_gt_0"] == 1.0
