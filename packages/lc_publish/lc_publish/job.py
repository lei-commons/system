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

from .__main__ import shard_reader
from .snapshot import SOURCES, build_sharded
from .upload import upload


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
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    work = Path(tempfile.mkdtemp(prefix="snapshot-"))
    try:
        manifest = build_sharded({s["id"]: shard_reader(data, s["prefix"]) for s in SOURCES}, work, date,
                                 relationships=shard_reader(data, "gleif-rr"))
        latest = upload(client.bucket(os.environ["LC_SNAPSHOT_BUCKET"]), work)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # the disk is memory on Cloud Run
    log({"event": "built", **{k: manifest[k] for k in ("date", "records", "recordsPerSource", "inMoreThanOneSource",
                                                        "relationships", "snapshotRoot")}, "files": len(latest["files"])})
    return 0


if __name__ == "__main__":
    sys.exit(main())
