"""Build a full snapshot from the data bucket, locally (the daily cycle uses lc_publish.job):

    python -m lc_publish --bucket lei-commons-prod-data --out out --date 2026-11-07
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from lc_core.control import SystemHalted, require_running
from lc_core.publishing import load as load_settings

from .snapshot import build_sharded, sources


def shard_reader(bucket, prefix: str):
    """Return read(n): a source's records in shard n, or {} if it has none."""
    def read(n: int) -> dict:
        blob = bucket.blob(f"{prefix}/records/{n:03d}.jsonl.gz")
        if not blob.exists():
            return {}
        records = {}
        for line in gzip.decompress(blob.download_as_bytes()).decode("utf-8").splitlines():
            record = json.loads(line)
            records[record["directoryId"]] = record
        return records

    return read


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--date", required=True)
    a = ap.parse_args(argv)
    try:
        require_running("job:publish")  # manual mode stops publishing too
    except SystemHalted as exc:
        print(f"not publishing: {exc}", file=sys.stderr)
        return 3

    from google.cloud import storage

    bucket = storage.Client().bucket(a.bucket)
    registered = sources()
    rel = [s["relationships"] for s in registered if s.get("relationships")]
    manifest = build_sharded({s["id"]: shard_reader(bucket, s["prefix"]) for s in registered}, Path(a.out), a.date,
                             settings=load_settings().snapshots,
                             relationships=shard_reader(bucket, rel[0]) if len(rel) == 1 else None)
    print(json.dumps({k: manifest[k] for k in ("date", "kind", "records", "recordsPerSource", "inMoreThanOneSource",
                                               "relationships", "snapshotRoot")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
