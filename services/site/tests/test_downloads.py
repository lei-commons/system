import http.client
import io
import json
import threading

import pytest

from lc_site.downloads import SnapshotFiles
from lc_site.server import make_server

MANIFEST = {"date": "2026-11-08", "records": 3, "relationships": 1, "snapshotRoot": "merkle:r",
            "decisionLog": {"day": "2026-11-07", "head": "h", "events": 4},
            "files": [{"name": "GB.parquet", "country": "GB", "records": 3, "sha256": "sha256:x"},
                      {"name": "relationships.parquet", "kind": "relationships", "records": 1}]}


class Listing(list):
    def __init__(self, items, prefixes):
        super().__init__(items)
        self.prefixes = prefixes


class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    @property
    def size(self):
        return len(self.bucket.objects[self.name])

    def exists(self):
        return self.name in self.bucket.objects

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def open(self, mode):
        return io.BytesIO(self.bucket.objects[self.name])


class Bucket:
    def __init__(self, objects):
        self.objects = objects

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name):
        return Blob(self, name) if name in self.objects else None

    def list_blobs(self, prefix="", delimiter=None):
        names = sorted(n for n in self.objects if n.startswith(prefix))
        if delimiter:
            return Listing([], sorted({n.split("/")[0] + "/" for n in names if "/" in n}))
        return Listing([Blob(self, n) for n in names], [])


BIG = bytes(range(256)) * 9000  # over two chunks


@pytest.fixture
def files():
    return SnapshotFiles(Bucket({"latest.json": b"{}", "2026-11-08/manifest.json": json.dumps(MANIFEST).encode(),
                                 "2026-11-08/GB.parquet": BIG, "2026-11-08/relationships.parquet": b"rel",
                                 "2026-11-08/NOTICE.txt": b"notice", "2026-11-08/other.bin": b"secret"}), ttl=0)


def test_lists_snapshots_with_their_files_and_sizes(files):
    snap = files.snapshots()["snapshots"][0]
    assert snap["date"] == "2026-11-08" and snap["decisionLog"]["head"] == "h"
    assert {f["name"]: f["bytes"] for f in snap["files"]} == {"GB.parquet": len(BIG), "relationships.parquet": 3,
                                                             "manifest.json": len(json.dumps(MANIFEST)), "NOTICE.txt": 6}


def test_only_listed_files_can_be_fetched(files):
    assert files.blob("2026-11-08", "GB.parquet") is not None and files.blob("2026-11-08", "NOTICE.txt") is not None
    for date, name in (("2026-11-08", "other.bin"), ("2026-11-08", "../latest.json"), ("../x", "GB.parquet"),
                       ("2026-11-09", "GB.parquet"), ("2026-11-08", "")):
        assert files.blob(date, name) is None, (date, name)


def test_downloads_stream_through_the_server_behind_the_login(files):
    class Snap:
        meta, count, relationship_count = {"date": "2026-11-08", "snapshotRoot": "r"}, 0, 0

    srv = make_server(Snap(), ["o@x.test"], "aud", ("127.0.0.1", 0), iap=lambda t, a: "o@x.test" if t == "ok" else None,
                      downloads=files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, token="ok"):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("GET", path, headers={"X-Goog-IAP-JWT-Assertion": token})
            r = c.getresponse()
            return r.status, r.getheader("Transfer-Encoding"), r.getheader("Content-Disposition"), r.read()

        status, encoding, disposition, body = get("/download/2026-11-08/GB.parquet")
        assert status == 200 and encoding == "chunked" and body == BIG
        assert disposition == 'attachment; filename="lei-commons-2026-11-08-GB.parquet"'
        assert get("/download/2026-11-08/other.bin")[0] == 404
        assert get("/download/2026-11-08/GB.parquet", token="bad")[0] == 403
        assert json.loads(get("/api/v1/snapshots")[3])["snapshots"][0]["records"] == 3
    finally:
        srv.shutdown()
        srv.server_close()
