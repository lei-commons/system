"""Builds the day's snapshot in GCP and copies it to the private snapshots bucket.

Part of the daily cycle, after ingestion and before the database load. Signing
and the GitHub release happen afterwards, from the files this writes.

Format v2 (docs/snapshots-v2.md): against the last published state, the day is
published as its changes, or as a full snapshot every ``full_every_days`` (and
whenever there is no usable previous state). Then old days are pruned.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from lc_core.control import SystemHalted, require_running
from lc_core.approvals import approved_record_sources
from lc_core.decisions import Recorder, failure, log_bucket_name, seal_through, yesterday
from lc_core.lease import Busy, lease
from lc_core.publishing import load as load_settings
from lc_core.snapshot_format import is_v2

from .__main__ import shard_reader
from .prune import prune
from .snapshot import STATE_FILES, StateReader, build_sharded, sha256_file, sources
from .upload import STATE_PREFIX, point_latest, upload_day, upload_state

COMPONENT = "job:build-snapshot"


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def previous_state(snapshots, data, today: str, work: Path):
    """(manifest, {kind: StateReader}, why): the last published v2 day and its state, or (None, None, why not)."""
    pointer = snapshots.blob("latest.json")
    if not pointer.exists():
        return None, None, "no snapshot yet"
    latest = json.loads(pointer.download_as_bytes())

    def manifest(date):
        blob = snapshots.blob(f"{date}/manifest.json")
        return json.loads(blob.download_as_bytes()) if blob.exists() else None

    previous = manifest(latest["date"])
    if previous is not None and previous["date"] == today:  # building today again: start from today's base
        base = previous.get("base") if is_v2(previous) else None
        previous = manifest(base["date"]) if base else None
        if previous is None:
            return None, None, "today was built before, with no usable base"
    if previous is None or not is_v2(previous):
        return None, None, "the previous snapshot is in the old format" if previous else "the previous manifest is missing"
    folder = f"{STATE_PREFIX}/{previous['date']}"
    meta_blob = data.blob(f"{folder}/state.json")
    if not meta_blob.exists():
        return None, None, f"no build state for {previous['date']}"
    meta = json.loads(meta_blob.download_as_bytes())
    if meta.get("snapshotRoot") != previous["snapshotRoot"]:
        return None, None, f"the build state for {previous['date']} doesn't match its manifest"
    target = work / "previous-state"
    target.mkdir(parents=True, exist_ok=True)
    readers = {}
    for kind, name in STATE_FILES.items():
        local = target / name
        blob = data.blob(f"{folder}/{name}")
        if not blob.exists():
            return None, None, f"the build state for {previous['date']} is incomplete"
        blob.download_to_filename(str(local))
        if sha256_file(local) != meta["files"].get(name):
            return None, None, f"the build state for {previous['date']} is damaged"
        readers[kind] = StateReader(local)
    return previous, readers, None


def combined(readers):
    """One relationship reader from several stores (today only GLEIF's)."""
    def read(n):
        out = {}
        for reader in readers:
            out.update(reader(n))
        return out
    return read


def publish(snapshots, data, date: str, readers: dict, relationships, settings, work: Path, record,
            decision_log=None, shards: int = None, now=None) -> dict:
    """Build the day against the last published state, upload it, point latest.json at it, then prune."""
    previous, state, why_not = previous_state(snapshots, data, date, work)
    if why_not:
        record({"event": "no-previous-state", "reason": why_not})
    extra = {"shards": shards} if shards else {}
    manifest = build_sharded(readers, work / "out", date, settings=settings.snapshots, previous=previous, state=state,
                             relationships=relationships, decision_log=decision_log, force_full=why_not, **extra)
    shutil.rmtree(work / "previous-state", ignore_errors=True)
    sent = upload_day(snapshots, work / "out", manifest)
    upload_state(data, work / "out", date)
    latest = point_latest(snapshots, manifest)
    changes = manifest["changes"]
    record({"event": "built", **{k: manifest[k] for k in ("date", "kind", "why", "anchor", "records", "recordsPerSource",
                                                           "inMoreThanOneSource", "relationships", "snapshotRoot")},
            "base": (manifest.get("base") or {}).get("date"), "parts": len(manifest["parts"]), "files": len(manifest["files"]),
            **{k: changes[k] for k in ("added", "changed", "deleted", "relationshipsAdded", "relationshipsChanged",
                                       "relationshipsDeleted")}, **sent})
    try:  # retention: a failure here never undoes the day; the next run tries again
        prune(snapshots, data, date, latest, settings.snapshots, now=now, log=record)
    except Exception as exc:
        record({"event": "prune-skipped", "error": type(exc).__name__, "reason": str(exc)[:200]})
    return manifest


def main() -> int:
    try:
        require_running("job:publish")  # manual mode stops publishing too
    except SystemHalted as exc:
        log({"event": "halted", "reason": str(exc)})
        return 0
    from google.cloud import storage

    client = storage.Client()
    data = client.bucket(os.environ["LC_DATA_BUCKET"])
    decisions = client.bucket(log_bucket_name(os.environ["LC_DATA_BUCKET"]))
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    work = Path(tempfile.mkdtemp(prefix="snapshot-"))
    record = Recorder(COMPONENT, log)
    snapshots = client.bucket(os.environ["LC_SNAPSHOT_BUCKET"])
    try:
        settings = load_settings()  # invalid settings stop the build before anything is written
        with lease(data, "build-snapshot"):
            seal = None
            try:  # every finished day goes into the chain, and its head into this manifest
                seals = seal_through(decisions, yesterday(date), str(work))
                seal = seals[-1] if seals else None
                record({"event": "log-sealed", "days": len(seals), **({k: seal[k] for k in ("day", "head", "events")} if seal else {})})
            except Exception as exc:  # the snapshot still goes out; the next run seals the day
                record({"event": "log-unsealed", "error": type(exc).__name__})
            # Only sources the owner has approved reach the snapshot, and so the database and site
            # (config/sources.yaml and the Data sources page). Unreadable approvals stop the build.
            approved = approved_record_sources()
            registered = sources()
            included = [s for s in registered if s["id"] in approved]
            record({"event": "sources-included", "included": ",".join(s["id"] for s in included),
                    "excluded": ",".join(s["id"] for s in registered if s["id"] not in approved)})
            relationship_stores = [s["relationships"] for s in included if s.get("relationships")]
            publish(snapshots, data, date, {s["id"]: shard_reader(data, s["prefix"]) for s in included},
                    combined([shard_reader(data, store) for store in relationship_stores]) if relationship_stores else None,
                    settings, work, record,
                    decision_log=seal)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})  # another run is building it
        record.write(decisions)
        return 0
    except Exception as exc:
        record(failure(exc))
        record.write(decisions)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)  # the disk is memory on Cloud Run
    record.write(decisions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
