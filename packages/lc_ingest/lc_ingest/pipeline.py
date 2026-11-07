"""The first pipeline step: map, apply the field allow-list, diff against the ledger."""

from __future__ import annotations

from collections import Counter

from lc_core.allowlist import AllowList

from .gleif import Skip, to_record
from .ledger import diff, volume_guard


def ingest(raws, current: dict, source_id: str, maker: str, allowlist: AllowList = None):
    """Return (changes, counts). ``current`` maps directory ID to the record as it stands.

    Raises VolumeGuardTripped, with nothing returned, if the batch looks wrong.
    """
    allowlist = allowlist or AllowList.load()
    counts = Counter()
    changes = []
    changed_records = 0
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
        found = diff(current.get(kept["directoryId"], {"directoryId": kept["directoryId"]}), kept, source_id, maker)
        if found:
            changed_records += 1
            changes.extend(found)
        counts["records"] += 1
    if current:
        volume_guard(changed_records, len(current))
    return changes, counts
