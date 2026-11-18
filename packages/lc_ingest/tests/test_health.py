from lc_ingest.health import HEALTH, fallback_decision, note


class Store:
    def __init__(self):
        self.objects = {}

    def read_json(self, name):
        return self.objects.get(name)

    def write_json(self, name, data):
        self.objects[name] = data


def test_failures_count_up_and_a_success_resets_them():
    s = Store()
    note(s, failure={"error": "BadFile", "message": "not a ZIP"}, now="t1")
    note(s, failure={"error": "BadFile", "message": "not a ZIP"}, now="t2")
    h = s.objects[HEALTH]
    assert h["consecutiveFailures"] == 2 and h["lastFailure"]["at"] == "t2" and h["lastFailure"]["refused"] is False
    note(s, outcome="done", now="t3")
    assert s.objects[HEALTH]["consecutiveFailures"] == 0 and s.objects[HEALTH]["lastSuccess"] == "t3"


def test_busy_or_halted_runs_change_nothing_and_errors_never_escape():
    s = Store()
    assert note(s, outcome="busy") is None and note(s, outcome="halted") is None and HEALTH not in s.objects

    class Broken:
        def read_json(self, name):
            raise PermissionError("no")

    events = []
    assert note(Broken(), outcome="done", log=events.append) is None
    assert events[0]["event"] == "health-unwritten"


def test_the_fallback_waits_for_consistent_failures_and_never_runs_around_a_refusal():
    assert fallback_decision(None, 3) == (False, "the regular download isn't failing")
    two = {"consecutiveFailures": 2, "lastFailure": {"error": "BadFile"}}
    assert fallback_decision(two, 3)[0] is False and "waits for 3" in fallback_decision(two, 3)[1]
    three = {"consecutiveFailures": 3, "lastFailure": {"error": "BadFile"}}
    assert fallback_decision(three, 3)[0] is True
    refused = {"consecutiveFailures": 9, "lastFailure": {"error": "BadFile", "refused": True, "status": 403}}
    ok, why = fallback_decision(refused, 3)
    assert ok is False and "refused us (HTTP 403)" in why
