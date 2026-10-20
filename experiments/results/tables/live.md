### Live-system measurements taken from the real Go gateway and the real Python workers over gRPC. Routing latencies are the gateway's own decision timings.

| Policy | Workers | Heartbeat (s) | Adv. hit \% | True hit \% | Stale \% | Fail \% | p50 ($\mu$s) | p95 ($\mu$s) | p99 ($\mu$s) |
|---|---|---|---|---|---|---|---|---|---|
| affinity | 1 | 1.0 | 61.0 | 87.1 | 0.0 | 0.0 | 1.0 | 5.3 | 12.0 |
| round-robin | 1 | 1.0 | 0.0 | 87.1 | 0.0 | 0.0 | 1.0 | 4.3 | 11.7 |
| least-loaded | 1 | 1.0 | 0.0 | 87.1 | 0.0 | 0.0 | 1.0 | 5.7 | 11.3 |
| affinity | 1 | 3.0 | 55.6 | 87.1 | 0.0 | 0.0 | 1.0 | 5.7 | 11.7 |
| round-robin | 1 | 3.0 | 0.0 | 87.1 | 0.0 | 0.0 | 1.0 | 4.3 | 11.3 |
| least-loaded | 1 | 3.0 | 0.0 | 87.1 | 0.0 | 0.0 | 1.0 | 4.7 | 10.3 |
| affinity | 2 | 1.0 | 72.2 | 84.6 | 0.0 | 0.0 | 1.3 | 6.7 | 12.0 |
| round-robin | 2 | 1.0 | 0.0 | 84.2 | 0.0 | 0.0 | 1.0 | 5.7 | 11.7 |
| least-loaded | 2 | 1.0 | 0.0 | 84.4 | 0.0 | 0.0 | 2.0 | 7.3 | 15.3 |
| affinity | 2 | 3.0 | 69.6 | 84.6 | 0.0 | 0.0 | 1.3 | 6.3 | 13.7 |
| round-robin | 2 | 3.0 | 0.0 | 84.2 | 0.0 | 0.0 | 1.0 | 5.3 | 11.0 |
| least-loaded | 2 | 3.0 | 0.0 | 84.3 | 0.0 | 0.0 | 1.3 | 7.0 | 16.3 |
| affinity | 4 | 1.0 | 78.2 | 82.5 | 0.0 | 0.0 | 2.0 | 8.7 | 16.0 |
| round-robin | 4 | 1.0 | 0.0 | 79.8 | 0.0 | 0.0 | 1.0 | 7.0 | 19.0 |
| least-loaded | 4 | 1.0 | 0.0 | 81.7 | 0.0 | 0.0 | 2.0 | 10.0 | 18.7 |
| affinity | 4 | 3.0 | 77.8 | 82.3 | 0.0 | 0.0 | 2.0 | 7.0 | 14.7 |
| round-robin | 4 | 3.0 | 0.0 | 79.8 | 0.0 | 0.0 | 1.0 | 6.0 | 14.0 |
| least-loaded | 4 | 3.0 | 0.0 | 80.7 | 0.0 | 0.0 | 2.0 | 10.3 | 21.3 |
