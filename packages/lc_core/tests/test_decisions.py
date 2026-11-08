import json

import pytest

from lc_core.decisions import Recorder, log_bucket_name, seal_day, seal_through, yesterday
from lc_core.events import GENESIS, verify


class PreconditionFailed(Exception):
    pass


class Listing(list):
    def __init__(self, items, prefixes):
        super().__init__(items)
        self.prefixes = prefixes


class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def _create(self, data: bytes, if_generation_match):
        if if_generation_match == 0 and self.name in self.bucket.objects:
            raise PreconditionFailed(self.name)
        if self.bucket.create_only and self.name in self.bucket.objects:
            raise PermissionError("create-only bucket")
        self.bucket.objects[self.name] = data

    def upload_from_filename(self, path, if_generation_match=None, content_type=None):
        with open(path, "rb") as f:
            self._create(f.read(), if_generation_match)

    def upload_from_string(self, data, if_generation_match=None, content_type=None):
        self._create(data.encode() if isinstance(data, str) else data, if_generation_match)

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def download_as_text(self):
        return self.bucket.objects[self.name].decode()


class Bucket:
    """Like the decision log bucket: objects can be created, never replaced."""

    def __init__(self):
        self.objects, self.create_only = {}, True

    def blob(self, name):
        return Blob(self, name)

    def list_blobs(self, prefix="", delimiter=None):
        names = sorted(n for n in self.objects if n.startswith(prefix))
        if delimiter:
            prefixes = sorted({prefix + n[len(prefix):].split(delimiter)[0] + delimiter for n in names
                               if delimiter in n[len(prefix):]})
            return Listing([self.blob(n) for n in names if delimiter not in n[len(prefix):]], prefixes)
        return Listing([self.blob(n) for n in names], [])


def run(bucket, actor, records, run_id, tmp_path):
    seen = []
    r = Recorder(actor, log=seen.append, run_id=run_id)
    for rec in records:
        r(rec)
    return r.write(bucket, str(tmp_path)), seen


def test_a_run_writes_its_decisions_once_and_passes_every_record_on(tmp_path):
    bucket = Bucket()
    name, seen = run(bucket, "job:ingest-gb", [
        {"event": "progress", "shard": 1},
        {"event": "done", "source": "gb", "publish": "2026-11-08 part1", "changes": 12, "new_records": 2,
         "existing_changed": 10, "total": 850001, "nested": {"x": 1}},
    ], "ingest-gb-abc", tmp_path)
    assert name.startswith("runs/") and name.endswith("-job-ingest-gb-ingest-gb-abc.jsonl")
    assert [r["event"] for r in seen] == ["progress", "done", "decisions-written"]
    lines = bucket.objects[name].decode().splitlines()
    event = json.loads(lines[0])
    assert event["decision"] == "done" and event["subject"] == "gb 2026-11-08 part1" and event["run_id"] == "ingest-gb-abc"
    assert event["refs"]["changes"] == 12 and "nested" not in event["refs"]
    assert verify(lines)[0]


def test_nothing_is_written_without_decisions_and_failures_never_raise(tmp_path):
    bucket = Bucket()
    assert run(bucket, "job:x", [{"event": "progress"}], "r1", tmp_path)[0] is None
    seen = []
    r = Recorder("job:x", log=seen.append, run_id="r2")
    r({"event": "busy", "reason": "lease held"})

    class Broken:
        def blob(self, name):
            raise PermissionError("no access")

    assert r.write(Broken(), str(tmp_path)) is None
    assert seen[-1] == {"event": "decisions-unwritten", "error": "PermissionError"}


def test_days_are_sealed_in_order_into_one_verifiable_chain(tmp_path):
    bucket = Bucket()
    for day, actor, run_id in (("2026-11-07", "job:ingest-gleif", "a"), ("2026-11-08", "job:ingest-gb", "b"),
                               ("2026-11-08", "job:build-snapshot", "c")):
        r = Recorder(actor, log=lambda record: None, run_id=run_id)
        r({"event": "already-done", "source": "gleif", "publish": f"{day} full"})
        r.events[0].ts = f"{day}T02:0{run_id}0:00.000Z".replace("a0", "10").replace("b0", "20").replace("c0", "30")
        r.write(bucket, str(tmp_path))

    seals = seal_through(bucket, "2026-11-08", str(tmp_path))
    assert [s["day"] for s in seals] == ["2026-11-07", "2026-11-08"]
    first, second = seals
    assert first["previousHead"] == GENESIS and first["events"] == 1
    assert second["previousHead"] == first["head"] and second["events"] == 2
    ok, count, head = verify(bucket.objects["chains/2026-11-08.jsonl"].decode().splitlines(), start=first["head"])
    assert ok and count == 2 and head == second["head"]
    assert all("runObject" in json.loads(line)["refs"] for line in bucket.objects["chains/2026-11-08.jsonl"].decode().splitlines())

    # Sealing again changes nothing, and the next day carries on from the last head.
    assert seal_day(bucket, "2026-11-08", str(tmp_path)) == second
    assert seal_through(bucket, "2026-11-08", str(tmp_path)) == []
    empty = seal_through(bucket, "2026-11-09", str(tmp_path))[0]
    assert empty["events"] == 0 and empty["head"] == second["head"] and "chains/2026-11-09.jsonl" not in bucket.objects


def test_a_tampered_run_object_cannot_be_sealed_unnoticed(tmp_path):
    bucket = Bucket()
    name, _ = run(bucket, "job:x", [{"event": "busy", "reason": "lease held"}], "r", tmp_path)
    day = name.split("/")[1]
    sealed = seal_day(bucket, day, str(tmp_path))
    line = json.loads(bucket.objects[f"chains/{day}.jsonl"].decode())
    line["reason"] = "something else"
    assert not verify([json.dumps(line)], start=sealed["previousHead"])[0]


def test_helpers():
    assert yesterday("2026-11-01") == "2026-10-31"
    assert log_bucket_name("lei-commons-prod-data") == "lei-commons-prod-decision-log"
    with pytest.raises(ValueError):
        log_bucket_name("elsewhere")
