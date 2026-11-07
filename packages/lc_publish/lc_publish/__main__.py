"""Build today's snapshot from the data bucket:

    python -m lc_publish --bucket lei-commons-prod-data --out out --date 2026-11-07
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from lc_core.control import SystemHalted, require_running

from .snapshot import SOURCES, build


def read_source(bucket, prefix: str) -> dict:
    records = {}
    for blob in bucket.list_blobs(prefix=f"{prefix}/records/"):
        for line in gzip.decompress(blob.download_as_bytes()).decode("utf-8").splitlines():
            record = json.loads(line)
            records[record["directoryId"]] = record
    return records


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
    manifest = build({s["id"]: read_source(bucket, s["prefix"]) for s in SOURCES}, Path(a.out), a.date)
    print(json.dumps({k: manifest[k] for k in ("date", "records", "recordsPerSource", "inMoreThanOneSource", "snapshotRoot")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
