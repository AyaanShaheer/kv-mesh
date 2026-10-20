# Real-model arm — not executed in the reported results

This directory is the intended home of the real-engine time-to-first-token
measurement, i.e. the fourth (optional) arm of the evaluation described in
§5 of the manuscript.

**It is empty because the arm was not executed successfully in the evaluation
environment**, and the manuscript therefore makes no real-model claim. The
reason is recorded here rather than left implicit, because the same
observation is a limitation a reader should know about.

## What was attempted

The measurement script is `experiments/scripts/bench_realmodel.py`. It starts
a vLLM OpenAI-compatible server with automatic prefix caching enabled, then
measures, for a fixed prompt set and model configuration:

* `cold_ttft_ms` — the time to the first streamed token of a prompt whose
  prefix is not in the engine's cache;
* `warm_ttft_ms` — the same quantity for the same prompt immediately
  afterwards, whose prefix the engine has cached;
* the difference, which is the prefill work that a cache hit avoids.

## Why it is not reported

Two independent blockers were hit on the evaluation machine:

1. **Toolchain.** The CUDA *compiler* (`nvcc`) is not installed system-wide;
   it exists only inside the virtual environment, under
   `nvidia/cu13`. vLLM's kernel compilation needs it on `PATH` and in
   `CUDA_HOME`. The script sets both, and the engine then starts, but the
   FlashInfer sampler additionally ships its own CCCL headers that conflict
   with the bundled toolkit. `VLLM_USE_FLASHINFER_SAMPLER=0` works around the
   conflict.
2. **Server binding.** With those workarounds the engine initialises and
   reports `Application startup complete`, but the HTTP server never binds its
   port in this environment, so no request can be issued against it. The
   in-process API (`vllm.LLM`) does work and produces correct completions,
   but it cannot be used to measure a streamed time-to-first-token.

The engine also takes roughly 20 minutes to initialise on this machine, so
each attempt is expensive.

## What would be needed

* A machine where the vLLM server binds and serves, or a switch to the
  in-process API with an explicit first-token measurement.
* Two engines behind the gateway, so that the *placement* decision can be
  evaluated end to end: with affinity, a repeated prompt reaches the worker
  that already holds its prefix; with round-robin, it reaches one of the
  workers that does not.

Until such a measurement exists, the manuscript's statements are confined to
the control plane, which is what §4 and §7.3 of the paper say.
