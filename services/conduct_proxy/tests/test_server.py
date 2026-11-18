"""End-to-end through a real gateway socket, with a fake outbound side."""

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from conduct_proxy.policy import Policy
from conduct_proxy.server import Gate, Gateway, make_server
from conduct_proxy.upstream import BlockedAddress
from lc_core.control import SystemHalted

CONFIG = Path(__file__).resolve().parents[3] / "config" / "egress-allowlist.yaml"


class FakeResponse:
    def __init__(self, status, body=b"hello", headers=None):
        self.status, self._body = status, body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def getheader(self, name):
        return self._headers.get(name.lower())

    def read(self, n=-1):
        out, self._body = (self._body, b"") if n < 0 else (self._body[:n], self._body[n:])
        return out


class FakeConn:
    def close(self):
        pass


class FakeUpstream:
    def __init__(self):
        self.calls = []
        self.routes = {}
        self.error = None

    def open(self, scheme, host, port, method, target, headers, body=None):
        self.calls.append(dict(scheme=scheme, host=host, method=method, target=target, headers=headers, body=body))
        if self.error:
            raise self.error
        return FakeConn(), self.routes.get((host, target)) or FakeResponse(200, headers={
            "Content-Type": "text/plain", "Content-Length": "5", "Set-Cookie": "a=1", "X-Secret": "no"})

    def non_robots(self):
        return [c for c in self.calls if c["target"] != "/robots.txt"]


@pytest.fixture
def env():
    up, logs, sleeps = FakeUpstream(), [], []
    state = {"halted": None}

    def check():
        if state["halted"]:
            raise state["halted"]

    gw = Gateway(Policy.from_file(CONFIG, {"gb-coh"}), upstream=up, gate=Gate(check, clock=_tick),
                 log=logs.append, sleep=sleeps.append)
    srv = make_server(gw, ("127.0.0.1", 0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], up, logs, sleeps, state
    srv.shutdown()
    srv.server_close()


_t = [0.0]


def _tick():
    _t[0] += 1000  # every gate check is past the cache window
    return _t[0]


def call(port, target=None, method="GET", path="/fetch", headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = dict(headers or {})
    if target is not None:
        h["X-LC-Target"] = target
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    out = r.status, dict(r.getheaders()), data
    c.close()
    return out


def test_allowed_request_goes_out_and_set_cookie_comes_back_stripped(env):
    port, up, logs, _, _ = env
    status, headers, body = call(port, "https://api.gleif.org/x?page=2", headers={"Cookie": "s=1", "X-LC-Actor": "job:a"})
    assert (status, body) == (200, b"hello")
    assert "Set-Cookie" not in headers and "X-Secret" not in headers
    sent = up.calls[0]
    assert sent["target"] == "/x?page=2" and "cookie" not in sent["headers"]
    assert logs[-1]["actor"] == "job:a" and logs[-1]["allowed"] is True


def test_denied_request_never_reaches_upstream(env):
    port, up, logs, _, _ = env
    status, headers, body = call(port, "https://example.org/login")
    assert status == 403 and headers["X-LC-Denied"] == "denied-path"
    assert json.loads(body)["rule"] == "denied-path"
    assert up.calls == []
    assert logs[-1]["allowed"] is False


def test_log_never_contains_query_or_credentials(env):
    port, _, logs, _, _ = env
    call(port, "https://api.company-information.service.gov.uk/c?items=secretvalue", headers={"Authorization": "Basic secretvalue"})
    assert "secretvalue" not in json.dumps(logs)


def test_connect_is_refused(env):
    port, up, _, _, _ = env
    status, headers, _ = call(port, method="CONNECT", path="example.org:443")
    assert status == 403 and headers["X-LC-Denied"] == "not-a-gateway-request"
    assert up.calls == []


def test_absolute_uri_proxying_is_refused(env):
    port, up, _, _, _ = env
    assert call(port, path="http://example.org/")[0] == 403
    assert up.calls == []


def test_missing_target_denied(env):
    assert call(env[0])[0] == 403


def test_health_works_while_paused(env):
    port, _, _, _, state = env
    state["halted"] = SystemHalted("system is paused")
    assert call(port, path="/health")[0] == 200


def test_paused_system_lets_nothing_out(env):
    port, up, _, _, state = env
    state["halted"] = SystemHalted("system is paused; conduct-proxy will not run")
    status, headers, _ = call(port, "https://api.gleif.org/x")
    assert status == 503 and headers["X-LC-Denied"] == "system-not-running"
    assert up.calls == []


def test_control_check_crash_fails_closed(env):
    port, up, _, _, state = env
    state["halted"] = RuntimeError("bucket unreachable")
    assert call(port, "https://api.gleif.org/x")[0] == 503
    assert up.calls == []


def test_robots_disallow_blocks_crawler_request(env):
    port, up, _, _, _ = env
    up.routes[("example.org", "/robots.txt")] = FakeResponse(200, b"User-agent: *\nDisallow: /\n")
    status, headers, _ = call(port, "https://example.org/page")
    assert status == 403 and headers["X-LC-Denied"] == "robots-disallowed"
    assert up.non_robots() == []


def test_robots_redirect_is_followed_through_policy(env):
    port, up, _, _, _ = env
    up.routes[("example.org", "/robots.txt")] = FakeResponse(301, b"", {"Location": "https://www.example.org/robots.txt"})
    up.routes[("www.example.org", "/robots.txt")] = FakeResponse(200, b"User-agent: *\nDisallow: /\n")
    assert call(port, "https://example.org/page")[1]["X-LC-Denied"] == "robots-disallowed"


def test_robots_redirect_to_ip_is_not_fetched(env):
    port, up, _, _, _ = env
    up.routes[("example.org", "/robots.txt")] = FakeResponse(301, b"", {"Location": "http://169.254.169.254/robots.txt"})
    call(port, "https://example.org/page")
    assert all(c["host"] != "169.254.169.254" for c in up.calls)
    assert up.non_robots() == []


def test_registry_hosts_do_not_fetch_robots(env):
    port, up, _, _, _ = env
    call(port, "https://api.gleif.org/x")
    assert [c["target"] for c in up.calls] == ["/x"]


def test_requests_to_one_host_are_paced(env):
    port, _, _, sleeps, _ = env
    for _ in range(3):
        call(port, "https://api.gleif.org/x")
    assert sum(1 for s in sleeps if s > 0) >= 2


def test_crawl_delay_slows_host_beyond_one_second(env):
    port, up, _, sleeps, _ = env
    up.routes[("example.org", "/robots.txt")] = FakeResponse(200, b"User-agent: *\nCrawl-delay: 10\n")
    call(port, "https://example.org/a")
    call(port, "https://example.org/b")
    assert max(sleeps) > 5


def test_registry_hosts_go_at_a_persons_pace(env):
    """Each request after the first waits 5 to 15 seconds (config pacing), never back to back."""
    port, up, _, sleeps, _ = env
    for _ in range(4):
        assert call(port, "https://api.gleif.org/x")[0] == 200
    assert len(sleeps) == 3 and all(4.9 <= s <= 15.1 for s in sleeps), sleeps


def test_after_an_error_the_host_is_left_alone_for_longer(env):
    port, up, _, sleeps, _ = env
    up.routes[("api.gleif.org", "/x")] = FakeResponse(403, b"no", {"Content-Length": "2"})
    assert call(port, "https://api.gleif.org/x")[0] == 403
    up.routes[("api.gleif.org", "/x")] = FakeResponse(200, b"ok", {"Content-Length": "2"})
    assert call(port, "https://api.gleif.org/x")[0] == 200
    assert sleeps and sleeps[-1] >= 119


def test_a_long_retry_after_from_the_host_is_honoured_and_the_job_told_when_to_come_back(env):
    port, up, _, _, _ = env
    up.routes[("api.gleif.org", "/x")] = FakeResponse(429, b"slow down", {"Content-Length": "9", "Retry-After": "2000"})
    assert call(port, "https://api.gleif.org/x")[0] == 429  # the host's own answer
    status, headers, _ = call(port, "https://api.gleif.org/x")
    assert status == 429 and headers["X-LC-Denied"] == "rate-limited"
    assert 1990 <= int(headers["Retry-After"]) <= 2001  # longer than the queue: come back then


def test_hosts_are_paced_separately(env):
    port, up, _, sleeps, _ = env
    up.routes[("api.gleif.org", "/x")] = FakeResponse(403, b"no", {"Content-Length": "2"})
    assert call(port, "https://api.gleif.org/x")[0] == 403
    assert call(port, "https://goldencopy.gleif.org/x")[0] in (200, 404)
    assert not sleeps  # a different host isn't held back by api.gleif.org's pause


def test_redirects_are_returned_not_followed(env):
    port, up, _, _, _ = env
    up.routes[("api.gleif.org", "/x")] = FakeResponse(302, b"", {"Location": "https://example.org/login", "Content-Length": "0"})
    status, headers, _ = call(port, "https://api.gleif.org/x")
    assert status == 302 and headers["location"] == "https://example.org/login"
    assert len(up.calls) == 1


def test_post_body_reaches_approved_host(env):
    port, up, _, _, _ = env
    assert call(port, "https://api.company-information.service.gov.uk/q", "POST", body=b"{}")[0] == 200
    assert up.calls[0]["body"] == b"{}"


def test_non_public_address_reported_as_denial(env):
    port, up, logs, _, _ = env
    up.error = BlockedAddress("api.gleif.org resolves to non-public address 10.0.0.1")
    status, headers, _ = call(port, "https://api.gleif.org/x")
    assert status == 403 and headers["X-LC-Denied"] == "non-public-address"
    assert logs[-1]["rule"] == "non-public-address"


@pytest.mark.parametrize("error", [ConnectionRefusedError(), http.client.BadStatusLine("junk")])
def test_upstream_failure_is_502(env, error):
    port, up, logs, _, _ = env
    up.error = error
    assert call(port, "https://api.gleif.org/x")[0] == 502
    assert logs[-1]["rule"] == "upstream-error"


def test_failure_mid_stream_is_logged(env):
    port, up, logs, _, _ = env

    class Broken(FakeResponse):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"")

    up.routes[("api.gleif.org", "/x")] = Broken(200)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/fetch", headers={"X-LC-Target": "https://api.gleif.org/x"})
    assert c.getresponse().status == 200
    c.close()
    for _ in range(100):
        if logs:
            break
        time.sleep(0.01)
    assert logs[-1]["rule"] == "stream-failed"


def test_bodies_go_back_chunked_for_cloud_runs_size_cap(env):
    port, up, _, _, _ = env
    big = bytes(range(256)) * 4096  # 1 MiB, several gateway chunks
    up.routes[("api.gleif.org", "/big")] = FakeResponse(200, big, {"Content-Length": str(len(big))})
    status, headers, body = call(port, "https://api.gleif.org/big")
    assert status == 200 and body == big
    assert headers.get("Transfer-Encoding") == "chunked" and "Content-Length" not in headers


def test_head_keeps_the_length_and_sends_no_body(env):
    port, up, _, _, _ = env
    up.routes[("api.gleif.org", "/f")] = FakeResponse(200, b"", {"Content-Length": "485000000"})
    status, headers, body = call(port, "https://api.gleif.org/f", method="HEAD")
    assert status == 200 and headers.get("Content-Length") == "485000000" and body == b""


def test_empty_body_is_a_valid_chunked_response(env):
    port, up, _, _, _ = env
    up.routes[("api.gleif.org", "/empty")] = FakeResponse(204, b"")
    assert call(port, "https://api.gleif.org/empty")[2] == b""


# ---- the browser profile (lc_browser): owner-approved form sites ----

NTA = "https://www.houjin-bangou.nta.go.jp"


class CookieMsg:
    def __init__(self, cookies):
        self.cookies = cookies

    def get_all(self, name, default=None):
        return self.cookies if name.lower() == "set-cookie" else default


def test_a_browser_host_gets_the_form_post_with_its_session_cookie_and_its_cookies_come_back(env):
    port, up, _, _, _ = env
    resp = FakeResponse(200, b"PK..", {"Content-Type": "application/octet-stream",
                                       "Content-Disposition": "attachment; filename*=utf-8'jp'00_zenkoku_all_20261030.zip"})
    resp.msg = CookieMsg(["JSESSIONID=abc; Path=/; Secure; HttpOnly", "AWSALB=x; Expires=Thu, 25 Nov 2026 05:15:55 GMT; Path=/"])
    up.routes[("www.houjin-bangou.nta.go.jp", "/download/zenken/index.html")] = resp
    status, headers, body = call(port, f"{NTA}/download/zenken/index.html", "POST", body=b"event=download&selDlFileNo=28341",
                                 headers={"Cookie": "JSESSIONID=abc", "Content-Type": "application/x-www-form-urlencoded",
                                          "Referer": f"{NTA}/download/zenken/", "User-Agent": "Mozilla/5.0 Chrome/131"})
    assert status == 200 and headers["content-disposition"].endswith("00_zenkoku_all_20261030.zip")
    sent = [c for c in up.non_robots()][-1]
    assert sent["method"] == "POST" and sent["body"] == b"event=download&selDlFileNo=28341"
    assert sent["headers"]["cookie"] == "JSESSIONID=abc" and sent["headers"]["referer"].startswith(NTA)
    assert sent["headers"]["user-agent"].startswith("Mozilla/5.0") and "lei-commons-browser" in sent["headers"]["user-agent"]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/fetch", headers={"X-LC-Target": f"{NTA}/download/zenken/index.html"})
    r = c.getresponse()
    assert r.msg.get_all("Set-Cookie") == resp.msg.cookies  # each cookie whole, dates and all
    r.read()
    c.close()


def test_a_browser_host_honours_robots_and_still_refuses_login_paths_and_credentials(env):
    port, up, _, _, _ = env
    up.routes[("www.houjin-bangou.nta.go.jp", "/robots.txt")] = FakeResponse(200, b"User-agent: *\nDisallow: /private/\n")
    assert call(port, f"{NTA}/private/x")[1]["X-LC-Denied"] == "robots-disallowed"
    assert call(port, f"{NTA}/login/")[1]["X-LC-Denied"] == "denied-path"
    assert call(port, f"{NTA}/x", headers={"Authorization": "Bearer t"})[1]["X-LC-Denied"] == "credentials-not-allowed"


def test_registry_hosts_still_get_no_cookies_and_no_posts(env):
    port, up, _, _, _ = env
    status, headers, _ = call(port, "https://api.gleif.org/x", "POST", body=b"x")
    assert status == 403 and headers["X-LC-Denied"] == "method-not-allowed"
    status, headers, _ = call(port, "https://api.gleif.org/x", headers={"Cookie": "a=1"})
    assert "cookie" not in up.calls[-1]["headers"] and "Set-Cookie" not in headers
