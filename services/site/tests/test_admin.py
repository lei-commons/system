"""The admin area (owner decision 2026-11-21): its paths, its own IAP audience, and the second-factor page."""

import http.client
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from lc_core import second_factor as sf
from lc_core.totp import MemorySteps, Verifier, code_at
from lc_site.second_factor import SecondFactor
from lc_site.server import is_admin, make_server

OWNER = "o@x.test"
# Tokens as IAP would sign them: "site" through the ordinary backend, "admin" through the admin backend.
TOKENS = {("site", "site-aud"): OWNER, ("admin", "admin-aud"): OWNER, ("stranger", "admin-aud"): "s@x.test"}
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}


def fake_iap(token, audience):
    return next((email for (t, aud), email in TOKENS.items() if t == token and aud in audience.split(",")), None)


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Store:
    def __init__(self, value=None):
        self.value, self.puts = value, []

    def get(self):
        return self.value

    def put(self, value):
        self.puts.append(value)
        self.value = value


class System:
    def sources_page(self):
        return {"sources": []}

    def collect(self, meta):
        return {"control": {"state": "running"}, "snapshot": meta}


class Actions:
    def __init__(self):
        self.calls = []

    def handle(self, email, headers, body):
        from lc_core.owner import Outcome

        self.calls.append(email)
        return Outcome(200, "done")


class Snap:
    meta, count, relationship_count = {"date": "d", "snapshotRoot": "r"}, 0, 0


class Decisions:
    def days(self):
        return {"days": [{"day": "2026-11-21", "sealed": False, "events": None, "head": None}]}

    def day(self, day):
        return {"day": day, "events": []} if day == "2026-11-21" else None


def serve(admin_audience, second=None):
    srv = make_server(Snap(), [OWNER], "site-aud", ("127.0.0.1", 0), iap=fake_iap, system=System(), actions=Actions(),
                      admin_audience=admin_audience, second_factor=second, decisions=Decisions())
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(path, token, method="GET", body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        h = {"X-Goog-IAP-JWT-Assertion": token, **(headers or {})}
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    return srv, call


@pytest.fixture
def split():
    srv, call = serve("admin-aud")
    yield call
    srv.shutdown()
    srv.server_close()


def test_admin_paths_take_only_the_admin_backends_token(split):
    for path in ("/admin/", "/api/v1/admin/system", "/api/v1/admin/sources"):
        assert split(path, "site")[0] == 403, path  # through the ordinary backend: refused
        assert split(path, "admin")[0] == 200, path
    assert split("/api/v1/admin/act", "site", "POST", {"action": "pause"}, SAME_ORIGIN)[0] == 403
    assert split("/api/v1/admin/act", "admin", "POST", {"action": "pause"}, SAME_ORIGIN)[0] == 200
    assert split("/api/v1/admin/system", "stranger")[0] == 403  # the admin backend, but not the owner


def test_the_ordinary_paths_still_work_and_the_old_admin_paths_are_gone(split):
    assert split("/", "site")[0] == 200
    assert split("/api/v1/meta", "site")[0] == 200
    assert split("/api/v1/system", "site")[0] == 404 and split("/api/v1/sources", "site")[0] == 404
    assert split("/api/v1/system", "admin")[0] == 403  # an admin token is no good outside the admin area
    assert split("/api/v1/act", "site", "POST", {"action": "pause"}, SAME_ORIGIN)[0] == 405
    assert split("/admin/anything-else", "admin")[0] == 404


def test_the_decision_log_is_in_the_admin_area(split):
    status, body = split("/api/v1/admin/log", "admin")
    assert status == 200 and json.loads(body)["days"][0]["day"] == "2026-11-21"
    assert split("/api/v1/admin/log/2026-11-21", "admin")[0] == 200
    assert split("/api/v1/admin/log/not-a-day", "admin")[0] == 404
    assert split("/api/v1/admin/log", "site")[0] == 403  # through the ordinary backend: refused
    assert split("/api/v1/log", "site")[0] == 404 and split("/api/v1/log/2026-11-21", "site")[0] == 404  # moved


def test_the_site_says_whether_the_viewer_is_an_admin(split):
    status, body = split("/api/v1/me", "site")
    assert status == 200 and json.loads(body) == {"admin": True}
    assert split("/api/v1/me", "nobody")[0] == 403  # the site is the owner's alone until it goes public


def test_the_admin_tabs_are_marked_and_hidden_from_everyone_else():
    static = Path(__file__).resolve().parents[1] / "lc_site" / "static"
    html, css, app = ((static / n).read_text() for n in ("index.html", "app.css", "app.js"))
    for route in ("sources", "trials", "system", "log", "approvals", "security"):
        assert f'data-route="{route}" data-area="admin"' in html, route
    assert 'body:not(.is-admin) #nav a[data-area="admin"] { display: none; }' in css
    admin_views = app[app.index("const ADMIN_VIEWS"):app.index("};", app.index("const ADMIN_VIEWS"))]
    public_views = app[app.index("const PUBLIC_VIEWS"):app.index("};", app.index("const PUBLIC_VIEWS"))]
    assert "log:" in admin_views and "log:" not in public_views


def test_before_the_admin_backend_exists_admin_paths_are_checked_like_the_rest():
    srv, call = serve("")
    try:
        assert call("/api/v1/admin/system", "site")[0] == 200
        assert call("/api/v1/admin/system", "admin")[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_admin_paths_match_the_load_balancers_config():
    config = yaml.safe_load((Path(__file__).resolve().parents[3] / "config" / "site.yaml").read_text())["admin"]
    assert config["reauth_method"] in ("ENROLLED_SECOND_FACTORS", "SECURE_KEY", "LOGIN")
    assert 5 <= config["reauth_max_age_minutes"] <= 1440
    for pattern in config["paths"]:
        example = pattern.replace("*", "x")
        assert is_admin(example), example
    for path in ("/", "/static/app.js", "/api/v1/search", "/api/v1/log", "/administrator", "/api/v1/adminx"):
        assert not is_admin(path), path


# ---- the second factor ----

@pytest.fixture
def sec():
    clock, store, events = Clock(), Store(), []
    page = SecondFactor(store=store, verifier=Verifier(MemorySteps(), clock=clock), record=events.append,
                        clock=lambda: datetime.fromtimestamp(clock.t, timezone.utc), log=lambda r: None)

    def post(step, code, headers=SAME_ORIGIN):
        return page.handle(OWNER, headers, json.dumps({"step": step, "code": code}).encode())

    def code(key, ahead=0):
        clock.t += 30  # each code is a new time step: a code works once
        return code_at(key, int(clock.t) // 30 + ahead)

    return page, store, events, post, code


def test_setup_shows_a_kept_key_until_a_code_from_it_activates_it(sec):
    page, store, events, post, code = sec
    first = page.view(OWNER)
    assert first["status"] == "setting-up" and first["key"] and first["uri"].startswith("otpauth://totp/lei-commons:")
    assert first["qr"] is None or first["qr"].startswith("data:image/svg+xml")
    assert page.view(OWNER)["key"] == first["key"] and len(store.puts) == 1  # a reload keeps the same key
    outcome, _ = post("setup", "000000")
    assert outcome.status == 403 and events[-1]["decision"] == "rejected" and "000000" not in str(events)
    outcome, _ = post("setup", code(first["key"]))
    assert outcome.ok and store.value == first["key"] and events[-1]["action"] == "second-factor:setup"
    after = page.view(OWNER)
    assert after == {"status": "active"}  # once set up, no page load ever shows a key


def test_a_rotation_needs_the_current_key_then_the_new_one(sec):
    page, store, events, post, code = sec
    current = sf.new_key()
    store.value = current
    assert post("confirm-rotation", code(current))[0].status == 409  # nothing to confirm
    assert post("rotate", "123456")[0].status == 403 and store.value == current
    outcome, extra = post("rotate", code(current))
    assert outcome.ok and extra["key"] and extra["key"] != current
    new = extra["key"]
    assert sf.status(store.value) == "rotating" and sf.active(store.value) == current
    assert page.view(OWNER) == {"status": "rotating"}  # the new key isn't shown again on a page load
    assert post("confirm-rotation", code(current))[0].status == 403  # a code from the old key doesn't confirm
    outcome, _ = post("confirm-rotation", code(new))
    assert outcome.ok and store.value == new
    assert [e["action"] for e in events if e["decision"] == "done"] == ["second-factor:rotate",
                                                                       "second-factor:confirm-rotation"]


def test_a_rotation_can_be_cancelled_with_the_current_key(sec):
    page, store, events, post, code = sec
    current = sf.new_key()
    store.value = current
    _, extra = post("rotate", code(current))
    assert post("cancel-rotation", code(extra["key"]))[0].status == 403  # the new key can't cancel
    outcome, _ = post("cancel-rotation", code(current))
    assert outcome.ok and store.value == current


def test_steps_need_same_origin_json_and_a_known_step(sec):
    page, store, events, post, code = sec
    store.value = sf.new_key()
    assert post("rotate", code(store.value), headers={"Content-Type": "application/json"})[0].status == 403
    assert post("rotate", code(store.value), headers={"Sec-Fetch-Site": "same-origin"})[0].status == 415
    assert post("delete-key", code(store.value))[0].status == 400
    assert post("setup", code(store.value))[0].status == 409  # already set up: only a rotation changes it
    assert sf.status(store.value) == "active" and events == []


def test_a_store_failure_changes_nothing(sec):
    page, store, events, post, code = sec
    current = sf.new_key()
    store.value = current

    def down(value):
        raise RuntimeError("secret manager down")

    store.put = down
    outcome, extra = post("rotate", code(current))
    assert outcome.status == 502 and extra == {} and store.value == current
    assert events[-1]["decision"] == "failed"


def test_the_second_factor_page_through_the_server():
    clock, store = Clock(), Store(sf.new_key())
    page = SecondFactor(store=store, verifier=Verifier(MemorySteps(), clock=clock), record=lambda e: None,
                        clock=lambda: datetime.fromtimestamp(clock.t, timezone.utc), log=lambda r: None)
    srv, call = serve("admin-aud", page)
    try:
        assert json.loads(call("/api/v1/admin/second-factor", "admin")[1]) == {"status": "active"}
        assert call("/api/v1/admin/second-factor", "site")[0] == 403
        good = code_at(store.value, int(clock.t) // 30)
        status, body = call("/api/v1/admin/second-factor", "admin", "POST", {"step": "rotate", "code": good}, SAME_ORIGIN)
        answer = json.loads(body)
        assert status == 200 and answer["ok"] and answer["key"] and sf.status(store.value) == "rotating"
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_approvals_page_is_admin_only():
    class Page:
        def pending(self):
            return {"pullRequests": []}

        def handle(self, email, headers, body):
            from lc_core.owner import Outcome

            return Outcome(200, "approved")

    srv = make_server(Snap(), [OWNER], "site-aud", ("127.0.0.1", 0), iap=fake_iap, admin_audience="admin-aud",
                      approvals=Page())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def call(token, method="GET", body=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request(method, "/api/v1/admin/approvals", body=json.dumps(body) if body else None,
                      headers={"X-Goog-IAP-JWT-Assertion": token, **(SAME_ORIGIN if body else {})})
            r = c.getresponse()
            out = r.status, r.read()
            c.close()
            return out

        assert call("site")[0] == 403 and call("admin")[0] == 200
        assert call("site", "POST", {"repo": "x"})[0] == 403
        assert json.loads(call("admin", "POST", {"repo": "x"})[1])["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
