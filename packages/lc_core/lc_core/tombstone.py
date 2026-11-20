"""Tombstones: a directory ID that merged into another stays resolvable (owner decision 2026-11-20).

When a record moves to a new ID (the GLEIF re-key, lc_ingest.rekey), its old ID keeps a small record: the
name, jurisdiction and registration it was published with, and one label, ``merged:<new ID>``
(config/labels.yaml ``merged``: "Record merged into another; tombstone points to survivor"). The label
follows the ``possible-duplicate:<ID>`` convention, so the published columns don't change. The snapshot
publishes the tombstone as its own row with the status category ``merged``; it is never merged with
another record. It carries no LEI: the LEI belongs to the record it merged into, so an LEI always
resolves to one record.
"""

from __future__ import annotations

import json

PREFIX = "merged:"
CATEGORY = "merged"  # config/labels.yaml legal_status
KEPT = ("legalName", "legalJurisdiction", "registrationAuthority", "registrationNumber")


def _labels(record: dict) -> list:
    labels = record.get("labels") or []
    if isinstance(labels, str):  # a published row or a database column holds the list as JSON
        try:
            labels = json.loads(labels)
        except ValueError:
            return []
    return labels if isinstance(labels, list) else []


def merged_into(record: dict) -> str | None:
    """The ID this record merged into, or None for a live record."""
    for label in _labels(record):
        if isinstance(label, str) and label.startswith(PREFIX):
            return label[len(PREFIX):]
    return None


def is_tombstone(record: dict) -> bool:
    return merged_into(record) is not None


def make(record: dict, into: str) -> dict:
    """The tombstone left at ``record``'s ID when it moves to ``into``."""
    if not into or into == record["directoryId"]:
        raise ValueError("a tombstone points to another ID")
    out = {"directoryId": record["directoryId"], **{k: record[k] for k in KEPT if record.get(k)}}
    country = (record.get("legalAddress") or {}).get("country")
    if country:
        out["legalAddress"] = {"country": country}  # where it was, for the part's country; no street lines
    out["labels"] = [PREFIX + into]
    return out
