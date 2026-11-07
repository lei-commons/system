"""End to end through a real socket, with a fake IAP and secret store."""

import http.client
import threading
from types import SimpleNamespace as NS

import pytest

from lc_console.auth import IAP_ISSUER, SESSION_SECONDS, Sessions, verify_iap
from lc_console.server import COOKIE, PENDING, App, make_server
from lc_console.totp import Verifier, code_at, new_secret

AUD = "/projects/1/locations/us-central1/services/owner-console"
OWNER = "owner@nfh.test"


class Store:
    def __init__(self, value=None):
        self.value, self.error = value, None

    def get(self):
        if self.error:
            raise self.error
        return self.value

    def put(self, value):
        self.value = value


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


def fake_iap(token, audience):
    return {"owner": OWNER, "other": "other@nfh.test"}.get(token) if audience == AUD else None


@pytest.fixture
def env():
    clock, logs = Clock(), []
    store = Store(new_secret())
    state = {"read": lambda: NS(state="paused", paused_components=(), monthly_budget_usd=1150.0)}
    app = App(owners=[OWNER], audience=AUD, store=store, read_state=lambda: state["read"](), sessions=Sessions(clock=clock),
              verifier=Verifier(clock), iap=fake_iap, log=logs.append)
    srv = make_server(app, ("127.0.0.1", 0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield NS(port=srv.server_address[1], store=store, clock=clock, logs=logs, app=app, state=state)
    srv.shutdown()
    srv.server_close()


def call(e, path="/", method="GET", token="owner", cookie=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", e.port, timeout=5)
    h = {}
    if token:
        h["X-Goog-IAP-JWT-Assertion"] = token
    if cookie:
        h["Cookie"] = f"{COOKIE}={cookie}"
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read().decode()
    out = NS(status=r.status, body=data, headers={k.lower(): v for k, v in r.getheaders()})
    c.close()
    return out


def code(e, offset=0):
    return code_at(e.store.value, int(e.clock.t) // 30 + offset)


def login(e):
    r = call(e, "/verify", "POST", body=f"code={code(e)}")
    assert r.status == 303
    return r.headers["set-cookie"].split(";")[0].split("=", 1)[1]


@pytest.mark.parametrize("token", [None, "", "forged", "other"])
@pytest.mark.parametrize("path,method", [("/", "GET"), ("/verify", "POST"), ("/enrol", "POST")])
def test_no_iap_owner_no_entry(env, token, path, method):
    r = call(env, path, method, token=token, body="code=123456" if method == "POST" else None)
    assert r.status == 403 and "paused" not in r.body


def test_valid_session_is_useless_without_iap(env):
    cookie = login(env)
    assert call(env, "/", token=None, cookie=cookie).status == 403


def test_session_belongs_to_one_person(env):
    env.app.owners.add("other@nfh.test")
    cookie = login(env)
    r = call(env, "/", token="other", cookie=cookie)
    assert "paused" not in r.body


def test_iap_alone_does_not_show_status(env):
    r = call(env)
    assert r.status == 200 and "Second factor" in r.body and "paused" not in r.body


def test_both_factors_show_status(env):
    r = call(env, cookie=login(env))
    assert r.status == 200 and "paused" in r.body and "1150" in r.body


def test_cookie_flags(env):
    r = call(env, "/verify", "POST", body=f"code={code(env)}")
    c = r.headers["set-cookie"]
    assert c.startswith("__Host-") and all(f in c for f in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"))


def test_wrong_code_gets_no_cookie(env):
    bad = "000000" if code(env) != "000000" else "111111"
    r = call(env, "/verify", "POST", body=f"code={bad}")
    assert r.status == 403 and "set-cookie" not in r.headers


def test_session_expires_after_an_hour(env):
    cookie = login(env)
    env.clock.t += SESSION_SECONDS + 1
    assert "paused" not in call(env, cookie=cookie).body


@pytest.mark.parametrize("cookie", ["junk", "a.b", ".", "aGVsbG8=.bad"])
def test_forged_cookies_refused(env, cookie):
    assert "paused" not in call(env, cookie=cookie).body


def test_tampered_cookie_refused(env):
    cookie = login(env)
    assert "paused" not in call(env, cookie="A" + cookie[1:]).body


def test_other_keys_cookie_refused(env):
    assert "paused" not in call(env, cookie=Sessions(clock=env.clock).issue(OWNER)).body


def test_login_failures_are_audited_without_the_code(env):
    call(env, "/verify", "POST", body="code=999999")
    assert env.logs[-1]["event"] == "second-factor" and env.logs[-1]["ok"] is False
    assert "999999" not in str(env.logs)


def pending_key(e):
    assert e.store.value.startswith(PENDING)
    return e.store.value[len(PENDING):]


def test_enrolment_needs_iap(env):
    env.store.value = None
    assert call(env, token=None).status == 403
    assert env.store.value is None


def test_enrolment_shows_a_stored_pending_key_and_a_qr_code(env):
    env.store.value = None
    r = call(env)
    assert "Set up your authenticator" in r.body and pending_key(env) in r.body
    assert "data:image/svg+xml" in r.body and "img-src data:" in r.headers["content-security-policy"]


def test_key_survives_reloads_and_a_restart(env):
    """The bug that blocked the first enrolment: a restart replaced the key."""
    env.store.value = None
    call(env)
    first = pending_key(env)
    call(env)
    assert pending_key(env) == first
    restarted = App(owners=[OWNER], audience=AUD, store=env.store, iap=fake_iap, log=lambda r: None)
    srv = make_server(restarted, ("127.0.0.1", 0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    r = call(NS(port=srv.server_address[1]))
    srv.shutdown()
    srv.server_close()
    assert first in r.body and pending_key(env) == first


def test_pending_key_gives_no_access_without_a_good_code(env):
    env.store.value = None
    call(env)
    r = call(env, "/enrol", "POST", body="code=abc")
    assert r.status == 403 and "set-cookie" not in r.headers
    assert env.store.value.startswith(PENDING)
    assert call(env, "/verify", "POST", body="code=123456").status == 404


def test_wrong_code_keeps_the_same_key_on_screen(env):
    env.store.value = None
    call(env)
    first = pending_key(env)
    r = call(env, "/enrol", "POST", body="code=abc")
    assert first in r.body and "Enter the code your app shows now" in r.body


def test_enrolment_tolerates_a_slow_phone_clock(env):
    env.store.value = None
    call(env)
    good = code_at(pending_key(env), int(env.clock.t) // 30 - 3)
    assert call(env, "/enrol", "POST", body=f"code={good}").status == 303


def test_lockout_is_explained_during_enrolment(env):
    env.store.value = None
    call(env)
    for _ in range(5):
        r = call(env, "/enrol", "POST", body="code=abc")
    assert "Wait five minutes" in r.body


def test_enrolment_confirms_the_key_after_a_good_code(env):
    env.store.value = None
    call(env)
    key = pending_key(env)
    good = code_at(key, int(env.clock.t) // 30)
    r = call(env, "/enrol", "POST", body=f"code={good}")
    assert r.status == 303 and env.store.value == key
    assert "paused" in call(env, cookie=r.headers["set-cookie"].split(";")[0].split("=", 1)[1]).body


def test_failed_store_write_during_enrolment_fails_closed(env):
    env.store.value = None
    call(env)
    good = code_at(pending_key(env), int(env.clock.t) // 30)
    env.store.put = lambda v: (_ for _ in ()).throw(RuntimeError("down"))
    r = call(env, "/enrol", "POST", body=f"code={good}")
    assert r.status == 503 and "set-cookie" not in r.headers


def test_enrolment_is_closed_once_a_secret_exists(env):
    before = env.store.value
    assert "Set up" not in call(env).body
    assert call(env, "/enrol", "POST", body="code=123456").status == 404
    assert env.store.value == before


def test_secret_store_failure_fails_closed(env):
    env.store.error = RuntimeError("down")
    r = call(env)
    assert r.status == 503 and "Set up" not in r.body and "paused" not in r.body


def test_unreadable_control_state_is_shown_not_hidden(env):
    cookie = login(env)

    def boom():
        raise RuntimeError("detail")

    env.state["read"] = boom
    r = call(env, cookie=cookie)
    assert "unreadable" in r.body and "detail" not in r.body


def test_values_are_escaped(env):
    cookie = login(env)
    env.state["read"] = lambda: NS(state="paused", paused_components=("<script>x</script>",), monthly_budget_usd=None)
    r = call(env, cookie=cookie)
    assert "<script>" not in r.body and "&lt;script&gt;" in r.body


def test_security_headers(env):
    h = call(env).headers
    assert h["cache-control"] == "no-store" and "default-src 'none'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_other_methods_refused(env, method):
    assert call(env, method=method).status == 405


def test_there_are_no_commands(env):
    cookie = login(env)
    assert call(env, "/pause", "POST", cookie=cookie, body="code=1").status == 404


def test_health_shows_nothing(env):
    r = call(env, "/health", token=None)
    assert r.status == 200 and r.body == "ok\n"


def verify_with(claims):
    return lambda token, aud: claims


@pytest.mark.parametrize("claims", [
    {"iss": "https://evil.test", "aud": AUD, "email": OWNER},
    {"iss": IAP_ISSUER, "aud": "/other", "email": OWNER},
    {"iss": IAP_ISSUER, "aud": AUD},
    {"iss": IAP_ISSUER, "aud": AUD, "email": ""},
    {},
])
def test_bad_iap_claims_refused(claims):
    assert verify_iap("t", AUD, verify=verify_with(claims)) is None


def test_good_iap_claims_accepted_and_email_normalised():
    assert verify_iap("t", AUD, verify=verify_with({"iss": IAP_ISSUER, "aud": AUD, "email": "Owner@NFH.test"})) == OWNER


def test_iap_verifier_crash_refuses():
    def boom(token, aud):
        raise ValueError("bad signature")

    assert verify_iap("t", AUD, verify=boom) is None


@pytest.mark.parametrize("token,aud", [(None, AUD), ("", AUD), ("t", ""), ("t", None)])
def test_missing_token_or_audience_refuses(token, aud):
    assert verify_iap(token, aud, verify=lambda t, a: pytest.fail("verified")) is None
