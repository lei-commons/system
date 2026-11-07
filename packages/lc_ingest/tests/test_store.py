"""The real storage code against a stand-in for Cloud Storage, so the file
format itself is tested, not just the logic around it."""

import gzip
import json

from lc_ingest.store import FORMAT, GcsStore


class Blob:
    def __init__(self, files, name):
        self.files, self.name = files, name

    def exists(self):
        return self.name in self.files

    def download_as_bytes(self):
        return self.files[self.name]

    def upload_from_string(self, data, content_type=None):
        self.files[self.name] = data.encode() if isinstance(data, str) else data


class Bucket:
    def __init__(self):
        self.files = {}

    def blob(self, name):
        return Blob(self.files, name)


def store(bucket=None):
    return GcsStore(bucket or Bucket(), "gb")


RECORDS = {"LECOZZ1": {"directoryId": "LECOZZ1", "legalName": "LINE\\nBREAK LTD"},
           "LECOZZ2": {"directoryId": "LECOZZ2", "legalName": "SECOND"}}


def test_shards_round_trip_through_the_real_format():
    s = store()
    s.read_manifest()
    s.write_manifest({"records": 2, "processed": []})
    s = GcsStore(s._bucket, "gb")
    s.read_manifest()
    s.write_shard(7, RECORDS)
    assert s.read_shard(7) == RECORDS


def test_one_json_object_per_real_line():
    b = Bucket()
    s = store(b)
    s.read_manifest()
    s.write_shard(1, RECORDS)
    s.write_changes("day", [{"a": 1}, {"b": 2}])
    for name in ("gb/records/001.jsonl.gz", "gb/changes/day.jsonl.gz"):
        lines = gzip.decompress(b.files[name]).decode().split("\n")
        assert lines[-1] == "" and all(json.loads(line) for line in lines[:-1]) and len(lines) == 3


def test_old_format_is_treated_as_absent_and_rebuilt():
    b = Bucket()
    b.files["gb/manifest.json"] = json.dumps({"records": 849999, "processed": ["2026-11-01"]}).encode()
    b.files["gb/records/007.jsonl.gz"] = gzip.compress(b'{"directoryId": "X"}\\n{"directoryId": "Y"}\\n')
    s = store(b)
    assert s.read_manifest() == {"format": FORMAT, "records": 0, "processed": []}
    assert s.read_shard(7) == {}


def test_current_format_is_read_normally():
    b = Bucket()
    s = store(b)
    s.read_manifest()
    s.write_shard(7, RECORDS)
    s.write_manifest({"records": 2, "processed": ["2026-11-01"]})
    again = store(b)
    assert again.read_manifest()["processed"] == ["2026-11-01"] and again.read_shard(7) == RECORDS
