import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from lc_ingest.export import LEDGER, StoreBusy, export, rebuild_and_compare
from lc_ingest.ledger import diff
from lc_ingest.store import encode_records, shard_of

T0 = datetime(2026, 11, 8, 2, 0, tzinfo=timezone.utc)


class Blob:
    def __init__(self, name, data, when):
        self.name, self.data, self.updated, self.time_created = name, data, when, when

    def download_as_bytes(self):
        return self.data

    def download_to_filename(self, path):
        with open(path, "wb") as f:
            f.write(self.data)


class Bucket:
    def __init__(self):
        self.blobs, self.clock = {}, T0

    def tick(self):
        self.clock += timedelta(seconds=1)
        return self.clock

    def put(self, name, data):
        old = self.blobs.get(name)
        blob = Blob(name, data, self.tick())
        if old:
            blob.time_created = old.time_created
        self.blobs[name] = blob

    def get_blob(self, name):
        return self.blobs.get(name)

    def list_blobs(self, prefix=""):
        return [b for n, b in sorted(self.blobs.items()) if n.startswith(prefix)]


def rec(i, name, status="Active"):
    return {"directoryId": f"LECOZZ{i:014d}", "legalName": name, "legalStatus": status}


def write_store(bucket, records, key, changes=None):
    """Like the runner: change file, then shards, then the manifest."""
    if changes is not None:
        body = "".join(json.dumps(c, sort_keys=True) + "\n" for c in changes)
        bucket.put(f"gb/changes/{key}.jsonl.gz", gzip.compress(body.encode()))
    shards = {}
    for r in records.values():
        shards.setdefault(shard_of(r["directoryId"]), {})[r["directoryId"]] = r
    for n, recs in shards.items():
        bucket.put(f"gb/records/{n:03d}.jsonl.gz", encode_records(recs))
    bucket.put("gb/manifest.json", json.dumps({"records": len(records), "lastKey": key, "format": 2}).encode())


def changes_between(old, new):
    out = []
    for k in sorted(set(old) | set(new)):
        out += diff(old.get(k, {}), new.get(k, {}), "gb-coh", "ingestion@gb")
    return out


def test_baseline_then_changes_rebuild_the_store_exactly(tmp_path):
    bucket = Bucket()
    day1 = {r["directoryId"]: r for r in (rec(i, f"ACME {i}") for i in range(40))}
    write_store(bucket, day1, "day1", changes=changes_between({}, day1))
    added = export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)
    assert added["baseline"] > 1 and added["changes"] == 0  # the first load's changes are in the baseline

    day2 = dict(day1)
    day2[rec(3, "x")["directoryId"]] = rec(3, "ACME 3 RENAMED", "Dissolved")
    day2[rec(99, "x")["directoryId"]] = rec(99, "NEW CO")
    write_store(bucket, day2, "day2", changes=changes_between(day1, day2))
    assert export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None) == {"baseline": 0, "changes": 1}
    assert export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None) == {"baseline": 0, "changes": 0}

    result = rebuild_and_compare(bucket, "gb", tmp_path / "ledger", tmp_path / "work", log=lambda *_: None)
    assert result["records"] == 41 and result["mismatchedShards"] == 0
    state = json.loads((tmp_path / "ledger" / LEDGER).read_text())
    assert [c["name"] for c in state["changes"]] == ["day2.jsonl.gz"] and state["upTo"]["storeManifest"]["lastKey"] == "day2"


def test_a_tampered_ledger_does_not_rebuild(tmp_path):
    bucket = Bucket()
    day1 = {r["directoryId"]: r for r in (rec(i, f"ACME {i}") for i in range(10))}
    write_store(bucket, day1, "day1", changes=changes_between({}, day1))
    export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)
    day2 = {**day1, rec(1, "x")["directoryId"]: rec(1, "ACME 1 LTD")}
    write_store(bucket, day2, "day2", changes=changes_between(day1, day2))
    export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)
    change_file = tmp_path / "ledger" / "changes" / "day2.jsonl.gz"
    change_file.write_bytes(gzip.compress(gzip.decompress(change_file.read_bytes()).replace(b"ACME 1 LTD", b"EVIL LTD")))
    with pytest.raises(ValueError, match="doesn't match its hash"):
        rebuild_and_compare(bucket, "gb", tmp_path / "ledger", tmp_path / "work", log=lambda *_: None)


def test_a_store_caught_mid_write_is_refused_or_waited_for(tmp_path):
    bucket = Bucket()
    day1 = {r["directoryId"]: r for r in (rec(i, f"ACME {i}") for i in range(10))}
    write_store(bucket, day1, "day1", changes=changes_between({}, day1))
    first = next(n for n in bucket.blobs if "/records/" in n)
    bucket.put(first, bucket.blobs[first].data)  # a shard rewritten after the manifest: an ingest is running
    with pytest.raises(StoreBusy):
        export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)

    bucket.put("gb/manifest.json", bucket.blobs["gb/manifest.json"].data)  # the ingest finished
    export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)
    bucket.put("gb/changes/next.jsonl.gz", gzip.compress(b""))  # a change file whose run hasn't finished
    assert export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)["changes"] == 0


def test_the_rebuild_is_skipped_when_the_store_moved_on(tmp_path):
    bucket = Bucket()
    day1 = {r["directoryId"]: r for r in (rec(i, f"ACME {i}") for i in range(5))}
    write_store(bucket, day1, "day1", changes=changes_between({}, day1))
    export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)
    write_store(bucket, {**day1, rec(7, "x")["directoryId"]: rec(7, "LATE")}, "day2", changes=[])
    assert rebuild_and_compare(bucket, "gb", tmp_path / "ledger", tmp_path / "work", log=lambda *_: None)["skipped"]


def test_big_change_files_are_split_into_ordered_parts_that_still_rebuild(tmp_path, monkeypatch):
    import lc_ingest.export as ex

    monkeypatch.setattr(ex, "SPLIT_BYTES", 10)
    monkeypatch.setattr(ex, "CHUNK_LINES", 7)
    bucket = Bucket()
    day1 = {r["directoryId"]: r for r in (rec(i, f"ACME {i}") for i in range(5))}
    write_store(bucket, day1, "day1", changes=changes_between({}, day1))
    export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)
    day2 = dict(day1)
    for i in range(5, 30):
        day2[rec(i, "x")["directoryId"]] = rec(i, f"NEW {i}")
    day2[rec(2, "x")["directoryId"]] = rec(2, "ACME 2 RENAMED")
    write_store(bucket, day2, "day2", changes=changes_between(day1, day2))
    export(bucket, "gb", tmp_path / "ledger", log=lambda *_: None)

    state = json.loads((tmp_path / "ledger" / LEDGER).read_text())
    parts = state["changes"][0]["parts"]
    assert len(parts) > 2 and parts[0]["name"] == "day2.part001.jsonl.gz"
    assert not (tmp_path / "ledger" / "changes" / "day2.jsonl.gz").exists()
    assert rebuild_and_compare(bucket, "gb", tmp_path / "ledger", tmp_path / "work", log=lambda *_: None)["mismatchedShards"] == 0

    (tmp_path / "ledger" / "changes" / parts[1]["name"]).write_bytes(gzip.compress(b""))
    with pytest.raises(ValueError, match="doesn't match its hash"):
        rebuild_and_compare(bucket, "gb", tmp_path / "ledger", tmp_path / "work2", log=lambda *_: None)
