"""Real-model arm of the evaluation: end-to-end time-to-first-token.

This arm runs a real LLM (a small instruction-tuned model served by vLLM with
automatic prefix caching) and measures the quantity that the control-plane
experiments are ultimately about: how much time a request spends waiting for
its first token when the prefix is already resident in the engine's cache
versus when it is not.

The measurement is deliberately conservative. It reports, for a fixed prompt
set and a fixed model configuration:

* ``cold_ttft_ms``  -- TTFT of a prompt whose prefix is not in the cache;
* ``warm_ttft_ms``  -- TTFT of the same prompt immediately afterwards, whose
  prefix the engine has cached;
* the difference between them, which is the prefill work that a cache hit
  avoids.

It does not claim that the mesh recovers the whole difference: the mesh
contributes only by making the hit more likely.

The vLLM server is expected to be running; this script starts it if it is not.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from typing import Dict, List

import httpx

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV = os.path.join(REPO, ".venv", "bin", "python")
CU13 = os.path.join(REPO, ".venv", "lib", "python3.12", "site-packages", "nvidia", "cu13")
HF_CACHE = os.environ.get("KVMESH_HF_CACHE", "/tmp/opencode/hf_cache")

MODEL = os.environ.get("KVMESH_TEST_MODEL", "Qwen/Qwen2.5-0.5B-Instruct")
GPU_UTIL = os.environ.get("KVMESH_GPU_UTIL", "0.45")
MAX_MODEL_LEN = int(os.environ.get("KVMESH_MAX_LEN", "2048"))


def server_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["HF_HOME"] = HF_CACHE
    env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    env["VLLM_LOGGING_LEVEL"] = "WARNING"
    if os.path.isdir(os.path.join(CU13, "bin")):
        env["CUDA_HOME"] = CU13
        env["PATH"] = os.path.join(CU13, "bin") + os.pathsep + env.get("PATH", "")
    return env


def wait_for_server(port: int, timeout: float = 1800.0) -> bool:
    """Wait until the OpenAI-compatible server answers a models request.

    The engine can take a long time to initialise on this platform, and it
    accepts connections before it is ready, so the probe is retried with a
    generous per-request timeout.
    """
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/v1/models"
    last_error = None
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=30.0)
            if r.status_code == 200:
                return True
            last_error = f"status {r.status_code}"
        except Exception as exc:  # noqa: BLE001 - any transport error means "not ready"
            last_error = repr(exc)
        time.sleep(5)
    print(f"[realmodel] server not ready: {last_error}", flush=True)
    return False


def start_server(port: int, enable_prefix_caching: bool) -> subprocess.Popen:
    cmd = [
        VENV,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        MODEL,
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        "--max-model-len",
        str(MAX_MODEL_LEN),
        "--gpu-memory-utilization",
        GPU_UTIL,
        "--enforce-eager",
    ]
    if enable_prefix_caching:
        cmd.append("--enable-prefix-caching")
    log = open("/tmp/opencode/vllm_server.log", "w")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=server_env())


def ttft_ms(port: int, prompt: str, max_tokens: int = 32, timeout: float = 300.0) -> float:
    """Measure the time to the first streamed token, in milliseconds."""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
    }
    t0 = time.perf_counter()
    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", f"http://127.0.0.1:{port}/v1/completions", json=payload) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if choices and choices[0].get("text"):
                    return (time.perf_counter() - t0) * 1000.0
    return (time.perf_counter() - t0) * 1000.0


PROMPTS = [
    (
        "You are a support assistant for an enterprise document-management system. "
        "The following is the internal retention policy PX-4417, which is reproduced "
        "in full.\n\n"
        "RETENTION POLICY PX-4417\n"
        "Scope: all records created by the platform, including generated derivatives.\n"
        "Categories: (1) transient operational records, retained 30 days; "
        "(2) customer correspondence, retained 24 months; "
        "(3) audit logs, retained 7 years; "
        "(4) model-training corpora, retained until the retention review that follows "
        "the applicable statutory period.\n"
        "Deletion is by automated job, run weekly, and is irreversible.\n\n"
        "Question: summarise the retention periods in one sentence per category."
    ),
    (
        "You are a support assistant for an enterprise document-management system. "
        "The following is the internal access-control standard AC-2200, which is "
        "reproduced in full.\n\n"
        "ACCESS CONTROL STANDARD AC-2200\n"
        "Access is granted on the basis of role, reviewed quarterly, and revoked "
        "immediately on role change.\n\n"
        "Question: summarise the access-control standard in one sentence."
    ),
]


def measure(port: int, repeats: int = 5) -> Dict:
    results = {"model": MODEL, "prefix_caching": True, "prompts": []}
    for prompt in PROMPTS:
        # A cold prefix: the engine has not seen this prompt.
        cold = ttft_ms(port, prompt)
        # A warm prefix: the same prompt again, so the engine's prefix cache
        # holds its tokens.
        warm = [ttft_ms(port, prompt) for _ in range(repeats)]
        results["prompts"].append(
            {
                "prompt_chars": len(prompt),
                "cold_ttft_ms": cold,
                "warm_ttft_ms_mean": statistics.fmean(warm),
                "warm_ttft_ms_median": statistics.median(warm),
                "warm_ttft_ms_min": min(warm),
                "warm_ttft_ms_max": max(warm),
                "warm_ttft_ms_all": warm,
                "saving_ms": cold - statistics.fmean(warm),
            }
        )
    savings = [p["saving_ms"] for p in results["prompts"]]
    results["mean_saving_ms"] = statistics.fmean(savings)
    results["mean_cold_ttft_ms"] = statistics.fmean([p["cold_ttft_ms"] for p in results["prompts"]])
    results["mean_warm_ttft_ms"] = statistics.fmean(
        [p["warm_ttft_ms_mean"] for p in results["prompts"]]
    )
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--out", default="experiments/results/realmodel/ttft.json")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--no-prefix-caching", action="store_true")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    proc = None
    try:
        if not wait_for_server(args.port, timeout=5):
            print("[realmodel] starting vLLM server ...", flush=True)
            t0 = time.time()
            proc = start_server(args.port, enable_prefix_caching=not args.no_prefix_caching)
            if not wait_for_server(args.port, timeout=2400):
                raise RuntimeError("vLLM server did not become ready")
            print(f"[realmodel] server ready in {time.time() - t0:.1f}s", flush=True)

        report = measure(args.port, repeats=args.repeats)
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2)
        print(json.dumps(report, indent=2))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=60)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    main()
