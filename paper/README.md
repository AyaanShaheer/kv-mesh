# The KV-Mesh manuscript

This directory holds the LaTeX source of the KV-Mesh paper and the
machine-generated tables and figures it includes.

```
paper/
├── kv-mesh.tex      manuscript source
├── kv-mesh.pdf      built manuscript
├── tables/          LaTeX fragments produced by the report generator
└── figures/         vector figures produced by the report generator
```

## Building

The manuscript uses only packages that `tectonic` can fetch on demand, so a
self-contained TeX installation is not required:

```bash
cd paper
tectonic -X compile kv-mesh.tex --outdir .
```

A full LaTeX installation works too:

```bash
pdflatex kv-mesh.tex && pdflatex kv-mesh.tex
```

## Regenerating the tables and figures

The tables and figures are **generated from the archived experimental
results**. Never edit `tables/*.tex` by hand: they are overwritten.

```bash
# From the repository root
make experiments   # run the sweeps, the live arm and the microbenchmarks
make report        # regenerate paper/tables/*.tex and paper/figures/*.pdf
make paper         # rebuild the PDF
```

`make report` reads:

| Source | Produces |
|---|---|
| `experiments/results/main/` | `policy_comparison`, `worker_scaling`, `significance`, `heartbeat_interval` |
| `experiments/results/staleness/` | `staleness`, `staleness_delay` |
| `experiments/results/churn/` | `churn` |
| `experiments/results/capacity/` | `capacity`, `host_capacity` |
| `experiments/results/saturation/` | `saturation` |
| `experiments/results/live/live_results.json` | `live` |
| `experiments/results/microbench/` | `pool`, `router_bench` |

## Conventions in the manuscript

* **No claim is made that the code was not measured.** Every quantitative
  statement in §6 is backed by a table in `tables/`, and every table is
  generated from an archived run.
* **Simulator, live-system and microbenchmark results are never pooled.**
  They appear in separate tables and the text says which arm a number comes
  from.
* **The cache is modelled, not real.** The worker does not run a model, does
  not allocate CUDA memory and does not perform a PCIe transfer. The paper
  says so in §2, §4 and §7.3, and the byte-pool timings are explicitly
  labelled as host-memory copy latencies.
* **A dash in a table means "not defined"**, not "zero": for example the
  stale-affinity rate is undefined for a policy that produced no advertised
  hits.

## Adding a new experiment

1. Add the configuration to `experiments/configs/`.
2. Add the sweep or benchmark driver under `experiments/scripts/`.
3. Add the table builder and the `emit(...)` call in
   `experiments/scripts/make_report.py`.
4. Reference the table from `kv-mesh.tex` with
   `\input{tables/<name>.tex}`.
5. Re-run `make experiments report paper`.
