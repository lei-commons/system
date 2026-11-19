"""The site serves the DeDi files and what they point to, only those, behind the owner's login, with DeDi's
headers; and reports their state for the System page's card, including when publishing or the check failed."""

import http.client
import json
import threading

import pytest

from lc_site.dedi_files import DediFiles, object_for
from lc_site.server import make_server
from test_downloads import Bucket

SHA = "a" * 64
MANIFEST = {"domain": "lei-commons.example", "keys": [{"kid": "lc-dedi-1"}], "updated_at": "2026-11-19T04:00:00Z",
            "next_update": "2026-11-26T04:00:00Z"}
STATUS = {"ok": True, "publishedAt": "2026-11-19T04:00:00Z", "date": "2026-11-19", "attempts": 1,
          "files": {"countries": {"records": 4, "updatedAt": "2026-11-19T04:00:00Z", "nextUpdate": "2026-11-20T16:00:00Z",
                                  "bytes": 900, "digest": "sha-256:x", "builtFrom": "2026-11-19"},
                    "data-sources": {"records": 24, "updatedAt": "2026-11-18T04:00:00Z", "nextUpdate": "2026-11-20T16:00:00Z"}}}


def bucket(**over):
    objects = {"dedi/dedi.index.json": json.dumps(MANIFEST).encode(), "dedi/dedi.countries.json": b'{"x":1}',
               "dedi/dedi.digests.json": b"{}", "dedi/schemas/country-snapshot.v1.json": b"{}",
               "dedi/status.json": json.dumps(STATUS).encode(), f"parts/{SHA}.parquet": b"PAR1",
               "2026-11-17/manifest.json": b"{}", "2026-11-17/NOTICE.txt": b"n", "dedi/secret.json": b"no",
               "latest.json": b"{}", "2026-11-17/changes.000.parquet": b"c"}
    objects.update(over)
    return Bucket({k: v for k, v in objects.items() if v is not None})


def test_only_dedi_paths_map_to_objects():
    ok = {"/.well-known/dedi.index.json": "dedi/dedi.index.json", "/dedi/dedi.countries.json": "dedi/dedi.countries.json",
          "/dedi/schemas/country-snapshot.v1.json": "dedi/schemas/country-snapshot.v1.json",
          f"/data/parts/{SHA}.parquet": f"parts/{SHA}.parquet", "/data/2026-11-17/manifest.json": "2026-11-17/manifest.json",
          "/data/2026-11-17/manifest.json.sigstore.json": "2026-11-17/manifest.json.sigstore.json",
          "/data/2026-11-17/NOTICE.txt": "2026-11-17/NOTICE.txt"}
    for path, obj in ok.items():
        assert object_for(path) == obj, path
    for path in ("/dedi/secret.json", "/dedi/status.json", "/dedi/../latest.json", "/data/latest.json",
                 "/data/parts/abc.parquet", "/data/2026-11-17/changes.000.parquet", "/dedi/dedi.x.json/..",
                 "/.well-known/other.json", "/dedi/schemas/x.json", "/data/2026-11-17/../../latest.json", ""):
        assert object_for(path) is None, path


@pytest.fixture
def server():
    class Snap:
        meta, count, relationship_count = {"date": "2026-11-19", "snapshotRoot": "r"}, 0, 0

    holder = {}

    def start(dedi):
        srv = make_server(Snap(), ["o@x.test"], "aud", ("127.0.0.1", 0), iap=lambda t, a: "o@x.test" if t == "ok" else None,
                          dedi=dedi)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        holder["srv"] = srv

        def get(path, token="ok"):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("GET", path, headers={"X-Goog-IAP-JWT-Assertion": token})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        return get

    yield start
    if "srv" in holder:
        holder["srv"].shutdown()
        holder["srv"].server_close()


def test_serves_dedi_files_with_dedi_headers_behind_the_login(server):
    get = server(DediFiles(bucket(), ttl=0))
    status, headers, body = get("/.well-known/dedi.index.json")
    assert status == 200 and json.loads(body)["domain"] == "lei-commons.example"
    assert headers["Content-Type"] == "application/json" and headers["Access-Control-Allow-Origin"] == "*"
    assert headers["Cache-Control"] == "public, max-age=300"
    status, headers, body = get(f"/data/parts/{SHA}.parquet")
    assert status == 200 and body == b"PAR1" and "immutable" in headers["Cache-Control"]
    assert get("/dedi/dedi.countries.json")[2] == b'{"x":1}'
    assert get("/dedi/secret.json")[0] == 404 and get("/dedi/dedi.missing.json")[0] == 404
    assert get("/.well-known/dedi.index.json", token="bad")[0] == 403  # private until the owner says otherwise


def test_without_a_bucket_the_routes_answer_503(server):
    get = server(None)
    assert get("/.well-known/dedi.index.json")[0] == 503 and get("/api/v1/dedi")[0] == 503


def test_status_lists_every_file_with_its_link(server):
    get = server(DediFiles(bucket(), ttl=0))
    d = json.loads(get("/api/v1/dedi")[2])
    assert d["state"] == "ok" and d["domain"] == "lei-commons.example"
    assert [(f["label"], f["path"]) for f in d["files"]] == [
        ("Index (manifest)", "/.well-known/dedi.index.json"), ("Countries", "/dedi/dedi.countries.json"),
        ("Data sources", "/dedi/dedi.data-sources.json"), ("Digests", "/dedi/dedi.digests.json")]
    assert d["files"][1]["records"] == 4 and d["files"][0]["keys"] == ["lc-dedi-1"]


def test_status_shows_failures_honestly():
    failed = {"ok": False, "failedAt": "2026-11-20T04:00:00Z", "error": "PublishError: KMS unavailable",
              "lastGood": "2026-11-19T04:00:00Z"}
    d = DediFiles(bucket(**{"dedi/status.json": json.dumps(failed).encode()}), ttl=0).status()
    assert d["state"] == "failing" and d["published"]["lastGood"] == "2026-11-19T04:00:00Z"
    assert [f["path"] for f in d["files"]] == ["/.well-known/dedi.index.json", "/dedi/dedi.digests.json"]
    bad_check = {"ok": False, "checkedAt": "2026-11-20T06:00:00Z", "problems": ["countries: stale"]}
    d = DediFiles(bucket(**{"dedi/check.json": json.dumps(bad_check).encode()}), ttl=0).status()
    assert d["state"] == "failing"
    nothing = DediFiles(bucket(**{"dedi/status.json": None, "dedi/dedi.index.json": None}), ttl=0).status()
    assert nothing["state"] == "unpublished" and nothing["files"][0]["present"] is False
    unreadable = DediFiles(bucket(**{"dedi/status.json": b"{oops"}), ttl=0).status()
    assert unreadable["state"] == "failing"
