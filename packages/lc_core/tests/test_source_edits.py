import pytest

from lc_core import source_edits as se
from lc_core.approvals import effective, registry
from lc_core.owner import Conflict, perform
from lc_core.totp import MemorySteps, Verifier, code_at

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
NOW = "2026-11-14T09:00:00Z"
BASE = [
    {"id": "gleif", "name": "GLEIF", "country": "*", "approval": "approved", "url": "https://www.gleif.org",
     "assessment": {"risk": "low", "summary": "CC0", "verified": False},
     "pipeline": {"job": "ingest-gleif", "store": "gleif", "records_as": "gleif", "label": "GLEIF", "refresh": "daily"}},
    {"id": "us-co-sos", "name": "Colorado", "country": "US", "url": "https://data.colorado.gov",
     "assessment": {"risk": "unknown", "summary": "Not researched.", "verified": False}},
]
SOURCES = [{"job": "ingest-gleif", "label": "GLEIF"}]


def test_editing_keeps_protected_fields_and_records_history():
    edits, changed = se.edit({}, BASE, "gleif", {"name": "GLEIF golden copy", "url": "https://www.gleif.org/x",
                                                 "assessment": {"verified": True}}, "owner:o", NOW)
    assert changed == ["name", "url", "assessment"]
    view = {s["id"]: s for s in se.merged(BASE, edits)}
    g = view["gleif"]
    assert g["name"] == "GLEIF golden copy" and g["pipeline"] == BASE[0]["pipeline"] and g["approval"] == "approved"
    assert g["assessment"] == {"risk": "low", "summary": "CC0", "verified": True}
    assert g["owner"]["edited_by"] == "owner:o"
    assert edits["history"][-1]["before"]["name"] == "GLEIF" and edits["history"][-1]["after"]["name"] == "GLEIF golden copy"
    assert BASE[0]["name"] == "GLEIF"  # the base isn't touched


@pytest.mark.parametrize("fields, why", [
    ({"id": "x"}, "can't be edited"), ({"pipeline": {}}, "can't be edited"), ({"approval": "approved"}, "can't be edited"),
    ({"url": "javascript:alert(1)"}, "http or https"), ({"country": "USA"}, "two-letter"), ({"name": "  "}, "required"),
    ({"access": "scrape"}, "one of"), ({"assessment": {"risk": "tiny"}}, "one of"),
    ({"assessment": {"verified": "yes"}}, "true or false"), ({"assessment": {"owner": 1}}, "assessment takes"),
    ({"name": "x" * 121}, "longer than"),
])
def test_invalid_edits_are_refused(fields, why):
    with pytest.raises(se.Invalid, match=why):
        se.edit({}, BASE, "us-co-sos", fields, "owner:o", NOW)


def test_an_edit_that_changes_nothing_is_refused():
    with pytest.raises(se.Invalid, match="nothing changed"):
        se.edit({}, BASE, "us-co-sos", {"name": "Colorado"}, "owner:o", NOW)


def test_adding_a_source_starts_pending_with_a_fresh_id():
    edits, sid = se.add({}, BASE, {"name": "Colorado SOS", "country": "us", "url": "https://x.example"}, "owner:o", NOW)
    assert sid == "us-colorado-sos"
    edits, sid2 = se.add(edits, BASE, {"name": "Colorado SOS", "country": "US"}, "owner:o", NOW)
    assert sid2 == "us-colorado-sos-2"
    view = {s["id"]: s for s in se.merged(BASE, edits)}
    assert view[sid]["country"] == "US" and view[sid]["owner"]["added"] is True and "pipeline" not in view[sid]
    assert effective(list(view.values()), {})[sid]["status"] == "pending"
    with pytest.raises(se.Invalid, match="name and a country"):
        se.add({}, BASE, {"url": "https://x.example"}, "owner:o", NOW)


def test_removing_hides_unbuilt_sources_only():
    edits = se.remove({}, BASE, "us-co-sos", "owner:o", NOW)
    assert [s["id"] for s in se.merged(BASE, edits)] == ["gleif"]
    with pytest.raises(se.Invalid, match="in use"):
        se.remove({}, BASE, "gleif", "owner:o", NOW)
    # even a hand-made removal of a built source leaves it in place
    assert [s["id"] for s in se.merged(BASE, {"sources": {"gleif": {"removed": True}}})] == ["gleif", "us-co-sos"]


def test_hints_keep_line_breaks_and_are_capped():
    edits = se.hint({}, BASE, "us-co-sos", "Try the bulk file:\r\nhttps://data.colorado.gov/x  ", "owner:o", NOW)
    assert se.merged(BASE, edits)[1]["owner"]["hint"] == "Try the bulk file:\nhttps://data.colorado.gov/x"
    assert se.hint(edits, BASE, "us-co-sos", "é" * 1000, "owner:o", NOW)
    with pytest.raises(se.Invalid, match="1000"):
        se.hint({}, BASE, "us-co-sos", "x" * 1001, "owner:o", NOW)


def test_history_is_bounded():
    edits = {}
    for i in range(se.MAX_HISTORY + 20):
        edits = se.hint(edits, BASE, "us-co-sos", f"hint {i}", "owner:o", NOW)
    assert len(edits["history"]) == se.MAX_HISTORY and edits["history"][-1]["after"]["hint"] == f"hint {se.MAX_HISTORY + 19}"


def test_the_real_registry_merges_cleanly():
    base = registry()
    assert se.merged(base, {}) == [{**s, "owner": {}} for s in base]


# The engine: the same path the site and the console use.

class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Backend:
    def __init__(self):
        self.approvals, self.approvals_gen = {}, 0
        self.edits, self.edits_gen = {}, 0
        self.events, self.conflict, self.unreadable = [], False, False

    def read_approvals(self):
        return dict(self.approvals), self.approvals_gen

    def write_approvals(self, decisions, generation):
        if generation != self.approvals_gen:
            raise Conflict("it changed meanwhile; try again")
        self.approvals, self.approvals_gen = decisions, generation + 1

    def read_edits(self):
        if self.unreadable:
            raise OSError("bucket unavailable")
        return self.edits, self.edits_gen

    def write_edits(self, edits, generation):
        if self.conflict or generation != self.edits_gen:
            raise Conflict("it changed meanwhile; try again")
        self.edits, self.edits_gen = edits, generation + 1

    def record(self, event):
        self.events.append(event)


@pytest.fixture
def env():
    clock = Clock()
    backend = Backend()
    verifier = Verifier(MemorySteps(), clock=clock)

    def act(action, data=None, reason="", code=None, advance=True):
        clock.t += 30 if advance else 0  # a fresh code each time, unless reusing the last one
        code = code if code is not None else code_at(SECRET, int(clock.t) // 30)
        return perform(backend=backend, verifier=verifier, secret=SECRET, sources=SOURCES, registry=BASE,
                       action=action, reason=reason, code=code, email="o@x.test", now=NOW, trigger="site", data=data)

    return backend, act


def test_add_then_approve_edit_hint_and_remove_through_the_engine(env):
    backend, act = env
    out = act("source-add", {"fields": {"name": "Delaware Division of Corporations", "country": "US"}})
    assert out.ok and "pending" in out.message
    sid = "us-delaware-division-of-corporations"
    assert backend.events[-1] | {} == {**backend.events[-1], "action": "source-add", "subject": f"source {sid}",
                                       "reason": f"added {sid}", "change": "add"}
    assert act(f"source-approve:{sid}", reason="terms read").ok  # approvals see added sources
    assert backend.approvals[sid]["status"] == "approved"
    out = act(f"source-edit:{sid}", {"fields": {"url": "https://corp.delaware.gov"}})
    assert out.ok and out.message == "Saved Delaware Division of Corporations: url."
    out = act(f"source-hint:{sid}", {"hint": "Their bulk file is paid; look for a free API.\nhttps://corp.delaware.gov"})
    assert out.ok and backend.events[-1]["hint_chars"] == 71 and "Their bulk" not in str(backend.events[-1])
    out = act(f"source-remove:{sid}")
    assert out.status == 400 and "deny it or keep it pending first" in out.message
    assert act(f"source-deny:{sid}", reason="paid only").ok
    assert act(f"source-remove:{sid}").ok
    assert act(f"source-edit:{sid}", {"fields": {"name": "x"}}).status == 404  # gone


def test_a_mistake_in_the_form_leaves_the_code_unused(env):
    backend, act = env
    out = act("source-edit:us-co-sos", {"fields": {"url": "ftp://nope"}})
    assert out.status == 400 and "http or https" in out.message and backend.edits == {} and backend.events == []
    out = act("source-edit:us-co-sos", {"fields": {"url": "https://x.example"}}, advance=False)  # the same code
    assert out.ok and backend.edits["sources"]["us-co-sos"]["fields"]["url"] == "https://x.example"
    assert act("source-hint:us-co-sos", {"hint": "again"}, advance=False).status == 403  # now it's used


def test_codes_and_failures_change_nothing(env):
    backend, act = env
    assert act("source-edit:us-co-sos", {"fields": {"name": "Colorado SOS"}}, code="000000").status == 403
    assert act("source-edit:nowhere", {"fields": {"name": "x"}}).status == 404
    assert act("source-remove:gleif").status == 400
    assert act("source-hint:us-co-sos", {"hint": "x" * 1001}).status == 400
    assert act("source-edit:us-co-sos", {"fields": "name"}).status == 400
    assert backend.edits == {} and backend.events == []
    backend.conflict = True
    out = act("source-edit:us-co-sos", {"fields": {"name": "Colorado SOS"}})
    assert out.status == 409 and backend.events[-1]["decision"] == "failed"
    backend.conflict, backend.unreadable = False, True
    out = act("source-edit:us-co-sos", {"fields": {"name": "Colorado SOS"}})
    assert out.status == 503 and backend.edits == {}
    assert act("source-approve:us-co-sos", reason="ok").ok  # approvals still work without the edits


def test_a_corrupt_edits_file_is_never_overwritten(env):
    backend, act = env
    backend.edits = None
    out = act("source-hint:us-co-sos", {"hint": "x"})
    assert out.status == 503 and backend.edits is None
