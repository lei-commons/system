"""Where records and changes live until the ledger repositories exist.

Everything is in the private data bucket, one prefix per source. Records are split
into 256 shards by directory ID, as the design specifies, so a day's work only
reads the shards it touches.
"""

from __future__ import annotations

import gzip
import hashlib
import json

SHARDS = 256
# Version 1 joined lines with a literal backslash-n, so its files can't be read
# back. Anything older than this is treated as absent and rebuilt from source.
FORMAT = 2


def shard_of(directory_id: str) -> int:
    return hashlib.sha256(directory_id.encode()).digest()[0] % SHARDS


class GcsStore:
    def __init__(self, bucket: str, prefix: str):
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket) if isinstance(bucket, str) else bucket
        self._prefix = prefix
        self._stale = True  # until a current-format manifest has been read

    def _blob(self, name):
        return self._bucket.blob(f"{self._prefix}/{name}")

    def read_manifest(self) -> dict:
        blob = self._blob("manifest.json")
        manifest = json.loads(blob.download_as_bytes()) if blob.exists() else {}
        self._stale = manifest.get("format") != FORMAT
        if self._stale:
            return {"format": FORMAT, "records": 0, "processed": []}
        return manifest

    def write_manifest(self, manifest: dict) -> None:
        manifest = {**manifest, "format": FORMAT}
        self._blob("manifest.json").upload_from_string(json.dumps(manifest, indent=2, sort_keys=True),
                                                       content_type="application/json")

    def read_shard(self, n: int) -> dict:
        blob = self._blob(f"records/{n:03d}.jsonl.gz")
        if self._stale or not blob.exists():
            return {}
        lines = gzip.decompress(blob.download_as_bytes()).decode().splitlines()
        return {r["directoryId"]: r for r in map(json.loads, lines)}

    def write_shard(self, n: int, records: dict) -> None:
        body = "".join(json.dumps(records[k], sort_keys=True) + "\n" for k in sorted(records))
        self._blob(f"records/{n:03d}.jsonl.gz").upload_from_string(gzip.compress(body.encode()),
                                                                   content_type="application/gzip")

    def write_changes(self, name: str, changes: list) -> None:
        body = "".join(json.dumps(c, sort_keys=True) + "\n" for c in changes)
        self._blob(f"changes/{name}.jsonl.gz").upload_from_string(gzip.compress(body.encode()),
                                                                  content_type="application/gzip")

    def keep_source(self, name: str, path: str) -> None:
        self._blob(f"sources/{name}").upload_from_filename(path)
