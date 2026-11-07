"""The daily GLEIF run.

1. Stop at once unless the system is running.
2. Read GLEIF's publish list through the gateway. A publish already processed is skipped.
3. Download the LastDay delta through the gateway and keep the raw file.
4. Map, filter and diff, loading only the shards the day touches.
5. Write the changes, then the records, then the manifest. A crash before the
   manifest leaves the publish unprocessed, and the rerun finds the shards
   already updated, so nothing is applied twice.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter

from lc_core.allowlist import AllowList
from lc_core.control import SystemHalted, require_running
from lc_core.fetch import Denied, Gateway
from lc_ingest.gleif_csv import read_zip
from lc_ingest.ledger import VolumeGuardTripped
from lc_ingest.pipeline import diff_records, map_records

from .store import GcsStore, shard_of

COMPONENT = "job:ingest-gleif"
LISTING = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
DELTA = "LastDay"
KEEP_PROCESSED = 100


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def run(gateway, store, gate=lambda: require_running(COMPONENT), allowlist=None, log=log) -> str:
    """Return the outcome: halted, already-done, held, denied or done."""
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "reason": str(exc)})
        return "halted"

    manifest = store.read_manifest()
    try:
        listing = gateway.get_json(LISTING)["data"]
    except Denied as exc:
        log({"event": "denied", "rule": exc.rule, "reason": exc.reason})
        return "denied"
    publish = listing["publish_date"]
    if publish in manifest["processed"]:
        log({"event": "already-done", "publish": publish})
        return "already-done"
    url = listing["lei2"]["delta_files"][DELTA]["csv"]["url"]
    name = publish.replace(" ", "T").replace(":", "")

    counts = Counter()
    with tempfile.NamedTemporaryFile(suffix=".zip") as tmp:
        try:
            size = gateway.download(url, tmp)
        except Denied as exc:
            log({"event": "denied", "rule": exc.rule, "reason": exc.reason})
            return "denied"
        tmp.flush()
        store.keep_source(f"{name}-{DELTA}.zip", tmp.name)
        tmp.seek(0)
        records = list(map_records(read_zip(tmp), counts, allowlist or AllowList.load()))

    shards = {shard_of(r["directoryId"]) for r in records}
    loaded = {n: store.read_shard(n) for n in shards}
    current = {k: v for shard in loaded.values() for k, v in shard.items()}
    try:
        changes, updated = diff_records(records, current, manifest["records"], f"src:gleif-{name}-{DELTA}",
                                        "ingestion@gleif", counts)
    except VolumeGuardTripped as exc:
        log({"event": "held", "publish": publish, "reason": str(exc)})
        return "held"

    # Collision-separated records may land in shards that weren't loaded yet.
    for directory_id in updated:
        n = shard_of(directory_id)
        if n not in loaded:
            loaded[n] = store.read_shard(n)
    new_records = 0
    for directory_id, record in updated.items():
        shard = loaded[shard_of(directory_id)]
        new_records += directory_id not in shard
        shard[directory_id] = record

    store.write_changes(name, changes)
    for n in sorted({shard_of(k) for k in updated}):
        store.write_shard(n, loaded[n])
    manifest["records"] += new_records
    manifest["processed"] = (manifest["processed"] + [publish])[-KEEP_PROCESSED:]
    store.write_manifest(manifest)
    log({"event": "done", "publish": publish, "bytes": size, "changes": len(changes), "records_changed": len(updated),
         "new_records": new_records, "total": manifest["records"], **{k: v for k, v in counts.items()}})
    return "done"


def main() -> int:
    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    # An explicit path: a missing allow-list must stop the job, never fall back to nothing.
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    outcome = run(gateway, GcsStore(os.environ["LC_DATA_BUCKET"]), allowlist=allowlist)
    return 0 if outcome in ("done", "already-done", "halted") else 1


if __name__ == "__main__":
    sys.exit(main())
