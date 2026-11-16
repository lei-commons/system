"""Loads the latest snapshot into the serving database.

Skips a snapshot already loaded, so it is safe to run as often as wanted.
Stops at once unless the system is running. The site keeps serving the
previous snapshot until the new one is complete.

Format v2 (docs/snapshots-v2.md): if the database holds a day in the latest
day's chain, it applies the days' changes incrementally, in one checked
transaction; otherwise (or if a check fails, with ``fallback_to_full``) it loads
the latest full snapshot plus the changes since, blue/green. Snapshots in the
old format (v1) are loaded in full, as before.
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
OK = ("loaded", "already-loaded", "halted", "no-snapshot", "busy")


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


class DamagedFile(Exception):
    """A downloaded file doesn't match the SHA-256 its manifest gives."""


def _sha256(path):
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _fetcher(bucket, work):
    """Download files one at a time, removing each after use: the disk is memory on Cloud Run,
    and a 24-million-record snapshot is several GB (the 2026-11-10 load ran out of memory).
    Each item is an object name, or a manifest entry whose SHA-256 is checked before use."""
    def fetched(items):
        for item in items:
            obj = item["object"] if isinstance(item, dict) else item
            local = os.path.join(work, obj.replace("/", "_"))
            bucket.blob(obj).download_to_filename(local)
            if isinstance(item, dict) and _sha256(local) != item["sha256"]:
                os.remove(local)
                raise DamagedFile(f"{obj} doesn't match the SHA-256 in its manifest")
            yield local
            os.remove(local)
    return fetched


def _steps(fetched, manifests):
    for m in manifests:
        entity = [f for f in m["files"] if f["kind"] == "changes"]
        relationship = [f for f in m["files"] if f["kind"] == "relationship-changes"]
        yield m["date"], fetched(entity), fetched(relationship), m["changes"]


def _meta(manifest):
    return {k: manifest[k] for k in ("date", "snapshotRoot", "records", "relationships", "stats")} | {
        "sources": manifest.get("sources") or []}


def run(*, bucket, connect, readers, gate=lambda: require_running(COMPONENT), log=log, settings=None) -> str:
    """Return the outcome: halted, no-snapshot, busy, already-loaded or loaded."""
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
    if latest.get("schemaVersion", 1) < 2:
        return _run_v1(bucket, connect, readers, latest, log)
    from lc_core.publishing import load as load_settings
    from lc_core.snapshot_format import from_anchor, plan_load
    from lc_directory.load import LOAD_VERSION, LoadMismatch, live_meta, load, load_incremental

    settings = settings or load_settings().load  # invalid settings stop the load before it starts

    def read_manifest(date):
        blob = bucket.blob(f"{date}/manifest.json")
        return json.loads(blob.download_as_bytes()) if blob.exists() else None

    manifest = read_manifest(latest["date"])
    if manifest is None:
        raise RuntimeError(f"latest.json names {latest['date']}, which has no manifest")
    with connect() as conn:
        # One load at a time. Postgres releases this lock by itself if the loader dies.
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext('lc-load-db'))").fetchone()[0]:
            log({"event": "busy", "reason": "another load is running"})
            return "busy"
        conn.commit()
        live = live_meta(conn)
        plan = plan_load(read_manifest, manifest, live, incremental=settings.incremental,
                         max_days=settings.incremental_max_days, max_fraction=settings.incremental_max_fraction,
                         load_version=LOAD_VERSION)
        if plan["mode"] == "already-loaded":
            log({"event": "already-loaded", "date": latest["date"]})
            return "already-loaded"
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="load-") as work:
            fetched = _fetcher(bucket, work)
            if plan["mode"] == "incremental":
                try:
                    counts = load_incremental(conn, _steps(fetched, plan["steps"]), _meta(manifest),
                                              vacuum_after=settings.vacuum_after_incremental)
                    log({"event": "loaded", "mode": "incremental", "date": manifest["date"], "from": live["date"],
                         "seconds": round(time.time() - started), **counts})
                    return "loaded"
                except Exception as exc:  # rolled back: the database still serves the day it held
                    log({"event": "incremental-failed", "error": type(exc).__name__, "reason": str(exc)[:300]})
                    if not settings.fallback_to_full or not isinstance(exc, (LoadMismatch, _db_errors())):
                        raise  # a damaged file would be as damaged for a full load
                    chain = from_anchor(read_manifest, manifest)
                    plan = {"mode": "full", "anchor": chain[0], "steps": chain[1:], "why": "the incremental load failed a check"}
                    started = time.time()
            anchor = plan["anchor"]
            counts = load(conn, fetched([f for f in anchor["files"] if f["kind"] == "entities"]),
                          fetched([f for f in anchor["files"] if f["kind"] == "relationships"]),
                          _meta(manifest), readers=readers, steps=_steps(fetched, plan["steps"]))
    log({"event": "loaded", "mode": "full", "why": plan["why"], "date": manifest["date"], "anchor": anchor["date"],
         "seconds": round(time.time() - started), **counts})
    return "loaded"


def _db_errors():
    import psycopg

    return psycopg.Error


def _run_v1(bucket, connect, readers, latest, log) -> str:
    """A snapshot in the old format: every file of the day is a full copy."""
    from lc_directory.load import already_loaded, load

    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext('lc-load-db'))").fetchone()[0]:
            log({"event": "busy", "reason": "another load is running"})
            return "busy"
        if already_loaded(conn, latest["snapshotRoot"]):
            log({"event": "already-loaded", "date": latest["date"]})
            return "already-loaded"
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="load-") as work:
            fetched = _fetcher(bucket, work)
            counts = load(conn, fetched(f"{latest['prefix']}/{n}" for n in latest.get("files", [])),
                          fetched(f"{latest['prefix']}/{n}" for n in latest.get("relationships", [])),
                          {k: latest[k] for k in ("date", "snapshotRoot")}, readers=readers)
    log({"event": "loaded", "mode": "full", "date": latest["date"], "seconds": round(time.time() - started), **counts})
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
    return 0 if outcome in OK else 1


if __name__ == "__main__":
    sys.exit(main())
