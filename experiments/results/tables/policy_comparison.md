### Policy comparison at the reference configuration (4 workers, fast tier of 8 entries, 3 s heartbeat, 10 s TTL). Brackets give bootstrap 95\% confidence intervals over 5 repetitions of 10,000 requests each. ``Stale'' is the fraction of advertised hits whose state was absent from both tiers at execution time; ``Degraded'' is the fraction that had to be served from the host tier instead of the fast tier. A dash means the quantity is undefined because the policy produced no advertised hits.

| Workload | Policy | Adv. hit \% | True hit \% | Fast-tier \% | Host-tier \% | Stale \% | Degraded \% | CV | Fail \% |
|---|---|---|---|---|---|---|---|---|---|
| Exact repeats | affinity | 37.6 [36.88, 38.26] | 97.4 [97.44, 97.44] | 22.6 [22.18, 23.00] | 74.9 [74.44, 75.26] | 0.0 [0.00, 0.00] | 61.9 [61.13, 62.54] | 0.0 [0.01, 0.02] | 0.0 [0.00, 0.00] |
| Exact repeats | round-robin | 0.0 [0.00, 0.00] | 97.4 [97.44, 97.44] | 12.2 [12.09, 12.34] | 85.2 [85.10, 85.35] | -- [--, --] | -- [--, --] | 0.0 [0.00, 0.00] | 0.0 [0.00, 0.00] |
| Exact repeats | least-loaded | 0.0 [0.00, 0.00] | 97.4 [97.44, 97.44] | 12.2 [11.89, 12.48] | 85.3 [84.96, 85.55] | -- [--, --] | -- [--, --] | 0.3 [0.34, 0.34] | 0.0 [0.00, 0.00] |
| Exact repeats | oracle | 0.0 [0.00, 0.00] | 99.4 [99.36, 99.36] | 49.6 [49.38, 49.93] | 49.7 [49.43, 49.98] | -- [--, --] | -- [--, --] | 0.0 [0.00, 0.00] | 0.0 [0.00, 0.00] |
| Hot set (Zipf) | affinity | 68.7 [68.34, 69.08] | 88.8 [88.77, 88.80] | 69.3 [68.75, 69.88] | 19.5 [18.89, 20.04] | 0.0 [0.00, 0.00] | 13.0 [12.56, 13.45] | 0.3 [0.25, 0.35] | 0.0 [0.00, 0.00] |
| Hot set (Zipf) | round-robin | 0.0 [0.00, 0.00] | 88.2 [87.95, 88.42] | 62.9 [62.72, 63.07] | 25.3 [25.01, 25.55] | -- [--, --] | -- [--, --] | 0.0 [0.00, 0.00] | 0.0 [0.00, 0.00] |
| Hot set (Zipf) | least-loaded | 0.0 [0.00, 0.00] | 88.2 [87.99, 88.37] | 62.9 [62.62, 63.12] | 25.3 [24.99, 25.70] | -- [--, --] | -- [--, --] | 0.3 [0.34, 0.35] | 0.0 [0.00, 0.00] |
| Hot set (Zipf) | oracle | 0.0 [0.00, 0.00] | 97.5 [97.49, 97.53] | 78.5 [78.20, 78.90] | 19.0 [18.62, 19.31] | -- [--, --] | -- [--, --] | 1.0 [0.89, 1.10] | 0.0 [0.00, 0.00] |
| Uniform (low reuse) | affinity | 0.7 [0.70, 0.76] | 2.0 [1.85, 2.06] | 0.3 [0.23, 0.27] | 1.7 [1.62, 1.79] | 67.8 [66.47, 69.10] | 31.1 [29.59, 32.69] | 0.0 [0.00, 0.00] | 0.0 [0.00, 0.00] |
| Uniform (low reuse) | round-robin | 0.0 [0.00, 0.00] | 1.8 [1.67, 1.86] | 0.2 [0.21, 0.24] | 1.5 [1.46, 1.63] | -- [--, --] | -- [--, --] | 0.0 [0.00, 0.00] | 0.0 [0.00, 0.00] |
| Uniform (low reuse) | least-loaded | 0.0 [0.00, 0.00] | 1.8 [1.77, 1.88] | 0.2 [0.21, 0.29] | 1.6 [1.55, 1.62] | -- [--, --] | -- [--, --] | 0.3 [0.34, 0.35] | 0.0 [0.00, 0.00] |
| Uniform (low reuse) | oracle | 0.0 [0.00, 0.00] | 7.1 [6.87, 7.25] | 0.8 [0.81, 0.88] | 6.2 [6.01, 6.43] | -- [--, --] | -- [--, --] | 0.0 [0.00, 0.01] | 0.0 [0.00, 0.00] |
