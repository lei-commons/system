"""Builds the day's snapshot in GCP and copies it to the private snapshots bucket.

Part of the daily cycle, after ingestion and before the database load. Signing
and the GitHub release happen afterwards, from the files this writes.
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
from lc_core.decisions import Recorder, log_bucket_name, seal_through, yesterday
from lc_core.lease import Busy, lease

from .__main__ import shard_reader
from .snapshot import SOURCES, build_sharded
from .upload import upload

COMPONENT = "job:build-snapshot"


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


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
    try:
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
            included = [s for s in SOURCES if s["id"] in approved]
            record({"event": "sources-included", "included": ",".join(s["id"] for s in included),
                    "excluded": ",".join(s["id"] for s in SOURCES if s["id"] not in approved)})
            manifest = build_sharded({s["id"]: shard_reader(data, s["prefix"]) for s in included}, work, date,
                                     relationships=shard_reader(data, "gleif-rr") if "gleif" in approved else None,
                                     decision_log=seal)
            latest = upload(client.bucket(os.environ["LC_SNAPSHOT_BUCKET"]), work)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})  # another run is building it
        record.write(decisions)
        return 0
    except Exception as exc:
        record({"event": "failed", "error": type(exc).__name__})
        record.write(decisions)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)  # the disk is memory on Cloud Run
    record({"event": "built", **{k: manifest[k] for k in ("date", "records", "recordsPerSource", "inMoreThanOneSource", "relationships",
                                                           "snapshotRoot")}, "files": len(latest["files"])})
    record.write(decisions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
