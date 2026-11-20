"""An old directory ID that merged into another answers with its tombstone and where it went; search and
suggestions leave tombstones out unless asked for them."""

import http.client
import json
import threading

import pytest

from lc_core import tombstone
from lc_core.publishing import load as load_settings
from lc_publish.snapshot import build as build_snapshot
from lc_site.data import Snapshot
from lc_site.server import make_server

AUD = "/projects/1/locations/us-central1/services/lei-commons-site"
OLD, NEW = "LECOZZ00000000000O11", "LECOZZ00000000000N22"
LEI = "5493000OREGON0000001"
REGISTER = {"directoryId": NEW, "legalName": "PRECISION CASTPARTS CORP.", "legalStatus": "Active", "legalJurisdiction": "US-OR",
            "registrationNumber": "5749114"}
GLEIF = {"directoryId": OLD, "lei": LEI, "legalName": "PRECISION CASTPARTS CORP", "legalJurisdiction": "US-OR",
         "registrationAuthority": "RA000631", "registrationNumber": "057491-14", "legalAddress": {"country": "US"}}


@pytest.fixture(scope="module")
def snap(tmp_path_factory):
    out = tmp_path_factory.mktemp("snap")
    records = {"us-or-sos": {NEW: REGISTER}, "gleif": {NEW: {**GLEIF, "directoryId": NEW}, OLD: tombstone.make(GLEIF, NEW)}}
    m = build_snapshot(records, out, "2026-11-22", settings=load_settings().snapshots)
    files = [str(out / "parts" / f["name"]) for f in m["parts"] if f["kind"] == "entities"]
    return Snapshot(files, {"date": m["date"], "snapshotRoot": m["snapshotRoot"]})


def ids(found):
    return [r["directoryId"] for r in found["results"]]


def test_the_old_id_answers_with_its_tombstone_and_where_it_went(snap):
    grave = snap.entity(OLD)
    assert grave["mergedInto"] == NEW and grave["statusCategory"] == "merged" and "lei" not in grave
    assert grave["legalName"] == "PRECISION CASTPARTS CORP" and grave["registrationNumber"] == "057491-14"
    live = snap.entity(NEW)
    assert "mergedInto" not in live and live["lei"] == LEI and live["sources"] == ["us-or-sos", "gleif"]


def test_search_leaves_tombstones_out_unless_asked(snap):
    assert ids(snap.search("precision castparts")) == [NEW]
    assert ids(snap.search(LEI)) == [NEW]  # an LEI resolves to one record
    assert ids(snap.search(OLD)) == [OLD]  # its own ID still finds it
    assert ids(snap.search("precision", status="merged")) == [OLD]
    assert [r["directoryId"] for r in snap.suggest("precision")] == [NEW]


def test_dashboards_count_tombstones_as_merged(snap):
    assert {x["value"]: x["records"] for x in snap.stats()["byStatusCategory"]} == {"active": 1, "merged": 1}


def test_the_api_gives_the_tombstone_with_merged_into(snap):
    srv = make_server(snap, ["owner@nfh.test"], AUD, ("127.0.0.1", 0),
                      iap=lambda token, audience: "owner@nfh.test" if audience == AUD else None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("GET", f"/api/v1/entities/{OLD}", headers={"X-Goog-IAP-JWT-Assertion": "owner"})
        r = c.getresponse()
        body = json.loads(r.read())
        assert r.status == 200 and body["mergedInto"] == NEW and body["directoryId"] == OLD
        c.close()
    finally:
        srv.shutdown()
        srv.server_close()
