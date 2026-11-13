"""Owner actions on the site: POST /api/v1/act, through a real socket."""

import http.client
import json
import threading
from datetime import datetime, timezone

import pytest

from lc_core.totp import MemorySteps, Verifier, code_at
from lc_site.actions import OwnerActions
from lc_site.server import make_server

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
REGISTRY = [{"id": "gleif", "name": "GLEIF", "approval": "approved"}, {"id": "us-co-sos", "name": "Colorado"}]


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Backend:
    def __init__(self):
        self.state, self.approvals, self.events, self.runs = {"state": "running", "paused_components": []}, {}, [], []
        self.edits = {}

    def read_edits(self):
        return self.edits, 1

    def write_edits(self, edits, generation):
        self.edits = edits

    def read_state(self):
        return dict(self.state), 1

    def write_state(self, state, generation):
        self.state = state

    def read_approvals(self):
        return dict(self.approvals), 1

    def write_approvals(self, decisions, generation):
        self.approvals = decisions

    def run_job(self, job):
        self.runs.append(job)
        return "x"

    def record(self, event):
        self.events.append(event)


@pytest.fixture
def site():
    clock, backend, changed = Clock(), Backend(), []
    secret = {"value": SECRET}
    actions = OwnerActions(backend=backend, verifier=Verifier(MemorySteps(), clock=clock), read_secret=lambda: secret["value"],
                           sources=[{"job": "ingest-gleif", "label": "GLEIF"}], registry=REGISTRY,
                           clock=lambda: datetime.fromtimestamp(clock.t, timezone.utc), log=lambda r: None,
                           on_change=lambda: changed.append(1))

    class Snap:
        meta, count, relationship_count = {"date": "d", "snapshotRoot": "r"}, 0, 0

    srv = make_server(Snap(), ["o@x.test"], "aud", ("127.0.0.1", 0), iap=lambda t, a: {"ok": "o@x.test", "other": "p@x.test"}.get(t),
                      actions=actions)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(body, token="ok", fetch_site="same-origin", content_type="application/json", path="/api/v1/act"):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        headers = {"Content-Type": content_type, "Origin": "null"}
        if token:
            headers["X-Goog-IAP-JWT-Assertion"] = token
        if fetch_site:
            headers["Sec-Fetch-Site"] = fetch_site
        c.request("POST", path, body=body if isinstance(body, (bytes, str)) else json.dumps(body), headers=headers)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        c.close()
        return out

    def code(offset=0):
        return code_at(SECRET, int(clock.t) // 30 + offset)

    yield post, code, clock, backend, changed, secret
    srv.shutdown()
    srv.server_close()


def test_approving_colorado_inline(site):
    post, code, clock, backend, changed, _ = site
    status, data = post({"action": "source-approve:us-co-sos", "reason": "terms checked", "code": code()})
    assert status == 200 and data["ok"] and backend.approvals["us-co-sos"]["status"] == "approved"
    assert backend.events[-1]["trigger"] == "site" and changed == [1]


def test_only_the_owner_from_the_sites_own_pages(site):
    post, code, clock, backend, changed, _ = site
    body = {"action": "pause", "reason": "x", "code": code()}
    assert post(body, token=None)[0] == 403
    assert post(body, token="other")[0] == 403
    assert post(body, fetch_site=None)[0] == 403  # not a browser saying it's this site
    assert post(body, fetch_site="cross-site")[0] == 403
    assert post(body, fetch_site="same-site")[0] == 403
    assert post("do=pause&reason=x", content_type="application/x-www-form-urlencoded")[0] == 415  # no plain forms
    assert backend.state["state"] == "running" and backend.events == []


def test_codes_reasons_and_unknown_actions(site):
    post, code, clock, backend, changed, _ = site
    assert post({"action": "source-approve:us-co-sos", "reason": "", "code": code()})[0] == 400
    assert post({"action": "source-approve:us-co-sos", "reason": "x", "code": "000000"})[0] == 403
    assert post({"action": "drop-everything", "reason": "x", "code": code()})[0] == 404
    assert post(b"{not json")[0] == 400
    used = code()
    assert post({"action": "pause", "reason": "x", "code": used})[0] == 200
    status, data = post({"action": "resume", "reason": "x", "code": used})
    assert status == 403 and "already used" in data["message"] and backend.state["state"] == "paused"


def test_toggling_a_source_back_and_forth(site):
    post, code, clock, backend, changed, _ = site
    for status in ("deny", "pending", "approve"):
        assert post({"action": f"source-{status}:gleif", "reason": status, "code": code()})[0] == 200
        clock.t += 30
    assert backend.approvals["gleif"]["status"] == "approved"
    assert [h["status"] for h in backend.approvals["gleif"]["history"]] == ["denied", "pending"]


def test_not_enrolled_or_secret_unavailable(site):
    post, code, clock, backend, changed, secret = site
    secret["value"] = "pending:ABC"
    status, data = post({"action": "pause", "reason": "x", "code": code()})
    assert status == 403 and "enrol" in data["message"]
    secret["value"] = None
    assert post({"action": "pause", "reason": "x", "code": code()})[0] == 403
    assert backend.events == []


def test_other_posts_are_refused(site):
    post, code, *_ = site
    assert post({"x": 1}, path="/api/v1/search")[0] == 405


def test_editing_a_source_and_leaving_a_suggestion_inline(site):
    post, code, clock, backend, changed, _ = site
    status, data = post({"action": "source-edit:us-co-sos", "code": code(),
                         "fields": {"url": "https://data.colorado.gov", "licence": "CC0-1.0"}})
    assert status == 200 and data["message"] == "Saved Colorado: url, licence."
    assert backend.edits["sources"]["us-co-sos"]["fields"] == {"url": "https://data.colorado.gov", "licence": "CC0-1.0"}
    assert backend.events[-1]["reason"] == "edited url, licence" and backend.events[-1]["fields"] == ["url", "licence"]
    clock.t += 30
    hint = "Look at the Business Entities dataset (4ykn-tg5h).\n" + "ü" * 940
    status, data = post({"action": "source-hint:us-co-sos", "code": code(), "hint": hint, "reason": "a lead"})
    assert status == 200 and backend.edits["sources"]["us-co-sos"]["hint"] == hint and backend.events[-1]["reason"] == "a lead"
    clock.t += 30
    status, data = post({"action": "source-add", "code": code(), "fields": {"name": "Delaware", "country": "US"}})
    assert status == 200 and "us-delaware" in backend.edits["sources"]
    clock.t += 30
    status, _ = post({"action": "source-approve:us-delaware", "code": code(), "reason": "ok"})
    assert status == 200 and backend.approvals["us-delaware"]["status"] == "approved"


def test_registry_changes_are_checked_like_every_action(site):
    post, code, clock, backend, changed, _ = site
    assert post({"action": "source-edit:us-co-sos", "code": code(), "fields": {"pipeline": {"job": "x"}}})[0] == 400
    assert post({"action": "source-remove:gleif", "code": code()})[0] == 400  # approved, and the code is unused...
    assert post({"action": "source-hint:us-co-sos", "code": code(), "hint": "x" * 1001})[0] == 400
    assert post({"action": "source-hint:us-co-sos", "code": "000000", "hint": "x"})[0] == 403
    assert post({"action": "source-hint:us-co-sos", "code": code(), "hint": "x"}, fetch_site="cross-site")[0] == 403
    assert post({"action": "source-hint:us-co-sos", "code": code(), "hint": "x"}, token="other")[0] == 403
    assert post({"action": "source-hint:us-co-sos", "code": code(), "hint": "x" * 40000})[0] == 400  # too big
    assert backend.edits == {} and backend.events == []
    assert post({"action": "source-hint:us-co-sos", "code": code(), "hint": "x"})[0] == 200  # ...so it still works
