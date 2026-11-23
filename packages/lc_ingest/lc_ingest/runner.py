"""One ingestion run, the same for every source, in bounded memory.

1. Stop at once unless the system is running.
2. Find the right file through the gateway. A file already processed is skipped.
3. Download it through the gateway. The file itself is never kept: only its SHA-256 and size, in the
   manifest and the done event (owner, 2026-11-23: no people's data is stored, and some sources' files hold
   rows about people that are dropped at step 4).
4. Partition: map and filter every record, writing each to its shard's file on disk.
5. Diff: one shard at a time, compare with the stored shard and stage the result.
   Only one shard's records are ever in memory, so file size doesn't matter.
6. Guard: if an implausible share of existing records changed, stop with nothing written,
   unless the owner approved a one-time mapping upgrade for exactly this source and
   publication (config/sources.yaml volume_guard.mapping_upgrades); that is logged.
7. Write the changes, then the shards, then the manifest. A crash before the
   manifest leaves the file unprocessed, and the rerun finds the shards already
   updated, so nothing is applied twice.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import tempfile
import time
from collections import Counter, defaultdict

from lc_core import guard
from lc_core.allowlist import AllowList
from lc_core.control import SystemHalted
from lc_core.fetch import Denied, FetchError

from . import filecheck
from .ledger import VolumeGuardTripped, diff, volume_guard
from .pipeline import map_records, separate_collision
from .store import decode_records, encode_records, shard_of

KEEP_PROCESSED = 100
DOWNLOAD_ATTEMPTS = 3
# A bad file (cut short, an error page, corrupt), a broken transfer, or a file that can't be read: fetch it again.
RETRYABLE = filecheck.FILE_ERRORS + (FetchError, csv.Error, json.JSONDecodeError, ValueError, OSError)


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


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def run_ingest(*, gateway, store, gate, find, read, mapper, source: str, maker: str, allowlist: AllowList, log,
               kind: str = "entity", seen: bool = False, prepare=None, fetch=None, attempts: int = DOWNLOAD_ATTEMPTS,
               check_as: str = None, sleep=time.sleep, backoff: float = 30.0, guard_settings=None,
               quality=None) -> str:
    """Return the outcome: halted, denied, already-done, held or done.

    ``find(gateway, manifest)`` returns (key, url, filename). ``key`` identifies
    the publication, so the same one is never processed twice. With ``seen``,
    the directory IDs in the file are kept under the key once the run is
    done, so a later step can tell which stored records a whole publication
    no longer lists.

    ``prepare(n, records, current, counts)``, if given, sees each shard's
    mapped records with the shard's stored records, just before they are
    compared, and returns the records to keep. Brazil uses it to join head
    offices to their company.

    ``fetch(gateway, url, file)``, if given, writes the publication into the
    open file instead of one download of ``url``. Singapore uses it to join 27
    files into one.

    ``guard_settings`` (lc_core.guard.GuardSettings) defaults to config/sources.yaml's
    ``volume_guard``. It is read before anything is fetched, so a bad setting stops the run.

    ``quality``, for a source on trial (docs/source-trials.md): a function returning a fresh
    lc_ingest.quality.Quality. The run then only measures: every mapped record of the attempt that succeeds is
    measured, the ``quality`` event is logged (counts only) and ``trial-report.json`` (with a sample) written to
    ``store``; nothing else is kept (no records, no changes, no copy of the file), and ``store``'s manifest
    notes the key as measured. The caller gives a trial its own small store and a key that names the code.
    """
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "source": source, "reason": str(exc)})
        return "halted"

    purge = getattr(store, "purge_sources", None)  # downloads kept by earlier code: deleted on the next run
    purged = purge() if purge else 0
    if purged:
        log({"event": "sources-purged", "source": source, "objects": purged})

    settings = guard_settings or guard.load()
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
        for attempt in range(1, attempts + 1):
            # Download, check and read are one step: a file cut short, an error page or a corrupt archive
            # is fetched again, every time logged, and nothing from it is kept (lc_ingest.filecheck).
            counts.clear()
            measure = quality() if quality else None
            partitions = _Partitions(os.path.join(work, f"in-{attempt}"))
            os.makedirs(os.path.join(work, f"in-{attempt}"), exist_ok=True)
            try:
                with open(download, "wb") as f:
                    size = (fetch or (lambda g, u, out: g.download(u, out)))(gateway, url, f)
                filecheck.check(download, check_as or filecheck.kind_for(filename))
                with open(download, "rb") as f:
                    for record in map_records(read(f), counts, allowlist, mapper=mapper, kind=kind):
                        if measure is not None:
                            measure.add(record)  # a trial measures and keeps nothing
                        else:
                            partitions.add(record)
                shards = partitions.close()
                break
            except Denied as exc:
                partitions.close()
                log({"event": "denied", "rule": exc.rule, "reason": exc.reason})
                return "denied"
            except RETRYABLE as exc:
                partitions.close()
                reason = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160] if str(exc) else ''}"
                if attempt == attempts:
                    log({"event": "download-failed", "source": source, "publish": key, "attempts": attempts, "reason": reason})
                    raise filecheck.BadFile(f"{filename}: still bad after {attempts} downloads ({reason})") from exc
                log({"event": "bad-download", "source": source, "publish": key, "attempt": attempt, "reason": reason})
                sleep(backoff * attempt)
        if attempt > 1:
            log({"event": "download-recovered", "source": source, "publish": key, "attempt": attempt})
        if measure is not None:  # a trial: the report is all it keeps
            os.remove(download)
            return _measured(store, manifest, key, source, measure, counts, size, log)
        digest = _sha256(download)  # evidence of exactly which file was read; the file itself is never kept
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
            if prepare is not None:
                records = prepare(n, records, current, counts)
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
            volume_guard(changed_existing, manifest["records"], settings)
        except VolumeGuardTripped as exc:
            upgrade = settings.upgrade_for(source, key)
            if upgrade is None:
                log({"event": "held", "publish": key, "reason": str(exc)})
                return "held"
            # A one-time mapping upgrade the owner approved for exactly this source and publication.
            log({"event": "mapping-upgrade", "source": source, "publish": key, "changed": changed_existing,
                 "total": manifest["records"], "max_fraction": settings.max_fraction, "reason": upgrade.reason,
                 "approved_by": upgrade.approved_by, "approved_on": upgrade.approved_on})

        store.write_changes_file(filename.rsplit(".", 1)[0], changes_path)
        for n in sorted(staged):
            store.write_shard_file(n, staged[n])
        manifest["records"] += new_records
        manifest["processed"] = (manifest["processed"] + [key])[-KEEP_PROCESSED:]
        manifest["lastKey"] = key
        manifest.setdefault("files", {})[key] = {"sha256": digest, "bytes": size}
        manifest["files"] = dict(list(manifest["files"].items())[-KEEP_PROCESSED:])
        store.write_manifest(manifest)
        if seen:
            store.keep_seen(key, seen_ids)

    log({"event": "done", "source": source, "publish": key, "bytes": size, "sha256": digest, "changes": changes_written,
         "records_changed": changed_existing + new_records, "existing_changed": changed_existing,
         "new_records": new_records, "total": manifest["records"], **dict(counts)})
    return "done"


def _measured(store, manifest, key, source, measure, counts, size, log) -> str:
    """A trial's end: the quality event (counts only), the report with its sample, and the key noted as measured."""
    log({"event": "quality", "source": source, "publish": key, "bytes": size, "quality": measure.summary(counts)})
    report = measure.report(counts, publish=key)
    store.write_json(f"trial-reports/{key.replace(' ', '_').replace('/', '-').replace('@', '')}.json", report)  # one per file
    store.write_json("trial-report.json", report)  # the latest
    manifest["processed"] = (manifest["processed"] + [key])[-KEEP_PROCESSED:]
    manifest["lastKey"] = key
    store.write_manifest(manifest)
    log({"event": "measured", "source": source, "publish": key, "records": measure.records})
    return "done"
