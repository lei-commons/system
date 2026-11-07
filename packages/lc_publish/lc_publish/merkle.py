"""Hashes anyone can recompute from the published files alone."""

from __future__ import annotations

import hashlib
import json


def leaf(record: dict) -> bytes:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(b"\x00" + canonical).digest()


def root(leaves: list) -> str:
    """Binary Merkle root over leaves in the given order. Empty input has a fixed root."""
    level = list(leaves) or [hashlib.sha256(b"").digest()]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return "sha256:" + level[0].hex()
