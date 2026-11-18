import pytest

from lc_core import cycle
from lc_core.decisions import Recorder, failure


def test_the_repository_settings_load():
    s = cycle.load()
    assert s["clean_days"] == 4 and s["retries"] == 5 and s["retry_wait_minutes"] == 10
    assert s["schedule_utc"] == "02:00" and s["cron"] == "0 2 * * *"


def test_the_cron_follows_the_time():
    s = cycle.settings({"schedule_utc": "23:05", "sources": {"retry_wait_minutes": 1, "retries": 0}, "gate": {"clean_days": 1}})
    assert s["cron"] == "5 23 * * *"


def test_missing_settings_are_refused_never_defaulted():
    with pytest.raises(cycle.CycleConfigError):
        cycle.settings({})


def test_a_failure_carries_its_type_and_first_line():
    assert failure(ValueError("bad thing\nmore")) == {"event": "failed", "error": "ValueError", "message": "bad thing"}
    assert failure(RuntimeError())["message"] == ""


def test_download_events_and_failures_become_decisions_with_their_reasons():
    r = Recorder("job:ingest-x", log=lambda record: None)
    r({"event": "bad-download", "source": "x", "publish": "2026-11-18", "attempt": 1, "reason": "FetchError: answered 403"})
    r({"event": "download-recovered", "source": "x", "publish": "2026-11-18", "attempt": 2})
    r({"event": "download-failed", "source": "x", "publish": "2026-11-18", "attempts": 3, "reason": "BadFile: not a ZIP"})
    r(failure(RuntimeError("x.zip: still bad after 3 downloads")))
    got = [(e.action, e.decision, e.reason) for e in r.events]
    assert got == [
        ("downloaded", "failed", "attempt 1 was bad, fetching again: FetchError: answered 403"),
        ("downloaded", "done", "good on attempt 2"),
        ("downloaded", "failed", "still bad after 3 attempts: BadFile: not a ZIP"),
        ("ran", "failed", "stopped with RuntimeError: x.zip: still bad after 3 downloads"),
    ]
