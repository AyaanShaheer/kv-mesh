"""Feasibility probe for the real-model (vLLM) arm of the evaluation.

Checks whether a small LLM can be served locally with automatic prefix
caching, and measures time-to-first-token for a repeated prompt so that the
cache effect can be quantified. Runs unattended and writes a JSON report.
"""

import json
import os
import sys
import time

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("VLLM_LOGGING_LEVEL", "WARNING")

# The CUDA *compiler* ships inside the virtualenv (nvidia/cu13), not in
# /usr/local/cuda. vLLM's kernel compilation needs it on PATH and in CUDA_HOME.
_CU13 = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".venv", "lib", "python3.12", "site-packages", "nvidia", "cu13",
)
if os.path.isdir(os.path.join(_CU13, "bin")):
    os.environ["CUDA_HOME"] = _CU13
    os.environ["PATH"] = os.path.join(_CU13, "bin") + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("CUDA_VERSION", "13.0")

MODEL = os.environ.get("KVMESH_TEST_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
PORT = int(os.environ.get("KVMESH_TEST_PORT", "8000"))
REPORT = os.environ.get("KVMESH_PROBE_REPORT", "/tmp/opencode/vllm_probe.json")


def main():
    report = {"model": MODEL, "ok": False, "stage": "start"}
    try:
        from vllm import LLM, SamplingParams
    except Exception as exc:  # pragma: no cover
        report["error"] = f"import vllm failed: {exc}"
        _write(report)
        return

    t0 = time.time()
    try:
        llm = LLM(
            model=MODEL,
            max_model_len=2048,
            gpu_memory_utilization=float(os.environ.get("KVMESH_GPU_UTIL", "0.70")),
            enable_prefix_caching=True,
            disable_log_stats=True,
            enforce_eager=os.environ.get("KVMESH_EAGER", "1") == "1",
        )
    except Exception as exc:
        report["error"] = f"engine start failed: {exc}"
        report["startup_s"] = time.time() - t0
        _write(report)
        return

    report["startup_s"] = time.time() - t0
    report["stage"] = "loaded"

    prompt = (
        "You are a helpful assistant for enterprise search. "
        "The following is internal policy document PX-4417. "
        "Summarize the retention policy in three bullet points."
    )
    sp = SamplingParams(max_tokens=64, temperature=0.0)

    # First call: cold, pays full prefill.
    t1 = time.time()
    out1 = llm.generate([prompt], sp)
    cold = time.time() - t1
    ntok = len(out1[0].outputs[0].token_ids)

    # Second call on the same engine: prefix cache should hit.
    t2 = time.time()
    out2 = llm.generate([prompt], sp)
    warm = time.time() - t2

    report.update(
        {
            "ok": True,
            "stage": "done",
            "cold_generate_s": cold,
            "warm_generate_s": warm,
            "cold_s": cold,
            "warm_s": warm,
            "speedup": (cold / warm) if warm > 0 else None,
            "output_tokens": ntok,
            "cold_text": out1[0].outputs[0].text[:400],
            "warm_text": out2[0].outputs[0].text[:400],
        }
    )
    _write(report)


def _write(report):
    with open(REPORT, "w") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "cold_text" and k != "warm_text"}, indent=2))


if __name__ == "__main__":
    main()
