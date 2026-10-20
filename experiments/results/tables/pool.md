### Microbenchmark of the C++/pybind11 host-memory byte pool. These are host-memory copy latencies; they are not PCIe or GPU transfer latencies.

| Operation | Payload | Pool capacity | p50 ($\mu$s) | p95 ($\mu$s) | p99 ($\mu$s) | MiB/s |
|---|---|---|---|---|---|---|
| offload\_cache | 1 KiB | unbounded | 0.48 | 2.70 | 4.47 | 835.92 |
| offload\_cache | 64 KiB | unbounded | 39.96 | 80.73 | 129.59 | 1344.25 |
| offload\_cache | 1024 KiB | unbounded | 995.77 | 2051.56 | 4755.03 | 788.98 |
| prefetch\_cache | 1 KiB | unbounded | 0.84 | 1.13 | 4.00 | 1038.87 |
| prefetch\_cache | 64 KiB | unbounded | 19.81 | 43.77 | 93.10 | 2275.91 |
| prefetch\_cache | 1024 KiB | unbounded | 456.77 | 1139.86 | 1594.93 | 1672.08 |
| offload\_cache | 1 KiB | 256 | 0.52 | 0.69 | 1.40 | 1640.63 |
| offload\_cache | 64 KiB | 256 | 8.25 | 39.49 | 62.80 | 4891.83 |
| offload\_cache | 1024 KiB | 256 | 338.55 | 748.03 | 1042.96 | 2603.37 |
| prefetch\_cache | 1 KiB | 256 | 0.27 | 0.81 | 1.09 | 2805.18 |
| prefetch\_cache | 64 KiB | 256 | 0.27 | 18.69 | 21.20 | 19287.03 |
| prefetch\_cache | 1024 KiB | 256 | 0.28 | 374.98 | 572.04 | 15821.41 |
| is\_offloaded | 1 KiB | unbounded | 0.33 | 0.39 | 1.19 | -- |
