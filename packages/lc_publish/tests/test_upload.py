"""The order of uploads: nothing points at something half-copied."""

import json

from lc_publish.testing import FakeBucket
from lc_core.publishing import load
from lc_publish.snapshot import build
from lc_publish.upload import UploadError, point_latest, upload_day, upload_state

from test_snapshot import sources

import pytest


def test_parts_then_day_files_then_manifest_then_state_then_latest(tmp_path):
    m = build(sources(), tmp_path, "2026-11-07", settings=load().snapshots)
    b, data = FakeBucket(), FakeBucket()
    sent = upload_day(b, tmp_path, m)
    upload_state(data, tmp_path, "2026-11-07")
    point_latest(b, m)
    assert b.writes[-1] == "latest.json" and b.writes[-2] == "2026-11-07/manifest.json"
    assert all(w.startswith("parts/") for w in b.writes[:len(m["parts"])])
    assert data.writes[-1] == "publish/state/2026-11-07/state.json"
    assert sent["uploaded"] == len(m["parts"]) and sent["reused"] == 0
    latest = json.loads(b.objects["latest.json"]["data"])
    assert latest == {**latest, "schemaVersion": 2, "date": "2026-11-07", "kind": "full", "snapshotRoot": m["snapshotRoot"]}
    again = upload_day(b, tmp_path, m)
    assert again["reused"] == len(m["parts"])  # stored once


def test_a_stored_object_of_another_size_is_refused(tmp_path):
    m = build(sources(), tmp_path, "2026-11-07", settings=load().snapshots)
    b = FakeBucket()
    b.objects[m["parts"][0]["object"]] = {"data": b"not it", "created": b.clock()}
    with pytest.raises(UploadError):
        upload_day(b, tmp_path, m)


def test_a_rebuilt_day_drops_files_its_manifest_no_longer_lists(tmp_path):
    m = build(sources(), tmp_path, "2026-11-07", settings=load().snapshots)
    b = FakeBucket()
    b.objects["2026-11-07/changes.007.parquet"] = {"data": b"old", "created": b.clock()}
    b.objects["2026-11-07/manifest.json.sigstore.json"] = {"data": b"old", "created": b.clock()}
    upload_day(b, tmp_path, m)
    assert "2026-11-07/changes.007.parquet" not in b.objects and "2026-11-07/manifest.json.sigstore.json" not in b.objects
