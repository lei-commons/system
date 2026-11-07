import http.client
import json
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from lc_publish.snapshot import build
from lc_site.data import MAX_LIMIT, Snapshot
from lc_site.server import make_server

OWNER = "owner@nfh.test"
AUD = "/projects/1/locations/us-central1/services/lei-commons-site"
ID = "LECOZZFZ6LZCOJBDKT73"


def records():
    gb = {ID: {"directoryId": ID, "legalName": "EXAMPLE 100% LIMITED", "legalJurisdiction": "GB", "registrationNumber": "01234567",
               "legalStatus": "Active", "legalAddress": {"country": "GB", "city": "LONDON"}, "industryCodes": ["62012"]}}
    gleif = {ID: {"directoryId": ID, "lei": "5493001KJTIIGC8Y1R12", "legalName": "EXAMPLE LTD", "legalJurisdiction": "GB"},
             "LECOZZ000000000000AA": {"directoryId": "LECOZZ000000000000AA", "lei": "529900T8BM49AURSDO55",
                                      "legalName": "<script>alert(1)</script> INC", "legalJurisdiction": "US-DE"}}
    for i in range(150):
        gb[f"LECOZZ0000000001{i:04d}"] = {"directoryId": f"LECOZZ0000000001{i:04d}", "legalName": f"MANY CO {i}",
                                         "legalJurisdiction": "GB"}
    return {"gb-coh": gb, "gleif": gleif}


@pytest.fixture(scope="module")
def snap(tmp_path_factory):
    out = tmp_path_factory.mktemp("snap")
    m = build(records(), out, "2026-11-07")
    return Snapshot(str(out / "*.parquet"), {"date": m["date"], "snapshotRoot": m["snapshotRoot"]})


def test_search_by_name_is_case_insensitive(snap):
    assert [r["directoryId"] for r in snap.search("example")] == [ID]


def test_search_by_lei_id_and_number(snap):
    for q in ("5493001KJTIIGC8Y1R12", ID, "01234567", ID.lower()):
        assert [r["directoryId"] for r in snap.search(q)] == [ID], q


def test_wildcards_in_the_query_are_literal(snap):
    assert [r["legalName"] for r in snap.search("100%")] == ["EXAMPLE 100% LIMITED"]
    # "%" finds only the name that contains a literal "%", not every record.
    assert [r["legalName"] for r in snap.search("%")] == ["EXAMPLE 100% LIMITED"] and snap.search("_") == []


def test_country_filter(snap):
    assert snap.search("inc", country="us")[0]["country"] == "US" and snap.search("inc", country="GB") == []


def test_limit_is_capped(snap):
    assert len(snap.search("MANY", limit=10_000)) == MAX_LIMIT and len(snap.search("MANY", limit=-5)) == 1


def test_injection_attempts_are_just_text(snap):
    assert snap.search("' OR 1=1 --") == [] and snap.search("x'; DROP TABLE e; --") == []
    assert snap.count == 152


def test_entity_decodes_nested_fields(snap):
    r = snap.entity(ID)
    assert r["lei"] == "5493001KJTIIGC8Y1R12" and r["legalAddress"]["city"] == "LONDON"
    assert r["industryCodes"] == ["62012"] and r["sources"] == ["gb-coh", "gleif"] and "filename" not in r


def test_unknown_entity(snap):
    assert snap.entity("NOPE") is None


def test_stats(snap):
    s = snap.stats()
    assert s["records"] == 152 and s["byCountry"][0] == {"value": "GB", "records": 151}


def fake_iap(token, audience):
    return {"owner": OWNER, "other": "other@nfh.test"}.get(token) if audience == AUD else None


@pytest.fixture
def server(snap):
    srv = make_server(snap, [OWNER], AUD, ("127.0.0.1", 0), iap=fake_iap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, token="owner", method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, headers={"X-Goog-IAP-JWT-Assertion": token} if token else {})
    r = c.getresponse()
    out = NS(status=r.status, body=r.read().decode(), headers={k.lower(): v for k, v in r.getheaders()})
    c.close()
    return out


@pytest.mark.parametrize("path", ["/", "/stats", "/entity/" + ID, "/api/v1/stats", "/api/v1/search?q=example",
                                  "/api/v1/entities/" + ID])
@pytest.mark.parametrize("token", [None, "", "forged", "other"])
def test_only_the_owner_gets_anything(server, path, token):
    r = get(server, path, token)
    assert r.status == 403 and "EXAMPLE" not in r.body and ID not in r.body


def test_owner_can_use_the_api(server):
    assert json.loads(get(server, "/api/v1/search?q=example").body)["results"][0]["directoryId"] == ID
    assert json.loads(get(server, "/api/v1/entities/" + ID).body)["lei"] == "5493001KJTIIGC8Y1R12"
    assert get(server, "/api/v1/entities/NOPE").status == 404
    assert json.loads(get(server, "/api/v1/stats").body)["records"] == 152


def test_owner_can_use_the_pages(server):
    assert ID in get(server, "/?q=example").body and "LONDON" in get(server, "/entity/" + ID).body
    assert "By country" in get(server, "/stats").body


def test_names_are_escaped(server):
    body = get(server, "/?q=alert").body
    assert "<script>alert" not in body and "&lt;script&gt;" in body


def test_every_page_says_it_is_private(server):
    assert "Private test instance" in get(server, "/").body


def test_security_headers_and_read_only(server):
    r = get(server, "/")
    assert r.headers["cache-control"] == "no-store" and "default-src 'none'" in r.headers["content-security-policy"]
    assert get(server, "/", method="POST").status == 405


def test_no_snapshot_yet():
    srv = make_server(None, [OWNER], AUD, ("127.0.0.1", 0), iap=fake_iap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert get(srv.server_address[1], "/").status == 503
        assert get(srv.server_address[1], "/", token=None).status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_health_reveals_nothing(server):
    r = get(server, "/health", token=None)
    assert r.status == 200 and r.body == "ok\n"
