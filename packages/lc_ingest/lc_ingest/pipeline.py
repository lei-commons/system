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


def ingest(raws, current: dict, source_id: str, maker: str, allowlist: AllowList = None):
    """Return (changes, counts). ``current`` maps directory ID to the record as it stands.

    Raises VolumeGuardTripped, with nothing returned, if the batch looks wrong.
    """
    allowlist = allowlist or AllowList.load()
    counts = Counter()
    changes = []
    changed_records = 0
    owners = {k: v["lei"] for k, v in current.items() if v.get("lei")}
    for raw in raws:
        try:
            record = separate_collision(to_record(raw), owners, counts)
        except Skip as exc:
            counts[f"skipped: {exc}"] += 1
            continue
        except (KeyError, TypeError, AttributeError):
            counts["skipped: malformed"] += 1
            continue
        kept, dropped = allowlist.filter("entity", record)
        if dropped:
            counts["fields dropped"] += len(dropped)
        found = diff(current.get(kept["directoryId"], {"directoryId": kept["directoryId"]}), kept, source_id, maker)
        if found:
            changed_records += 1
            changes.extend(found)
        counts["records"] += 1
    if current:
        volume_guard(changed_records, len(current))
    return changes, counts
