"""Where records and changes live until the ledger repositories exist.

Everything is in the private data bucket, one prefix per source. Records are
split into 256 shards by directory ID, as the design specifies, so a run only
reads the shards it touches, and one shard at a time fits easily in memory.
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


def encode_records(records: dict) -> bytes:
    """One shard's bytes: one JSON object per line, sorted by directory ID, gzipped."""
    body = "".join(json.dumps(records[k], sort_keys=True) + "\n" for k in sorted(records))
    return gzip.compress(body.encode("utf-8"))


def decode_records(data: bytes) -> dict:
    lines = gzip.decompress(data).decode("utf-8").splitlines()
    return {r["directoryId"]: r for r in map(json.loads, lines)}


class GcsStore:
    def __init__(self, bucket, prefix: str):
        if isinstance(bucket, str):
            from google.cloud import storage

            bucket = storage.Client().bucket(bucket)
        self._bucket = bucket
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
        return decode_records(blob.download_as_bytes())

    def write_shard(self, n: int, records: dict) -> None:
        self._blob(f"records/{n:03d}.jsonl.gz").upload_from_string(encode_records(records),
                                                                   content_type="application/gzip")

    def write_shard_file(self, n: int, path: str) -> None:
        """Upload a shard already encoded with ``encode_records``."""
        self._blob(f"records/{n:03d}.jsonl.gz").upload_from_filename(path, content_type="application/gzip")

    def write_changes_file(self, name: str, path: str) -> None:
        """Upload a gzipped JSON-lines file of changes."""
        self._blob(f"changes/{name}.jsonl.gz").upload_from_filename(path, content_type="application/gzip")

    def keep_seen(self, name: str, ids) -> None:
        """The directory IDs one publication file listed, one per line, gzipped."""
        body = "".join(i + "\n" for i in sorted(ids)).encode("utf-8")
        self._blob(f"seen/{name}.txt.gz").upload_from_string(gzip.compress(body), content_type="application/gzip")

    def read_seen(self, name: str):
        """The IDs kept by keep_seen, or None if there is no such list."""
        blob = self._blob(f"seen/{name}.txt.gz")
        if not blob.exists():
            return None
        return set(gzip.decompress(blob.download_as_bytes()).decode("utf-8").split())

    def write_marker(self, name: str, body: dict) -> None:
        self._blob(f"markers/{name}.json").upload_from_string(json.dumps(body, sort_keys=True), content_type="application/json")

    def has_marker(self, name: str) -> bool:
        return self._blob(f"markers/{name}.json").exists()

    def keep_source(self, name: str, path: str) -> None:
        self._blob(f"sources/{name}").upload_from_filename(path)
