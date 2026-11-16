"""The site with format v2: the in-memory fallback built from a full snapshot plus later days' changes,
and the Data page's downloads (shared parts, change files, and what must never be reachable)."""

import json
from dataclasses import replace
from datetime import date as Date
from datetime import datetime, timedelta, timezone

import pytest
from lc_core.publishing import load as load_settings
from lc_publish.job import publish
from lc_publish.testing import FakeBucket, World
from lc_site.downloads import SnapshotFiles
from lc_site.server import load_from

START = Date(2026, 11, 18)


@pytest.fixture(scope="module")
def days(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("v2")
    base = load_settings()
    s = replace(base, snapshots=replace(base.snapshots, full_every_days=3, part_target_records=200, prune_grace_days=0))
    snapshots, data, world, out = FakeBucket(), FakeBucket(), World(n=250), []
    for i in range(5):
        date = (START + timedelta(days=i)).isoformat()
        work = tmp / date
        work.mkdir()
        out.append(publish(snapshots, data, date, world.readers(), world.relationship_reader(), s, work, lambda e: None,
                           now=datetime.fromisoformat(date + "T03:00:00+00:00").replace(tzinfo=timezone.utc)))
        world.mutate()
    return snapshots, out, world


def test_the_fallback_is_the_full_snapshot_plus_each_days_changes(days):
    snapshots, manifests, _ = days
    latest = json.loads(snapshots.objects["latest.json"]["data"])
    m = manifests[-1]
    assert m["kind"] == "changes"  # day 5: a full snapshot on day 4, then changes
    snap = load_from(snapshots, latest)
    assert snap.count == m["records"] and snap.relationship_count == m["relationships"]
    assert snap.meta == {"date": m["date"], "snapshotRoot": m["snapshotRoot"]}
    added = m["changes"]["added"]
    assert added and snap.search("NEW")["total"] >= added  # records added since the full snapshot are searchable


def test_the_fallback_refuses_a_damaged_file(days):
    snapshots, manifests, _ = days
    name = f"{manifests[-1]['date']}/changes.000.parquet"
    original = snapshots.objects[name]["data"]
    snapshots.objects[name]["data"] = original + b"\0"
    try:
        with pytest.raises(ValueError, match="doesn't match"):
            load_from(snapshots, json.loads(snapshots.objects["latest.json"]["data"]))
    finally:
        snapshots.objects[name]["data"] = original


def test_the_data_page_lists_full_and_change_days(days):
    snapshots, manifests, _ = days
    listed = {s["date"]: s for s in SnapshotFiles(snapshots).snapshots()["snapshots"]}
    full, changes = listed[manifests[3]["date"]], listed[manifests[4]["date"]]
    assert full["kind"] == "full" and changes["kind"] == "changes" and changes["anchor"] == full["date"]
    assert changes["base"] == full["date"] and changes["changes"]["added"] == manifests[4]["changes"]["added"]
    assert all(f["kind"] in ("changes", "relationship-changes", "about") for f in changes["files"])
    assert any(f["kind"] == "entities" for f in full["files"]) and all(f["bytes"] for f in full["files"])


def test_downloads_reach_shared_parts_and_change_files_only(days):
    snapshots, manifests, _ = days
    files = SnapshotFiles(snapshots)
    full, changes = manifests[3], manifests[4]
    part = next(f for f in full["files"] if f["kind"] == "entities")
    assert files.blob(full["date"], part["name"]).name == part["object"]
    change = next(f for f in changes["files"] if f["kind"] == "changes")
    assert files.blob(changes["date"], change["name"]).name == f"{changes['date']}/{change['name']}"
    assert files.blob(changes["date"], "manifest.json").name == f"{changes['date']}/manifest.json"
    assert files.blob(changes["date"], part["name"]) is None  # a part isn't a file of a changes day
    for bad in ("../latest.json", "latest.json", "parts", "x.parquet"):
        assert files.blob(full["date"], bad) is None


def test_a_tampered_manifest_cannot_point_downloads_elsewhere(days):
    snapshots, manifests, _ = days
    m = json.loads(json.dumps(manifests[3]))
    m["files"][0]["object"] = "latest.json"
    m["files"][1]["object"] = f"{m['date']}/../../secret"
    name = f"{m['date']}/manifest.json"
    original = snapshots.objects[name]["data"]
    snapshots.objects[name]["data"] = json.dumps(m).encode()
    try:
        files = SnapshotFiles(snapshots)
        assert files.blob(m["date"], m["files"][0]["name"]) is None and files.blob(m["date"], m["files"][1]["name"]) is None
    finally:
        snapshots.objects[name]["data"] = original


def test_the_fallback_credits_every_record_too(days):
    snapshots, manifests, world = days
    snap = load_from(snapshots, json.loads(snapshots.objects["latest.json"]["data"]))
    some = next(iter(world.sources["gb-coh"]))
    record = snap.entity(some)
    assert record and record["registryUrl"] and record["credits"][0]["id"] == "gb-coh"
    assert "Open Government Licence" in record["credits"][0]["attribution"] and record["credits"][0]["citation"]
