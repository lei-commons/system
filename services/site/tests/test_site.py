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


RELS = {"REL1": {"directoryId": "REL1", "startNode": "529900T8BM49AURSDO55", "endNode": "5493001KJTIIGC8Y1R12",
                 "relationshipType": "IS_DIRECTLY_CONSOLIDATED_BY", "relationshipStatus": "ACTIVE", "percentage": 60.0},
        "REL2": {"directoryId": "REL2", "startNode": "5493001KJTIIGC8Y1R12", "endNode": "NOTINDIRECTORY000000",
                 "relationshipType": "IS_ULTIMATELY_CONSOLIDATED_BY", "relationshipStatus": "ACTIVE"}}


@pytest.fixture(scope="module")
def snap(tmp_path_factory):
    from lc_publish.shards import shard_of
    from lc_publish.snapshot import build_sharded

    out = tmp_path_factory.mktemp("snap")
    recs = records()
    readers = {sid: (lambda n, r=r: {k: v for k, v in r.items() if shard_of(k) == n}) for sid, r in recs.items()}
    m = build_sharded(readers, out, "2026-11-07", relationships=lambda n: {k: v for k, v in RELS.items() if shard_of(k) == n})
    entity_files = [str(out / f["name"]) for f in m["files"] if f.get("kind") != "relationships"]
    return Snapshot(entity_files, {"date": m["date"], "snapshotRoot": m["snapshotRoot"]}, [str(out / "relationships.parquet")])


def ids(found):
    return [r["directoryId"] for r in found["results"]]


def test_search_by_name_is_ranked_and_case_insensitive(snap):
    assert ids(snap.search("example")) == [ID]


def test_search_by_lei_id_and_number(snap):
    for q in ("5493001KJTIIGC8Y1R12", ID, "01234567", ID.lower()):
        assert ids(snap.search(q)) == [ID], q


def test_exact_name_ranks_first():
    pass  # covered at scale by the ordering rule; see test_order_prefers_exact_then_prefix


def test_order_prefers_exact_then_prefix(snap):
    names = [r["legalName"] for r in snap.search("many co 1", limit=5)["results"]]
    assert names[0] == "MANY CO 1"


def test_country_filter_and_facets(snap):
    found = snap.search("inc", country="us")
    assert found["results"][0]["country"] == "US" and snap.search("inc", country="GB")["total"] == 0
    assert {"value": "GB", "records": 150} in snap.search("many")["facets"]["country"]


def test_source_filter(snap):
    assert ids(snap.search("example", source="gleif")) == [ID] and snap.search("many", source="gleif")["total"] == 0


def test_paging(snap):
    first, second = snap.search("many", limit=100), snap.search("many", limit=100, page=2)
    assert first["total"] == 150 and len(first["results"]) == 100 and len(second["results"]) == 50
    assert not set(ids(first)) & set(ids(second))


def test_limit_and_page_are_capped(snap):
    assert len(snap.search("MANY", limit=10_000)["results"]) == MAX_LIMIT and len(snap.search("MANY", limit=-5)["results"]) == 1
    assert snap.search("many", page=10_000)["results"] == []


def test_injection_attempts_are_just_text(snap):
    assert snap.search("' OR 1=1 --")["total"] == 0 and snap.search("x'; DROP TABLE e; --")["total"] == 0
    assert snap.search("example", country="X' OR '1'='1")["total"] == 0
    assert snap.count == 152


def test_relationships_both_ways_with_names(snap):
    rel = snap.relationships(ID)
    (child,) = rel["down"]
    assert child["role"] == "direct subsidiary" and child["otherName"] == "<script>alert(1)</script> INC"
    (parent,) = rel["up"]
    assert parent["role"] == "ultimate parent" and parent["otherId"] is None and parent["otherLei"] == "NOTINDIRECTORY000000"
    assert snap.relationship_count == 2


def test_entity_decodes_nested_fields(snap):
    r = snap.entity(ID)
    assert r["lei"] == "5493001KJTIIGC8Y1R12" and r["legalAddress"]["city"] == "LONDON"
    assert r["industryCodes"] == ["62012"] and r["sources"] == ["gb-coh", "gleif"] and "filename" not in r


def test_unknown_entity(snap):
    assert snap.entity("NOPE") is None


def test_stats(snap):
    s = snap.stats()
    assert s["records"] == 152 and s["byCountry"][0] == {"value": "GB", "records": 151} and s["relationships"] == 2


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


PATHS = ["/", "/index.html", "/static/app.js", "/static/views/graph.js", "/static/vendor/cytoscape-3.34.4.esm.min.js",
         "/api/v1/meta", "/api/v1/stats", "/api/v1/search?q=example", "/api/v1/suggest?q=ex", "/api/v1/entities/" + ID,
         "/api/v1/entities/" + ID + "/relationships", "/api/v1/graph/" + ID]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("token", [None, "", "forged", "other"])
def test_only_the_owner_gets_anything(server, path, token):
    r = get(server, path, token)
    assert r.status == 403 and "EXAMPLE" not in r.body and ID not in r.body and "cytoscape" not in r.body.lower()


@pytest.mark.parametrize("path", PATHS)
def test_owner_gets_everything(server, path):
    assert get(server, path).status == 200


def test_app_shell_and_assets_have_the_right_types(server):
    assert "text/html" in get(server, "/").headers["content-type"]
    assert get(server, "/static/app.js").headers["content-type"].startswith("text/javascript")
    assert get(server, "/static/app.css").headers["content-type"].startswith("text/css")
    assert "/static/app.js" in get(server, "/").body


@pytest.mark.parametrize("path", ["/static/../server.py", "/static/../../tests/test_site.py", "/static/%2e%2e/server.py",
                                  "/static/", "/static/nope.js", "/static/vendor/README.md/../../server.py", "/server.py", "/entity/x"])
def test_only_listed_files_are_served(server, path):
    r = get(server, path)
    assert r.status == 404 and "import" not in r.body


def test_content_policy_allows_only_our_own_scripts(server):
    csp = get(server, "/").headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe" not in csp and "default-src 'none'" in csp
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert get(server, "/").headers["cache-control"] == "no-store"


def test_vendored_library_matches_its_recorded_checksum():
    import hashlib
    from pathlib import Path

    static = Path(__file__).resolve().parents[1] / "lc_site" / "static" / "vendor"
    digest = hashlib.sha256((static / "cytoscape-3.34.4.esm.min.js").read_bytes()).hexdigest()
    assert digest == "0d7e373a706f0f01d6fb7a6ccf04988c01307cee1c2f9bcc7b068e9f6b431e3f"
    assert digest in (static / "README.md").read_text()


def test_api_meta_suggest_and_search(server):
    assert json.loads(get(server, "/api/v1/meta").body)["records"] == 152
    names = [r["legalName"] for r in json.loads(get(server, "/api/v1/suggest?q=many%20co%201").body)["results"]]
    assert names[0] == "MANY CO 1" and all(n.startswith("MANY CO 1") for n in names)
    assert json.loads(get(server, "/api/v1/suggest?q=m").body)["results"] == []
    assert json.loads(get(server, "/api/v1/search?q=example").body)["results"][0]["directoryId"] == ID


def test_api_entity_and_relationships(server):
    assert json.loads(get(server, "/api/v1/entities/" + ID).body)["lei"] == "5493001KJTIIGC8Y1R12"
    rel = json.loads(get(server, "/api/v1/entities/" + ID + "/relationships").body)
    assert rel["down"][0]["role"] == "direct subsidiary"
    assert get(server, "/api/v1/entities/NOPE").status == 404 and get(server, "/api/v1/entities/NOPE/relationships").status == 404


def test_api_graph_neighbourhood(server):
    g = json.loads(get(server, "/api/v1/graph/" + ID).body)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert g["focus"] == ID and len(g["edges"]) == 2 and not g["truncated"]
    assert nodes["LECOZZ000000000000AA"]["inDirectory"] and nodes["LEI:NOTINDIRECTORY000000"]["inDirectory"] is False
    child = next(e for e in g["edges"] if e["type"] == "IS_DIRECTLY_CONSOLIDATED_BY")
    assert (child["source"], child["target"]) == ("LECOZZ000000000000AA", ID)
    assert get(server, "/api/v1/graph/NOPE").status == 404


def test_graph_caps_large_neighbourhoods(snap):
    g = snap.graph(ID, per_node=1)
    assert len(g["edges"]) == 1 and g["truncated"]
    assert snap.graph(ID, depth=99)["focus"] == ID


def test_names_come_back_as_data_not_markup(server):
    body = get(server, "/api/v1/search?q=script").body
    assert json.loads(body)["results"][0]["legalName"] == "<script>alert(1)</script> INC"


def test_unknown_api_path(server):
    assert get(server, "/api/v1/nope").status == 404


def test_read_only(server):
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert get(server, "/api/v1/stats", method=method).status == 405


def test_no_snapshot_yet():
    srv = make_server(None, [OWNER], AUD, ("127.0.0.1", 0), iap=fake_iap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert get(srv.server_address[1], "/api/v1/stats").status == 503
        assert get(srv.server_address[1], "/").status == 200
        assert get(srv.server_address[1], "/api/v1/stats", token=None).status == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_health_reveals_nothing(server):
    r = get(server, "/health", token=None)
    assert r.status == 200 and r.body == "ok\n"


class FakeDirectory:
    def __init__(self, count):
        self.count = count


@pytest.mark.parametrize("env,database,expected", [
    ({}, None, "snapshot"),
    ({"LC_DB_HOST": "10.0.0.1"}, lambda: FakeDirectory(4_189_860), "database"),
    ({"LC_DB_HOST": "10.0.0.1"}, lambda: FakeDirectory(0), "snapshot"),
    ({"LC_DB_HOST": "10.0.0.1"}, lambda: (_ for _ in ()).throw(TimeoutError("no route")), "snapshot"),
])
def test_site_uses_the_database_only_when_it_is_loaded_and_reachable(monkeypatch, env, database, expected):
    from lc_site.server import choose_directory

    monkeypatch.delenv("LC_DB_HOST", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    chosen = choose_directory("bucket", open_database=database, load_snapshot=lambda: "snapshot")
    assert ("database" if isinstance(chosen, FakeDirectory) else chosen) == expected
