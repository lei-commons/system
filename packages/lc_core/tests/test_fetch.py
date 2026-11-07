import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from lc_core.fetch import Denied, FetchError, Gateway


class FakeGateway(BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        target = self.headers.get("X-LC-Target")
        type(self).seen.append(dict(self.headers))
        status, headers, body = self.routes.get(target, (404, {}, b""))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def gw():
    FakeGateway.routes, FakeGateway.seen = {}, []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Gateway(f"http://127.0.0.1:{srv.server_address[1]}", "job:test", token=lambda aud: "tok")
    srv.shutdown()
    srv.server_close()


def test_get_json_and_identity_headers(gw):
    FakeGateway.routes["https://api.gleif.org/x"] = (200, {}, b'{"ok": 1}')
    assert gw.get_json("https://api.gleif.org/x") == {"ok": 1}
    h = FakeGateway.seen[0]
    assert h["X-Serverless-Authorization"] == "Bearer tok" and h["X-LC-Actor"] == "job:test"
    assert "Authorization" not in h


def test_download_streams_to_a_file(gw):
    body = b"x" * (3 * 1024 * 1024 + 7)
    FakeGateway.routes["https://goldencopy.gleif.org/f.zip"] = (200, {}, body)
    out = io.BytesIO()
    assert gw.download("https://goldencopy.gleif.org/f.zip", out) == len(body) and out.getvalue() == body


def test_redirects_are_followed_through_the_gateway(gw):
    FakeGateway.routes["https://a.test/1"] = (302, {"Location": "/2"}, b"")
    FakeGateway.routes["https://a.test/2"] = (200, {}, b"[]")
    assert gw.get_json("https://a.test/1") == []
    assert [s["X-LC-Target"] for s in FakeGateway.seen] == ["https://a.test/1", "https://a.test/2"]


def test_redirect_to_a_denied_place_is_denied(gw):
    FakeGateway.routes["https://a.test/1"] = (302, {"Location": "https://a.test/login"}, b"")
    FakeGateway.routes["https://a.test/login"] = (403, {"X-LC-Denied": "denied-path"}, json.dumps({"reason": "login"}).encode())
    with pytest.raises(Denied) as exc:
        gw.get_json("https://a.test/1")
    assert exc.value.rule == "denied-path"


def test_paused_system_raises_denied(gw):
    FakeGateway.routes["https://a.test/x"] = (503, {"X-LC-Denied": "system-not-running"}, b'{"reason": "paused"}')
    with pytest.raises(Denied) as exc:
        gw.get_json("https://a.test/x")
    assert (exc.value.status, exc.value.rule) == (503, "system-not-running")


def test_redirect_loop_stops(gw):
    FakeGateway.routes["https://a.test/loop"] = (302, {"Location": "/loop"}, b"")
    with pytest.raises(FetchError, match="redirects"):
        gw.get_json("https://a.test/loop")


def test_target_errors_raise(gw):
    with pytest.raises(FetchError, match="404"):
        gw.get_json("https://a.test/missing")


def test_unreachable_gateway_raises():
    with pytest.raises(FetchError, match="unreachable"):
        Gateway("http://127.0.0.1:9", "job:t", token=lambda a: "t", timeout=2).get_json("https://a.test/")


@pytest.mark.parametrize("url", ["", "ftp://x", "not a url"])
def test_bad_gateway_url_refused(url):
    with pytest.raises(ValueError):
        Gateway(url, "job:t")
