import base64
import json

import pytest

from budget_guard.guard import BUDGET_NAME, decide, parse_push
from budget_guard.server import Conflict, handle


def push(**over):
    payload = {"budgetDisplayName": BUDGET_NAME, "costAmount": 10.0, "budgetAmount": 100000.0, "currencyCode": "INR", **over}
    data = base64.b64encode(json.dumps(payload).encode()).decode()
    return json.dumps({"message": {"data": data}}).encode()


def over_cap():
    return push(costAmount=100000.0)


def notice(cost, budget=100.0):
    return {"cost": cost, "budget": budget, "currency": "INR"}


class Store:
    def __init__(self, state=None, read_error=None, write_error=None, record_error=None):
        self.state = state if state is not None else {"state": "running", "paused_components": ["x"], "monthly_budget_usd": 1150}
        self.read_error, self.write_error, self.record_error = read_error, write_error, record_error
        self.writes, self.events, self.reads = [], [], 0

    def read(self):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return self.state, 7

    def write(self, state, generation):
        if self.write_error:
            raise self.write_error
        self.writes.append((state, generation))

    def record(self, event):
        if self.record_error:
            raise self.record_error
        self.events.append(event)


def run(body, store):
    logs = []
    return handle(body, store, logs.append), logs


def test_parses_a_real_shaped_notification():
    assert parse_push(push()) == {"cost": 10.0, "budget": 100000.0, "currency": "INR"}


@pytest.mark.parametrize("body", [
    b"", b"not json", b"[]", b"{}", b'{"message": {}}', b'{"message": {"data": "%%%"}}',
    push(budgetDisplayName="someone else's budget"), push(costAmount="abc"), push(costAmount="nan"), push(budgetAmount=0),
    push(budgetAmount=-1), push(costAmount=-1), push(budgetAmount="inf"),
])
def test_anything_else_is_ignored(body):
    assert parse_push(body) is None


@pytest.mark.parametrize("state", ["running", "manual"])
def test_cap_pauses_a_working_system(state):
    new = decide(notice(100), {"state": state, "paused_components": ["x"]})
    assert new["state"] == "paused" and new["paused_components"] == ["x"] and new["updated_by"] == "budget-guard"


def test_over_the_cap_pauses_too():
    assert decide(notice(250), {"state": "running"})["state"] == "paused"


def test_below_the_cap_does_nothing():
    assert decide(notice(99.99), {"state": "running"}) is None


@pytest.mark.parametrize("state", ["paused", "stopped"])
def test_never_touches_a_system_the_owner_already_halted(state):
    assert decide(notice(1000), {"state": state}) is None


def test_it_can_never_resume():
    for state in ("running", "paused", "stopped", "manual"):
        for cost in (0, 50, 100, 1000):
            new = decide(notice(cost), {"state": state})
            assert new is None or new["state"] == "paused"


def test_normal_notifications_read_nothing():
    s = Store()
    assert run(push(), s)[0] == 204 and s.reads == 0 and s.writes == []


def test_forecast_alone_does_not_pause():
    s = Store()
    run(push(costAmount=10.0, forecastThresholdExceeded=1.0), s)
    assert s.writes == []


def test_cap_writes_state_with_the_generation_and_records_it():
    s = Store()
    status, logs = run(over_cap(), s)
    assert status == 204
    state, generation = s.writes[0]
    assert state["state"] == "paused" and generation == 7
    assert s.events[0]["decision"] == "paused" and "100000 INR" in s.events[0]["reason"]


def test_owner_racing_the_guard_makes_pubsub_retry():
    assert run(over_cap(), Store(write_error=Conflict()))[0] == 500


@pytest.mark.parametrize("kw", [{"read_error": OSError("down")}, {"write_error": OSError("down")}])
def test_storage_failure_makes_pubsub_retry(kw):
    assert run(over_cap(), Store(**kw))[0] == 500


def test_failed_record_does_not_undo_or_retry_the_pause():
    s = Store(record_error=OSError("down"))
    assert run(over_cap(), s)[0] == 204 and len(s.writes) == 1


def test_already_halted_is_logged_not_rewritten():
    s = Store(state={"state": "stopped"})
    status, logs = run(over_cap(), s)
    assert status == 204 and s.writes == [] and logs[0]["action"] == "none"


def test_junk_is_acknowledged_so_it_stops_retrying():
    assert run(b"junk", Store())[0] == 204
