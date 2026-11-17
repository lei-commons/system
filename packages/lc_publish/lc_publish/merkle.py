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


class StreamingRoot:
    """The same root as ``root(leaves)``, fed one leaf at a time, in memory that grows with log(n).

    Each level holds at most one node waiting for its right-hand partner. At the end the
    tail is closed as ``root`` does: an odd node at any level is paired with itself.
    """

    def __init__(self):
        self._pending = []  # per level: a complete left node, or None
        self.count = 0

    def add(self, leaf_hash: bytes) -> None:
        carry, level = leaf_hash, 0
        while level < len(self._pending) and self._pending[level] is not None:
            carry = hashlib.sha256(b"\x01" + self._pending[level] + carry).digest()
            self._pending[level] = None
            level += 1
        if level == len(self._pending):
            self._pending.append(None)
        self._pending[level] = carry
        self.count += 1

    def digest(self) -> bytes:
        if not self.count:
            return hashlib.sha256(b"").digest()
        top = max(i for i, node in enumerate(self._pending) if node is not None)
        carry = None
        for level, node in enumerate(self._pending):
            if node is not None and carry is not None:
                carry = hashlib.sha256(b"\x01" + node + carry).digest()
            elif node is not None:
                if level == top:
                    return node
                carry = hashlib.sha256(b"\x01" + node + node).digest()
            elif carry is not None:
                carry = hashlib.sha256(b"\x01" + carry + carry).digest()
            if level >= top and carry is not None and not any(self._pending[level + 1:]):
                return carry
        return carry

    def hexdigest(self) -> str:
        return "sha256:" + self.digest().hex()
