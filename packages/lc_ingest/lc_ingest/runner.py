"""One ingestion run, the same for every source, in bounded memory.

1. Stop at once unless the system is running.
2. Find the right file through the gateway. A file already processed is skipped.
3. Download it through the gateway and keep the raw file.
4. Partition: map and filter every record, writing each to its shard's file on disk.
5. Diff: one shard at a time, compare with the stored shard and stage the result.
   Only one shard's records are ever in memory, so file size doesn't matter.
6. Guard: if an implausible share of existing records changed, stop with nothing written.
7. Write the changes, then the shards, then the manifest. A crash before the
   manifest leaves the file unprocessed, and the rerun finds the shards already
   updated, so nothing is applied twice.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from collections import Counter, defaultdict

from lc_core.allowlist import AllowList
from lc_core.control import SystemHalted
from lc_core.fetch import Denied

from .ledger import VolumeGuardTripped, diff, volume_guard
from .pipeline import map_records, separate_collision
from .store import decode_records, encode_records, shard_of

KEEP_PROCESSED = 100


class _Partitions:
    """Per-shard gzipped JSON-lines files, opened as records arrive."""

    def __init__(self, directory: str):
        self._dir, self._files = directory, {}

    def add(self, record: dict) -> None:
        n = shard_of(record["directoryId"])
        if n not in self._files:
            self._files[n] = gzip.open(os.path.join(self._dir, f"{n:03d}.jsonl.gz"), "wt", encoding="utf-8")
        self._files[n].write(json.dumps(record, sort_keys=True) + "\n")

    def close(self) -> list:
        for f in self._files.values():
            f.close()
        return sorted(self._files)

    def read(self, n: int):
        with gzip.open(os.path.join(self._dir, f"{n:03d}.jsonl.gz"), "rt", encoding="utf-8") as f:
            for line in f:
                yield json.loads(line)


def run_ingest(*, gateway, store, gate, find, read, mapper, source: str, maker: str, allowlist: AllowList, log,
               kind: str = "entity", seen: bool = False) -> str:
    """Return the outcome: halted, denied, already-done, held or done.

    ``find(gateway, manifest)`` returns (key, url, filename). ``key`` identifies
    the publication, so the same one is never processed twice. With ``seen``,
    the directory IDs in the file are kept under the key once the run is
    done, so a later step can tell which stored records a whole publication
    no longer lists.
    """
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "source": source, "reason": str(exc)})
        return "halted"

    manifest = store.read_manifest()
    try:
        key, url, filename = find(gateway, manifest)
    except Denied as exc:
        log({"event": "denied", "rule": exc.rule, "reason": exc.reason})
        return "denied"
    if key in manifest["processed"]:
        log({"event": "already-done", "publish": key})
        return "already-done"

    counts = Counter()
    source_id = f"src:{source}-{filename}"
    with tempfile.TemporaryDirectory(prefix="ingest-") as work:
        for sub in ("in", "stage"):
            os.mkdir(os.path.join(work, sub))
        download = os.path.join(work, filename)
        with open(download, "wb") as f:
            try:
                size = gateway.download(url, f)
            except Denied as exc:
                log({"event": "denied", "rule": exc.rule, "reason": exc.reason})
                return "denied"
        store.keep_source(filename, download)

        partitions = _Partitions(os.path.join(work, "in"))
        with open(download, "rb") as f:
            for record in map_records(read(f), counts, allowlist, mapper=mapper, kind=kind):
                partitions.add(record)
        shards = partitions.close()
        os.remove(download)  # the disk is memory on Cloud Run

        changes_path = os.path.join(work, "changes.jsonl.gz")
        staged, moved = {}, defaultdict(list)
        changed_existing = new_records = changes_written = 0

        def load(n):
            path = staged.get(n)
            if path:
                with open(path, "rb") as f:
                    return decode_records(f.read())
            return store.read_shard(n)

        seen_ids = set()

        def process(n, records, out):
            nonlocal changed_existing, new_records, changes_written
            current = load(n)
            owners = {k: v["lei"] for k, v in current.items() if v.get("lei")}
            updated = {}
            for record in records:
                record = separate_collision(record, owners, counts)
                target = shard_of(record["directoryId"])
                if target != n:
                    moved[target].append(record)  # kept apart under its own ID, in another shard
                    continue
                directory_id = record["directoryId"]
                if seen:
                    seen_ids.add(directory_id)
                old = updated.get(directory_id) or current.get(directory_id, {"directoryId": directory_id})
                found = diff(old, record, source_id, maker)
                counts["records"] += 1
                if not found:
                    continue
                for change in found:
                    out.write(json.dumps(change, sort_keys=True) + "\n")
                changes_written += len(found)
                if directory_id not in updated:
                    if directory_id in current:
                        changed_existing += 1
                    else:
                        new_records += 1
                updated[directory_id] = record
            if updated:
                path = os.path.join(work, "stage", f"{n:03d}.jsonl.gz")
                with open(path, "wb") as f:
                    f.write(encode_records({**current, **updated}))
                staged[n] = path

        with gzip.open(changes_path, "wt", encoding="utf-8") as out:
            for n in shards:
                process(n, partitions.read(n), out)
            for n in sorted(moved):
                process(n, moved.pop(n), out)

        try:
            volume_guard(changed_existing, manifest["records"])
        except VolumeGuardTripped as exc:
            log({"event": "held", "publish": key, "reason": str(exc)})
            return "held"

        store.write_changes_file(filename.rsplit(".", 1)[0], changes_path)
        for n in sorted(staged):
            store.write_shard_file(n, staged[n])
        manifest["records"] += new_records
        manifest["processed"] = (manifest["processed"] + [key])[-KEEP_PROCESSED:]
        manifest["lastKey"] = key
        store.write_manifest(manifest)
        if seen:
            store.keep_seen(key, seen_ids)

    log({"event": "done", "source": source, "publish": key, "bytes": size, "changes": changes_written,
         "records_changed": changed_existing + new_records, "existing_changed": changed_existing,
         "new_records": new_records, "total": manifest["records"], **dict(counts)})
    return "done"
