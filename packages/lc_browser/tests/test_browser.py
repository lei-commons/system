"""The browser path end to end, offline: a real headless Chromium, the real relay, and a fake gateway that
plays a form-download site (a one-time token, a session cookie, a file behind a form post)."""

import http.server
import io
import json
import threading
import zipfile

import pytest

from lc_browser.relay import Relay
from lc_browser.runner import AccessControl, Steps, download
from lc_core.fetch import Gateway

pytest.importorskip("playwright.sync_api")

SITE = "www.form-site.test"
PAGE = f"https://{SITE}/download/"
ZIP = io.BytesIO()
with zipfile.ZipFile(ZIP, "w") as z:
    z.writestr("all.csv", "1,ACME\n")
ZIP = ZIP.getvalue()

FORM = """<!doctype html><html><head><title>Downloads</title></head><body>
<img src="/big-banner.png"><script src="https://other.test/tracker.js"></script>
<form id="f" action="/download/index.html" method="post">
<input type="hidden" name="token" value="TOKEN-1"><input type="hidden" name="event" value="download">
<input type="hidden" name="no" id="no"></form>
<h2>CSV (Shift_JIS)</h2><a href="#" onclick="return dl(1);">all</a>
<h2>CSV (Unicode)</h2><a href="#" onclick="return dl(2);">all</a>
<script>function dl(n){document.getElementById('no').value=n;document.getElementById('f').submit();return false;}</script>
%s</body></html>"""


class FakeGateway(http.server.BaseHTTPRequestHandler):
    """Answers /fetch as the conduct gateway would, for one fake site."""
    seen = []
    extra = ""

    def log_message(self, *a):
        pass

    def _answer(self, status, body=b"", headers=()):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        target = self.headers.get("X-LC-Target", "")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.seen.append({"method": self.command, "target": target, "cookie": self.headers.get("Cookie"),
                          "ua": self.headers.get("User-Agent"), "body": body.decode()})
        if not target.startswith(f"https://{SITE}/"):
            return self._answer(403, b'{"denied": true}', [("X-LC-Denied", "not-allowed")])
        if self.command == "GET" and target == PAGE:
            return self._answer(200, (FORM % self.extra).encode(), [("Content-Type", "text/html; charset=utf-8"),
                                                                    ("Set-Cookie", "SID=s1; Path=/; Secure; HttpOnly")])
        if self.command == "POST" and target == f"https://{SITE}/download/index.html":
            ok = "token=TOKEN-1" in body.decode() and "no=2" in body.decode() and "SID=s1" in (self.headers.get("Cookie") or "")
            if not ok:
                return self._answer(400, b"bad form")
            return self._answer(200, ZIP, [("Content-Type", "application/octet-stream"),
                                           ("Content-Disposition", "attachment; filename=all_20261030.zip")])
        return self._answer(404, b"no")

    do_GET = do_POST = _handle


@pytest.fixture
def gateway():
    FakeGateway.seen, FakeGateway.extra = [], ""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield Gateway(f"http://127.0.0.1:{srv.server_address[1]}", "job:test", token=lambda a: "t", sleep=lambda s: None)
    srv.shutdown()


def flow(page, step, timeout_ms):
    step(page, "open the page")
    page.goto(PAGE, wait_until="domcontentloaded")
    step(page, "pick the Unicode file")
    no = page.evaluate("""() => { const hs = [...document.querySelectorAll('h2')];
        const h = hs.find(x => x.textContent.includes('Unicode'));
        let el = h; while ((el = el.nextElementSibling)) { const a = el.matches('[onclick^="return dl("]') ? el : null;
          if (a) return a.getAttribute('onclick').match(/dl\\((\\d+)\\)/)[1]; } return null; }""")
    with page.expect_download(timeout=timeout_ms) as info:
        page.click(f'[onclick="return dl({no});"]')
    return info.value


def test_the_browser_posts_the_form_with_its_cookie_and_gets_the_file_through_the_gateway(gateway):
    out, events = io.BytesIO(), []
    size = download(flow, gateway, out, [SITE], log=events.append, sleep=lambda s: None, timeout_s=60)
    assert size == len(ZIP) and out.getvalue() == ZIP
    seen = FakeGateway.seen
    post = next(s for s in seen if s["method"] == "POST")
    assert "no=2" in post["body"] and "SID=s1" in post["cookie"]  # the site's own session cookie came back
    assert post["ua"].startswith("Mozilla/")  # the browser's own; the gateway appends lei-commons-browser
    assert not any("banner.png" in s["target"] for s in seen)  # images are never fetched
    assert not any("other.test" in s["target"] for s in seen)  # another host never leaves the container
    done = [e for e in events if e["event"] == "browser-downloaded"][0]
    assert done["file"] == "all_20261030.zip" and done["refused"] >= 1
    assert [e["step"] for e in events if e["event"] == "browser-step"] == ["open the page", "pick the Unicode file"]


def test_a_captcha_or_login_stops_the_run_as_a_refusal(gateway):
    FakeGateway.extra = '<div class="g-recaptcha" data-sitekey="x"></div>'
    with pytest.raises(AccessControl) as raised:
        download(flow, gateway, io.BytesIO(), [SITE], log=lambda e: None, sleep=lambda s: None, timeout_s=60)
    assert raised.value.status == 403  # recorded as the source refusing us (lc_core.decisions)
    assert not any(s["method"] == "POST" for s in FakeGateway.seen)  # nothing was submitted
    FakeGateway.extra = '<form><input type="password" name="pw"></form>'
    with pytest.raises(AccessControl):
        download(flow, gateway, io.BytesIO(), [SITE], log=lambda e: None, sleep=lambda s: None, timeout_s=60)


def test_steps_pause_like_a_person_between_steps():
    class Page:
        def query_selector(self, s):
            return None

        def title(self):
            return "Downloads"

    waits = []
    steps = Steps(log=lambda e: None, sleep=waits.append, rand=lambda a, b: (a + b) / 2)
    for what in ("a", "b", "c"):
        steps(Page(), what)
    assert waits == [10.0, 10.0]  # none before the first step


def test_the_relay_refuses_other_hosts_and_plain_http(gateway):
    import http.client

    relay = Relay(gateway, [SITE], log=lambda e: None).start()
    try:
        port = int(relay.proxy.rsplit(":", 1)[1])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("CONNECT", "evil.test:443")
        assert c.getresponse().status == 403
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", f"http://{SITE}/x")
        assert c.getresponse().status == 403
        assert relay.refused == 1
    finally:
        relay.stop()
