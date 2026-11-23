"""A trial job stops once its source is marked invalid, counts its failed days, and starts over once the owner
re-approves it (lc_core.validity; owner decision 2026-11-22)."""

from ingest_bulk.job import trial_gate, trial_outcome


class Store:
    def __init__(self, validity=None):
        self.files = {"validity.json": validity} if validity is not None else {}

    def read_json(self, name):
        return self.files.get(name)

    def write_json(self, name, data):
        self.files[name] = data


def outcome(store, result, ended=None, at="2026-11-23T03:00:00Z", log=None):
    trial_outcome("xx", store, result, ended or {}, log=(log.append if log is not None else lambda e: None), now=at, threshold=3)


def test_denied_runs_on_three_days_mark_the_source_invalid_and_its_job_then_stops():
    store, events = Store(), []
    for day in ("23", "24", "25"):
        outcome(store, "denied", {"rule": "upstream-error", "reason": "TimeoutError at the catalogue"},
                at=f"2026-11-{day}T03:00:00Z", log=events)
    mark = store.files["validity.json"]["invalid"]
    assert mark["at"] == "2026-11-25T03:00:00Z" and "denied (upstream-error): TimeoutError at the catalogue" in mark["reason"]
    assert events[-1]["event"] == "trial-invalidated"

    events.clear()
    approval = {"xx": {"status": "approved", "at": "2026-11-22T10:00:00Z"}}
    assert trial_gate("xx", store, decisions=approval, log=events.append) is False
    assert events == [{"event": "trial-invalid", "source": "xx", "since": "2026-11-25T03:00:00Z",
                       "why": "marked invalid; it runs again only once the owner re-approves it"}]
    assert store.files["validity.json"]["invalid"] == mark  # written back, so the lifecycle rule keeps it


def test_a_re_approved_source_runs_again_with_a_fresh_count():
    store = Store({"failedDays": ["2026-11-23", "2026-11-24", "2026-11-25"],
                   "invalid": {"at": "2026-11-25T03:00:00Z", "reason": "r"}})
    events = []
    assert trial_gate("xx", store, decisions={"xx": {"status": "approved", "at": "2026-11-26T09:00:00Z"}}, log=events.append)
    assert events[0]["event"] == "trial-retried" and store.files["validity.json"]["failedDays"] == []


def test_paused_busy_and_halted_runs_count_nothing_and_a_measured_run_clears_the_count():
    store = Store()
    outcome(store, "denied", {"rule": "system-not-running"})
    outcome(store, "halted")
    outcome(store, "busy")
    assert store.files == {}
    outcome(store, "held", {"reason": "volume guard"})
    assert store.files["validity.json"]["failedDays"] == ["2026-11-23"]
    outcome(store, "already-done", at="2026-11-24T03:00:00Z")
    assert store.files["validity.json"]["failedDays"] == []


def test_a_source_never_marked_runs():
    store = Store()
    assert trial_gate("xx", store, decisions={}) is True
