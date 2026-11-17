"""Fetching datasets through the site's HTTP API, when most days publish only their changes.

For every day, full or changes: ask /api/v1/snapshots/<date>/recipe what makes up that day's whole
directory, download each file over HTTP (behind the login), check its SHA-256, rebuild the day from
only what was downloaded, and compare it with the true records. Also: the listing matches what the
downloads return, nothing outside a manifest can be fetched, and pruning never strands a kept day."""

import hashlib
import http.client
import json
import threading
from dataclasses import replace
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from lc_core.publishing import load as load_settings
from lc_publish.job import publish
from lc_publish.rebuild import DirStore, rebuild
from lc_publish.testing import FakeBucket, World
from lc_site.downloads import SnapshotFiles
from lc_site.server import make_server

START = Date(2026, 11, 18)


class Snap:
    meta, count, relationship_count = {"date": "d", "snapshotRoot": "r"}, 0, 0


def published(tmp, days, **over):
    base = load_settings()
    s = replace(base, snapshots=replace(base.snapshots, **{"full_every_days": 3, "part_target_records": 200,
                                                           "change_file_max_rows": 30, "prune_grace_days": 0, **over}))
    snapshots, data, world, truths = FakeBucket(), FakeBucket(), World(n=250, seed=11), {}
    for i in range(days):
        date = (START + timedelta(days=i)).isoformat()
        (tmp / date).mkdir()
        publish(snapshots, data, date, world.readers(), world.relationship_reader(), s, tmp / date, lambda e: None,
                now=datetime.fromisoformat(date + "T03:00:00").replace(tzinfo=timezone.utc))
        truths[date] = world.truth()
        world.mutate()
    return snapshots, truths


class Site:
    def __init__(self, bucket):
        self.srv = make_server(Snap(), ["o@x.test"], "aud", ("127.0.0.1", 0),
                               iap=lambda t, a: "o@x.test" if t == "ok" else None, downloads=SnapshotFiles(bucket, ttl=0))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path, token="ok", method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request(method, path, headers={"X-Goog-IAP-JWT-Assertion": token})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def json(self, path):
        status, body = self.get(path)
        return status, json.loads(body)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    snapshots, truths = published(tmp_path_factory.mktemp("fetch"), 8)
    site = Site(snapshots)
    yield snapshots, truths, site
    site.close()


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fetch_whole_day(site, date, into: Path):
    """What a client does: the recipe, then every file and manifest, each checked by its SHA-256."""
    status, recipe = site.json(f"/api/v1/snapshots/{date}/recipe")
    assert status == 200, recipe
    for group in [recipe["anchor"]] + recipe["steps"]:
        folder = into / group["date"]
        folder.mkdir(parents=True, exist_ok=True)
        status, body = site.get(group["manifest"])
        assert status == 200
        (folder / "manifest.json").write_bytes(body)
        for f in group["files"]:
            status, body = site.get(f["url"])
            assert status == 200 and sha(body) == f["sha256"] and len(body) == f["bytes"], f["url"]
            (folder / f["name"]).write_bytes(body)
    return recipe


def rows_of(folder):
    rows = []
    for path in sorted(folder.glob("*.parquet")):
        if not path.name.startswith("relationships"):
            rows += [json.dumps(r, sort_keys=True) for r in pq.read_table(path).to_pylist()]
    return sorted(rows)


def test_every_day_can_be_fetched_whole_over_http_and_matches_the_truth(world, tmp_path):
    snapshots, truths, site = world
    kinds = {}
    for date, truth in truths.items():
        into = tmp_path / date
        recipe = fetch_whole_day(site, date, into)
        kinds[date] = recipe["kind"]
        report = rebuild(DirStore(into), date, tmp_path / f"out-{date}", log=lambda r: None)
        assert report["ok"] and report["snapshotRoot"] == recipe["snapshotRoot"]
        assert rows_of(tmp_path / f"out-{date}") == truth and recipe["records"] == len(truth)
    assert sorted(set(kinds.values())) == ["changes", "full"]


def test_a_full_days_recipe_is_just_its_parts_and_a_changes_day_names_its_full_snapshot(world):
    snapshots, truths, site = world
    status, listing = site.json("/api/v1/snapshots")
    by = {s["date"]: s for s in listing["snapshots"]}
    for date in truths:
        recipe = site.json(f"/api/v1/snapshots/{date}/recipe")[1]
        if by[date]["kind"] == "full":
            assert recipe["anchor"]["date"] == date and recipe["steps"] == []
        else:
            assert recipe["anchor"]["date"] == by[date]["anchor"]
            dates = [s["date"] for s in recipe["steps"]]
            assert dates == sorted(dates) and dates[-1] == date and dates[0] > recipe["anchor"]["date"]
        assert recipe["bytes"] == sum(f["bytes"] for g in [recipe["anchor"]] + recipe["steps"] for f in g["files"])


def test_the_listing_matches_what_the_downloads_return(world):
    snapshots, truths, site = world
    listing = site.json("/api/v1/snapshots")[1]["snapshots"]
    assert {s["date"] for s in listing} == set(truths)
    for snap in listing:
        for f in snap["files"]:
            status, body = site.get(f"/download/{snap['date']}/{f['name']}")
            assert status == 200 and len(body) == f["bytes"], (snap["date"], f["name"])
            if f.get("sha256"):
                assert sha(body) == f["sha256"]


def test_a_changes_day_serves_only_its_own_files(world):
    snapshots, truths, site = world
    listing = {s["date"]: s for s in site.json("/api/v1/snapshots")[1]["snapshots"]}
    changes_day = next(d for d, s in listing.items() if s["kind"] == "changes")
    anchor = listing[changes_day]["anchor"]
    part = next(f["name"] for f in listing[anchor]["files"] if f["kind"] == "entities")
    assert site.get(f"/download/{changes_day}/{part}")[0] == 404
    assert site.get(f"/download/{anchor}/{part}")[0] == 200


@pytest.mark.parametrize("path,status", [
    ("/api/v1/snapshots/2030-01-01/recipe", 404),
    ("/api/v1/snapshots/not-a-date/recipe", 404),
    ("/api/v1/snapshots/../latest.json/recipe", 404),
    ("/download/2026-11-18/../latest.json", 404),
    ("/download/2026-11-18/latest.json", 404),
    ("/download/latest.json", 404),
])
def test_bad_requests_reach_nothing(world, path, status):
    assert world[2].get(path)[0] == status


def test_everything_needs_the_owners_login(world):
    site = world[2]
    for path in ("/api/v1/snapshots", "/api/v1/snapshots/2026-11-19/recipe", "/download/2026-11-19/manifest.json"):
        assert site.get(path, token="bad")[0] == 403 and site.get(path, token="")[0] == 403
    assert site.get("/api/v1/snapshots/2026-11-19/recipe", method="POST")[0] in (403, 405)


def test_a_broken_chain_is_reported_not_guessed(tmp_path):
    snapshots, truths = published(tmp_path, 3)
    del snapshots.objects["2026-11-19/manifest.json"]
    site = Site(snapshots)
    try:
        status, body = site.json("/api/v1/snapshots/2026-11-20/recipe")
        assert status == 409 and "can't be assembled" in body["error"]
    finally:
        site.close()


def test_an_old_format_day_is_its_own_recipe(tmp_path):
    bucket = FakeBucket()
    m = {"date": "2026-11-10", "records": 1, "snapshotRoot": "sha256:v1",
         "files": [{"name": "GB.parquet", "country": "GB", "records": 1, "sha256": sha(b"gb")}]}
    bucket.objects["2026-11-10/manifest.json"] = {"data": json.dumps(m).encode(), "created": bucket.clock()}
    bucket.objects["2026-11-10/GB.parquet"] = {"data": b"gb", "created": bucket.clock()}
    site = Site(bucket)
    try:
        status, recipe = site.json("/api/v1/snapshots/2026-11-10/recipe")
        assert status == 200 and recipe["kind"] == "full" and recipe["steps"] == []
        status, body = site.get(recipe["anchor"]["files"][0]["url"])
        assert status == 200 and body == b"gb"
    finally:
        site.close()


def test_after_pruning_every_kept_day_is_still_fetchable_whole(tmp_path):
    snapshots, truths = published(tmp_path, 10, retain_days=2)
    site = Site(snapshots)
    try:
        listing = site.json("/api/v1/snapshots")[1]["snapshots"]
        kept = {s["date"] for s in listing}
        assert len(kept) < len(truths)  # pruning happened
        for date in sorted(kept):
            into = tmp_path / f"f-{date}"
            fetch_whole_day(site, date, into)
            report = rebuild(DirStore(into), date, tmp_path / f"o-{date}", log=lambda r: None)
            assert report["ok"] and rows_of(tmp_path / f"o-{date}") == truths[date]
        for date in set(truths) - kept:
            assert site.json(f"/api/v1/snapshots/{date}/recipe")[0] == 404
    finally:
        site.close()
