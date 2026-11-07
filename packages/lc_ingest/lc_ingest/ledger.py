"""Ledger changes: one JSON line per changed field.

The old value is recorded only as a hash, so a change line never repeats a
value that may later be redacted, and the chain can still be checked.
"""

from __future__ import annotations

import hashlib
import json

CHECKS = ("schema", "invariants", "volume-guard")


class VolumeGuardTripped(Exception):
    """Too much changed at once. The batch is held, not applied."""


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def leaf_hash(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def diff(old: dict, new: dict, source_id: str, maker: str, tier: int = 0) -> list:
    """Return the changes that turn ``old`` into ``new``. Same input, same output."""
    directory_id = new.get("directoryId") or old.get("directoryId")
    changes = []
    for path in sorted(set(old) | set(new)):
        if path == "directoryId" or old.get(path) == new.get(path):
            continue
        changes.append({
            "directoryId": directory_id,
            "fieldPath": path,
            "oldLeafHash": leaf_hash(old[path]) if path in old else None,
            "newValue": new.get(path),
            "sourceIds": [source_id],
            "tier": tier,
            "maker": maker,
            "checks": list(CHECKS),
        })
    return changes


def volume_guard(changed_records: int, total_records: int, max_fraction: float = 0.1, minimum: int = 100) -> None:
    """Hold a batch that changes an implausible share of the records.

    A broken parser or a truncated source file looks like a mass change, so it
    is stopped here rather than rewriting the directory. A first load has no
    baseline, and small batches are too noisy to judge.
    """
    if total_records < minimum:
        return
    if changed_records > total_records * max_fraction:
        raise VolumeGuardTripped(f"{changed_records} of {total_records} records changed")


def apply(state: dict, changes: list) -> dict:
    """Replay changes onto a record. The ledger must be able to rebuild any day."""
    result = dict(state)
    for change in changes:
        path = change["fieldPath"]
        expected = change["oldLeafHash"]
        actual = leaf_hash(result[path]) if path in result else None
        if expected != actual:
            raise ValueError(f"{path}: ledger chain broken")
        if change["newValue"] is None:
            result.pop(path, None)
        else:
            result[path] = change["newValue"]
    return result
