"""A source on trial is marked invalid after failing on several separate days; the owner can re-approve it once
more, and it then gets as many days again (owner decision 2026-11-22; lc_core.validity)."""

import pytest

from lc_core.validity import gate, note, state, validity_name

FAIL = {"error": "TimeoutError", "message": "the catalogue didn't answer"}


def fail(v, at, threshold=3):
    return note(v, failed=True, failure=FAIL, now=at, source="xx", threshold=threshold)


def test_three_failed_days_mark_a_source_invalid_with_the_date_and_the_reason():
    v, event = fail({}, "2026-11-23T03:00:00Z")
    v, event = fail(v, "2026-11-23T03:40:00Z")  # the retry round: the same day counts once
    assert v["failedDays"] == ["2026-11-23"] and event is None and "invalid" not in v
    v, _ = fail(v, "2026-11-24T03:00:00Z")
    v, event = fail(v, "2026-11-25T03:00:00Z")
    assert v["invalid"]["at"] == "2026-11-25T03:00:00Z"
    assert "3 separate days" in v["invalid"]["reason"] and "2026-11-23, 2026-11-24, 2026-11-25" in v["invalid"]["reason"]
    assert "TimeoutError: the catalogue didn't answer" in v["invalid"]["reason"]
    # the public log gets counts and the error's type, never the message
    assert event == {"event": "trial-invalidated", "source": "xx", "failedDays": 3, "error": "TimeoutError"}
    assert state(v, {"status": "approved", "at": "2026-11-22T10:00:00Z"})["state"] == "invalid"


def test_a_successful_run_clears_the_count():
    v, _ = fail({}, "2026-11-23T03:00:00Z")
    v, _ = fail(v, "2026-11-24T03:00:00Z")
    v, event = note(v, failed=False, now="2026-11-25T03:00:00Z", source="xx")
    assert v["failedDays"] == [] and v["lastSuccess"] == "2026-11-25T03:00:00Z" and event is None
    v, _ = fail(v, "2026-11-26T03:00:00Z")
    assert "invalid" not in v and state(v, None)["state"] == "failing"


def test_an_invalid_source_does_not_run_until_the_owner_re_approves_it():
    v = {}
    for day in ("23", "24", "25"):
        v, _ = fail(v, f"2026-11-{day}T03:00:00Z")
    old_approval = {"status": "approved", "by": "owner:o@x.test", "at": "2026-11-22T10:00:00Z"}
    may, kept, event = gate(v, old_approval, "xx")
    assert not may and kept == v and event["event"] == "trial-invalid"
    assert gate(v, {"status": "denied", "at": "2026-11-27T10:00:00Z"}, "xx")[0] is False

    again = {"status": "approved", "by": "owner:o@x.test", "at": "2026-11-26T08:00:00Z"}
    assert state(v, again)["state"] == "retrying"
    may, fresh, event = gate(v, again, "xx")
    assert may and event["event"] == "trial-retried" and "invalid" not in fresh and fresh["failedDays"] == []
    assert fresh["history"][0]["reason"] == v["invalid"]["reason"] and fresh["history"][0]["reapprovedAt"] == again["at"]

    # tried again for as many days, then marked invalid again with the new date and reason
    for day in ("27", "28"):
        fresh, _ = fail(fresh, f"2026-11-{day}T03:00:00Z")
    assert "invalid" not in fresh
    fresh, event = fail(fresh, "2026-11-29T03:00:00Z")
    assert fresh["invalid"]["at"] == "2026-11-29T03:00:00Z" and event["event"] == "trial-invalidated"
    shown = state(fresh, again)
    assert shown["state"] == "invalid" and shown["at"] == "2026-11-29T03:00:00Z" and len(shown["history"]) == 1


def test_times_with_offsets_compare_as_times():
    v = {"invalid": {"at": "2026-11-25T03:00:00Z", "reason": "x"}}
    assert state(v, {"status": "approved", "at": "2026-11-25T08:00:00+05:30"})["state"] == "invalid"  # 02:30 UTC
    assert state(v, {"status": "approved", "at": "2026-11-25T09:00:00+05:30"})["state"] == "retrying"  # 03:30 UTC


def test_a_new_source_is_ok_and_the_threshold_is_respected():
    assert state(None, None)["state"] == "ok"
    v, event = fail({}, "2026-11-23T03:00:00Z", threshold=1)
    assert v["invalid"] and event


def test_the_validity_path_takes_only_a_source_id():
    assert validity_name("cy-drcor") == "trials/cy-drcor/validity.json"
    with pytest.raises(ValueError):
        validity_name("../control")


def test_the_registry_sets_the_number_of_days(tmp_path):
    from lc_core.sources import invalid_after_failed_days

    assert invalid_after_failed_days() == 3  # config/sources.yaml
    path = tmp_path / "s.yaml"
    path.write_text("sources: []\n")
    assert invalid_after_failed_days(path) == 3
    path.write_text("trials: {invalid_after_failed_days: 0}\nsources: []\n")
    with pytest.raises(ValueError):
        invalid_after_failed_days(path)
