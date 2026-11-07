"""The first pipeline step: map, apply the field allow-list, diff against the ledger."""

from __future__ import annotations

from collections import Counter

from lc_core.allowlist import AllowList

from .gleif import Skip, lei_id, record_country, to_record
from .ledger import diff, volume_guard


def separate_collision(record: dict, owners: dict, counts: Counter) -> dict:
    """Keep two LEIs that share a registration as two records, flagged (Article 4.2).

    Sharing a registration number can mean a duplicate LEI or two funds under one
    number. A wrong merge costs more than a missed one, so the first LEI keeps the
    registry-based ID and any other gets its own, with a label for resolution.
    """
    owner = owners.setdefault(record["directoryId"], record["lei"])
    if owner == record["lei"]:
        return record
    counts["kept apart: shared registration"] += 1
    shared = record["directoryId"]
    record = {**record, "directoryId": lei_id(record_country(record), record["lei"])}
    record["labels"] = sorted(set(record.get("labels", [])) | {f"possible-duplicate:{shared}"})
    owners.setdefault(record["directoryId"], record["lei"])
    return record


def map_records(raws, counts: Counter, allowlist: AllowList):
    """Source records to allow-listed directory records. Skips are counted, never fatal."""
    for raw in raws:
        try:
            record = to_record(raw)
        except Skip as exc:
            counts[f"skipped: {exc}"] += 1
            continue
        except (KeyError, TypeError, AttributeError):
            counts["skipped: malformed"] += 1
            continue
        kept, dropped = allowlist.filter("entity", record)
        if dropped:
            counts["fields dropped"] += len(dropped)
        yield kept


def diff_records(records, current: dict, total: int, source_id: str, maker: str, counts: Counter):
    """Return (changes, new records by ID). ``current`` must hold every record these IDs touch.

    Raises VolumeGuardTripped, with nothing returned, if the batch looks wrong.
    """
    owners = {k: v["lei"] for k, v in current.items() if v.get("lei")}
    changes, updated = [], {}
    for record in records:
        record = separate_collision(record, owners, counts)
        old = updated.get(record["directoryId"]) or current.get(record["directoryId"], {"directoryId": record["directoryId"]})
        found = diff(old, record, source_id, maker)
        if found:
            changes.extend(found)
            updated[record["directoryId"]] = record
        counts["records"] += 1
    if total:
        volume_guard(len(updated), total)
    return changes, updated


def ingest(raws, current: dict, source_id: str, maker: str, allowlist: AllowList = None):
    """Map and diff in one go, with ``current`` as the whole directory. Returns (changes, counts)."""
    counts = Counter()
    records = map_records(raws, counts, allowlist or AllowList.load())
    changes, _ = diff_records(records, current, len(current), source_id, maker, counts)
    return changes, counts
