"""Loads the latest snapshot into the serving database.

Skips a snapshot already loaded, so it is safe to run as often as wanted.
Stops at once unless the system is running. The swap is atomic, so the site
keeps serving the previous snapshot until the new one is complete.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

from lc_core.control import SystemHalted, require_running
from lc_core.decisions import Recorder

COMPONENT = "job:load-db"


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def run(*, bucket, connect, readers, gate=lambda: require_running(COMPONENT), log=log) -> str:
    """Return the outcome: halted, no-snapshot, already-loaded or loaded."""
    from lc_directory.load import already_loaded, load

    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "reason": str(exc)})
        return "halted"
    pointer = bucket.blob("latest.json")
    if not pointer.exists():
        log({"event": "no-snapshot"})
        return "no-snapshot"
    latest = json.loads(pointer.download_as_bytes())
    with connect() as conn:
        # One load at a time. Postgres releases this lock by itself if the loader dies.
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext('lc-load-db'))").fetchone()[0]:
            log({"event": "busy", "reason": "another load is running"})
            return "busy"
        if already_loaded(conn, latest["snapshotRoot"]):
            log({"event": "already-loaded", "date": latest["date"]})
            return "already-loaded"
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="load-") as work:
            paths = {"files": [], "relationships": []}
            for group in paths:
                for name in latest.get(group, []):
                    local = os.path.join(work, name)
                    bucket.blob(f"{latest['prefix']}/{name}").download_to_filename(local)
                    paths[group].append(local)
            counts = load(conn, paths["files"], paths["relationships"], latest, readers=readers)
    log({"event": "loaded", "date": latest["date"], "seconds": round(time.time() - started), **counts})
    return "loaded"


def main() -> int:
    import psycopg
    from google.cloud import secretmanager, storage

    project = os.environ["GCP_PROJECT_ID"]
    password = secretmanager.SecretManagerServiceClient().access_secret_version(
        name=f"projects/{project}/secrets/{os.environ.get('LC_DB_SECRET', 'db-admin-password')}/versions/latest"
    ).payload.data.decode()

    def connect():
        return psycopg.connect(host=os.environ["LC_DB_HOST"], dbname=os.environ.get("LC_DB_NAME", "lei_commons"),
                               user=os.environ.get("LC_DB_ADMIN", "lc_admin"), password=password, sslmode="require")

    client = storage.Client()
    record = Recorder(COMPONENT, log)  # every decision goes to the decision log too
    try:
        outcome = run(bucket=client.bucket(os.environ["LC_SNAPSHOT_BUCKET"]), connect=connect,
                      readers=[os.environ["LC_SITE_DB_USER"]], log=record)
    except Exception as exc:
        record({"event": "failed", "error": type(exc).__name__})
        raise
    finally:  # the log bucket is <project>-decision-log
        record.write(client.bucket(os.environ.get("LC_DECISION_LOG_BUCKET", f"{project}-decision-log")))
    return 0 if outcome in ("loaded", "already-loaded", "halted", "no-snapshot", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
