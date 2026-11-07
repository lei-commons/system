"""One ingestion run, the same for every source.

1. Stop at once unless the system is running.
2. Find the latest file through the gateway. A file already processed is skipped.
3. Download it through the gateway and keep the raw file.
4. Map, filter and diff, loading only the shards the run touches.
5. Write the changes, then the records, then the manifest. A crash before the
   manifest leaves the file unprocessed, and the rerun finds the shards already
   updated, so nothing is applied twice.
"""

from __future__ import annotations

import tempfile
from collections import Counter

from lc_core.allowlist import AllowList
from lc_core.control import SystemHalted
from lc_core.fetch import Denied

from .ledger import VolumeGuardTripped
from .pipeline import diff_records, map_records
from .store import shard_of

KEEP_PROCESSED = 100


def run_ingest(*, gateway, store, gate, find, read, mapper, source: str, maker: str, allowlist: AllowList, log) -> str:
    """Return the outcome: halted, denied, already-done, held or done.

    ``find(gateway)`` returns (key, url, filename): ``key`` identifies the
    publication, so the same one is never processed twice.
    """
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "reason": str(exc)})
        return "halted"

    manifest = store.read_manifest()
    try:
        key, url, filename = find(gateway)
    except Denied as exc:
        log({"event": "denied", "rule": exc.rule, "reason": exc.reason})
        return "denied"
    if key in manifest["processed"]:
        log({"event": "already-done", "publish": key})
        return "already-done"

    counts = Counter()
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        try:
            size = gateway.download(url, tmp)
        except Denied as exc:
            log({"event": "denied", "rule": exc.rule, "reason": exc.reason})
            return "denied"
        tmp.flush()
        store.keep_source(filename, tmp.name)
        tmp.seek(0)
        records = list(map_records(read(tmp), counts, allowlist, mapper=mapper))

    loaded = {n: store.read_shard(n) for n in {shard_of(r["directoryId"]) for r in records}}
    current = {k: v for shard in loaded.values() for k, v in shard.items()}
    try:
        changes, updated = diff_records(records, current, manifest["records"], f"src:{source}-{filename}", maker, counts)
    except VolumeGuardTripped as exc:
        log({"event": "held", "publish": key, "reason": str(exc)})
        return "held"

    # Records kept apart from a shared registration may land in shards not loaded yet.
    for directory_id in updated:
        n = shard_of(directory_id)
        if n not in loaded:
            loaded[n] = store.read_shard(n)
    new_records = 0
    for directory_id, record in updated.items():
        shard = loaded[shard_of(directory_id)]
        new_records += directory_id not in shard
        shard[directory_id] = record

    store.write_changes(filename.rsplit(".", 1)[0], changes)
    for n in sorted({shard_of(k) for k in updated}):
        store.write_shard(n, loaded[n])
    manifest["records"] += new_records
    manifest["processed"] = (manifest["processed"] + [key])[-KEEP_PROCESSED:]
    store.write_manifest(manifest)
    log({"event": "done", "publish": key, "bytes": size, "changes": len(changes), "records_changed": len(updated),
         "new_records": new_records, "total": manifest["records"], **dict(counts)})
    return "done"
