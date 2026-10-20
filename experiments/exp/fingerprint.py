"""Prompt fingerprinting.

The fingerprint must agree with the Go gateway (``gateway/router/router.go``)
and the Python worker (``worker/main.py``), which both take the first 16
hexadecimal digits of the MD5 digest of the UTF-8 prompt and parse them as an
unsigned 64-bit integer.

The cross-language agreement is asserted by ``tests/test_fingerprint_agreement.py``,
which compares this implementation against the compiled Go helper.
"""

from __future__ import annotations

import hashlib


def hash_prompt(prompt: str) -> int:
    """Return the 64-bit fingerprint of ``prompt``."""
    return int(hashlib.md5(prompt.encode("utf-8")).hexdigest()[:16], 16)


def prompt_for_key(key: str) -> str:
    """Map a workload key to a concrete prompt string.

    Workloads index prompts by short opaque keys. The key is embedded in a
    longer, human-readable prompt so that the fingerprint is computed over a
    non-trivial byte string (rather than over the bare key), which keeps the
    hashing cost representative of the real path.
    """
    return f"SYSTEM CONTEXT v1 :: session={key} :: request payload follows <<{key}>>"
