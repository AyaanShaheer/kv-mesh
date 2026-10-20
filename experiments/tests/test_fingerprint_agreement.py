"""Cross-language and cross-component consistency tests.

The KV-Mesh prototype computes the prompt fingerprint in three places: the Go
gateway (``gateway/router/router.go``), the Python worker
(``worker/main.py``) and the experiment harness
(``experiments/exp/fingerprint.py``). All three must agree exactly, because a
disagreement would make the gateway route on a key the worker never stores.

These tests are run as part of the reproducibility workflow described in
Appendix B of the manuscript.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "experiments"))
sys.path.insert(0, os.path.join(REPO, "worker"))

from exp.fingerprint import hash_prompt, prompt_for_key  # noqa: E402


PROMPTS = [
    "",
    "a",
    "Explain quantum computing in simple terms",
    "Explain quantum computing in simple terms.",
    "SYSTEM CONTEXT v1 :: session=k00042 :: request payload follows <<k00042>>",
    "unicode: \u00e9\u00e8\u00ea \u4f60\u597d \U0001f600",
    "tab\tand trailing spaces   ",
    "x" * 4096,
]

# The line-oriented probe protocol cannot carry a prompt that contains a
# newline, so that prompt is checked by the in-process tests only.
LINE_ORIENTED_PROMPTS = [p for p in PROMPTS if "\n" not in p]


def test_reference_hash_matches_the_documented_definition():
    """The fingerprint is the first 16 hex digits of the MD5 digest."""
    for prompt in PROMPTS:
        expected = int(hashlib.md5(prompt.encode("utf-8")).hexdigest()[:16], 16)
        assert hash_prompt(prompt) == expected


def test_worker_helper_agrees_with_the_harness():
    """The worker's own hash helper agrees with the harness."""
    import main as worker_main

    for prompt in PROMPTS:
        assert worker_main.hash_prompt(prompt) == hash_prompt(prompt)


def test_fingerprint_is_stable_and_collision_free_on_the_workload_keys():
    """Distinct workload keys produce distinct fingerprints."""
    seen = {}
    for i in range(5000):
        key = f"k{i:05d}"
        digest = hash_prompt(prompt_for_key(key))
        assert digest not in seen, f"collision between {key} and {seen.get(digest)}"
        seen[digest] = key


@pytest.mark.skipif(
    subprocess.run(["which", "go"], capture_output=True).returncode != 0,
    reason="the Go toolchain is required to cross-check the gateway implementation",
)
def test_gateway_implementation_agrees_with_the_harness():
    """The Go gateway hashes exactly like the Python harness."""
    binary = os.path.join(REPO, "gateway", "bin", "hashprobe")
    if not os.path.exists(binary):
        subprocess.run(
            ["go", "build", "-o", binary, "./cmd/hashprobe"],
            cwd=os.path.join(REPO, "gateway"),
            check=True,
        )
    payload = "\n".join(LINE_ORIENTED_PROMPTS) + "\n"
    out = subprocess.run(
        [binary], input=payload.encode("utf-8"), capture_output=True, check=True
    )
    values = out.stdout.decode().strip().split("\n")
    assert len(values) == len(LINE_ORIENTED_PROMPTS)
    for prompt, value in zip(LINE_ORIENTED_PROMPTS, values):
        assert int(value, 16) == hash_prompt(prompt), (
            f"the Go and Python fingerprints disagree for {prompt!r}"
        )
