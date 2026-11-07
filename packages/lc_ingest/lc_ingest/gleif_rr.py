"""GLEIF relationship records (RR-CDF): parents, fund managers, sub-funds, branches.

Both ends are LEIs, so these describe entities, never people. A relationship's
stored ID is derived from its two ends and its type, so the same relationship
always lands in the same place, and it travels in the ``directoryId`` key the
storage layer uses for every record.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile

from .gleif import Skip

MAX_PERIODS = 5
MAX_QUANTIFIERS = 3


def relationship_id(start: str, end: str, kind: str) -> str:
    return "REL" + hashlib.sha256(f"{start}|{end}|{kind}".encode()).hexdigest()[:29].upper()


def _period(row: dict):
    for i in range(1, MAX_PERIODS + 1):
        if row.get(f"Relationship.Period.{i}.periodType") == "RELATIONSHIP_PERIOD":
            return (row.get(f"Relationship.Period.{i}.startDate") or None,
                    row.get(f"Relationship.Period.{i}.endDate") or None)
    return None, None


def _percentage(row: dict):
    for i in range(1, MAX_QUANTIFIERS + 1):
        if row.get(f"Relationship.Quantifiers.{i}.QuantifierUnits") == "PERCENTAGE":
            amount = row.get(f"Relationship.Quantifiers.{i}.QuantifierAmount")
            if amount:
                value = float(amount)
                if 0 <= value <= 100:
                    return value
    return None


def to_relationship(row: dict) -> dict:
    """Map one RR row. Raises Skip for anything that isn't LEI to LEI."""
    if row.get("Relationship.StartNode.NodeIDType") != "LEI" or row.get("Relationship.EndNode.NodeIDType") != "LEI":
        raise Skip("not between two LEIs")
    start, end = row["Relationship.StartNode.NodeID"], row["Relationship.EndNode.NodeID"]
    kind = row["Relationship.RelationshipType"]
    if not (start and end and kind) or start == end:
        raise Skip("incomplete relationship")
    period_start, period_end = _period(row)
    record = {
        "directoryId": relationship_id(start, end, kind),
        "startNode": start,
        "endNode": end,
        "relationshipType": kind,
        "relationshipStatus": row.get("Relationship.RelationshipStatus") or None,
        "percentage": _percentage(row),
        "periodStart": (period_start or "")[:10] or None,
        "periodEnd": (period_end or "")[:10] or None,
    }
    return {k: v for k, v in record.items() if v is not None}


def read_zip(binary_file):
    """Yield RR rows from a golden copy zip, one at a time."""
    with zipfile.ZipFile(binary_file) as archive:
        names = [n for n in archive.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in the archive, found {len(names)}")
        with archive.open(names[0]) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
