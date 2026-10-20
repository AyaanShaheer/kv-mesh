"""Microbenchmark for the C++ host-memory byte pool (pybind11 extension).

IMPORTANT (manuscript §5.4, §7.3): the numbers produced here measure a *host
memory copy* performed by the C++ extension. They are not PCIe transfer
latencies and not GPU transfer latencies: the extension stores byte payloads
in a ``std::unordered_map`` on the host and copies them back out. The
manuscript reports them as "memory path latency" and never as GPU/CPU
transfer latency.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "worker"))

import kv_cpp  # noqa: E402


def bench_offload(payload_sizes: List[int], iterations: int, capacity: int) -> List[Dict]:
    rows = []
    for size in payload_sizes:
        payload = b"\x5a" * size
        manager = kv_cpp.KVMemoryManager(capacity)
        manager.set_verbose(False)
        samples = []
        for i in range(iterations):
            key = i + 1
            t0 = time.perf_counter()
            manager.offload_cache(key, payload)
            samples.append((time.perf_counter() - t0) * 1e6)  # microseconds
        samples.sort()
        rows.append(
            {
                "operation": "offload_cache",
                "payload_bytes": size,
                "pool_capacity": capacity,
                "iterations": iterations,
                "mean_us": statistics.fmean(samples),
                "p50_us": samples[len(samples) // 2],
                "p95_us": samples[int(0.95 * len(samples))],
                "p99_us": samples[int(0.99 * len(samples))],
                "max_us": samples[-1],
                "throughput_mib_s": (size * iterations / (sum(samples) / 1e6)) / (1024 * 1024),
            }
        )
    return rows


def bench_prefetch(payload_sizes: List[int], iterations: int, capacity: int) -> List[Dict]:
    rows = []
    for size in payload_sizes:
        payload = b"\x5a" * size
        manager = kv_cpp.KVMemoryManager(capacity)
        manager.set_verbose(False)
        for i in range(iterations):
            manager.offload_cache(i + 1, payload)
        samples = []
        for i in range(iterations):
            key = i + 1
            t0 = time.perf_counter()
            manager.prefetch_cache(key)
            samples.append((time.perf_counter() - t0) * 1e6)
        samples.sort()
        rows.append(
            {
                "operation": "prefetch_cache",
                "payload_bytes": size,
                "pool_capacity": capacity,
                "iterations": iterations,
                "mean_us": statistics.fmean(samples),
                "p50_us": samples[len(samples) // 2],
                "p95_us": samples[int(0.95 * len(samples))],
                "p99_us": samples[int(0.99 * len(samples))],
                "max_us": samples[-1],
                "throughput_mib_s": (size * iterations / (sum(samples) / 1e6)) / (1024 * 1024),
            }
        )
    return rows


def bench_is_offloaded(iterations: int) -> Dict:
    manager = kv_cpp.KVMemoryManager(0)
    manager.set_verbose(False)
    for i in range(1024):
        manager.offload_cache(i, b"x" * 1024)
    samples = []
    for i in range(iterations):
        t0 = time.perf_counter()
        manager.is_offloaded(i % 1024)
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    return {
        "operation": "is_offloaded",
        "payload_bytes": 1024,
        "pool_capacity": 0,
        "iterations": iterations,
        "mean_us": statistics.fmean(samples),
        "p50_us": samples[len(samples) // 2],
        "p95_us": samples[int(0.95 * len(samples))],
        "p99_us": samples[int(0.99 * len(samples))],
        "max_us": samples[-1],
    }


def bench_hash_consistency() -> Dict:
    """Confirm the Python and C++ sides agree on the fingerprint of a prompt."""
    import hashlib

    prompts = [f"SYSTEM CONTEXT v1 :: session=k{i:05d}" for i in range(1000)]
    t0 = time.perf_counter()
    py_hashes = [int(hashlib.md5(p.encode()).hexdigest()[:16], 16) for p in prompts]
    py_us = (time.perf_counter() - t0) * 1e6 / len(prompts)

    manager = kv_cpp.KVMemoryManager(0)
    manager.set_verbose(False)
    t0 = time.perf_counter()
    for h in py_hashes:
        manager.offload_cache(h, b"x")
    cpp_us = (time.perf_counter() - t0) * 1e6 / len(py_hashes)

    present = sum(1 for h in py_hashes if manager.is_offloaded(h))
    return {
        "prompts": len(prompts),
        "python_hash_mean_us": py_us,
        "cpp_offload_mean_us": cpp_us,
        "resident_after_offload": present,
        "all_resident": present == len(py_hashes),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="experiments/results/microbench/pool_benchmark.json")
    ap.add_argument("--iterations", type=int, default=2000)
    args = ap.parse_args()

    payload_sizes = [1024, 64 * 1024, 1024 * 1024]
    rows = []
    for capacity in (0, 256):
        rows.extend(bench_offload(payload_sizes, args.iterations, capacity))
        rows.extend(bench_prefetch(payload_sizes, args.iterations, capacity))
    rows.append(bench_is_offloaded(args.iterations * 10))

    report = {
        "note": (
            "Host-memory copy timings of the pybind11 byte pool. These are NOT "
            "PCIe or GPU transfer latencies."
        ),
        "rows": rows,
        "hash_consistency": bench_hash_consistency(),
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2)

    print(f"{'operation':16s} {'bytes':>9s} {'cap':>6s} {'p50 us':>10s} {'p95 us':>10s} {'MiB/s':>10s}")
    for r in rows:
        print(
            f"{r['operation']:16s} {r['payload_bytes']:>9d} {r['pool_capacity']:>6d} "
            f"{r['p50_us']:>10.2f} {r['p95_us']:>10.2f} {r.get('throughput_mib_s', float('nan')):>10.1f}"
        )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
