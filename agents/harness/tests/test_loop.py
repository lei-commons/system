from types import SimpleNamespace as NS

import pytest

from lc_core.control import SystemHalted
from lc_harness.loop import Budgets, run_session


def usage(n=10):
    return NS(input_tokens=n, output_tokens=n, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def text(t="ok", stop="end_turn"):
    return NS(stop_reason=stop, usage=usage(), content=[NS(type="text", text=t)])


def tool(*names):
    blocks = [NS(type="tool_use", id=f"t{i}", name=n, input={}) for i, n in enumerate(names)]
    return NS(stop_reason="tool_use", usage=usage(), content=[NS(type="thinking", thinking=""), *blocks])


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, *, system, tools, messages):
        self.calls.append(list(messages))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class Tools:
    def __init__(self):
        self.ran = []

    def definitions(self):
        return []

    def call(self, name, args):
        self.ran.append(name)
        return f"{name} output", False


def session(model, tools=None, gate=lambda: None, budgets=Budgets(), clock=None):
    kw = {"clock": clock} if clock else {}
    events = []
    result = run_session(model=model, tools=tools or Tools(), system="s", task="do it", budgets=budgets, gate=gate,
                         trace=lambda kind, **f: events.append((kind, f)), **kw)
    return result, events


def test_finishes_with_the_models_text():
    r, _ = session(Model(text("all done")))
    assert (r.status, r.text, r.steps) == ("done", "all done", 1)


def test_runs_tools_and_returns_all_results_in_one_message():
    m, t = Model(tool("a", "b"), text()), Tools()
    r, _ = session(m, t)
    assert r.status == "done" and t.ran == ["a", "b"]
    last = m.calls[1][-1]
    assert last["role"] == "user" and [x["tool_use_id"] for x in last["content"]] == ["t0", "t1"]


def test_assistant_content_goes_back_unchanged():
    first = tool("a")
    m = Model(first, text())
    session(m)
    assert m.calls[1][1] == {"role": "assistant", "content": first.content}


def test_history_is_append_only():
    m = Model(tool("a"), tool("a"), text())
    session(m)
    assert m.calls[2][: len(m.calls[1])] == m.calls[1]


def test_gate_runs_before_the_first_model_call():
    def gate():
        raise SystemHalted("system is paused")

    m = Model(text())
    r, _ = session(m, gate=gate)
    assert r.status == "halted" and m.calls == []


def test_gate_runs_between_model_calls():
    state = {"n": 0}

    def gate():
        state["n"] += 1
        if state["n"] == 3:
            raise SystemHalted("owner paused")

    m = Model(tool("a"), text())
    r, _ = session(m, gate=gate)
    assert r.status == "halted" and len(m.calls) == 1


def test_gate_runs_before_each_tool():
    state = {"n": 0}

    def gate():
        state["n"] += 1
        if state["n"] == 3:
            raise SystemHalted("owner paused")

    t = Tools()
    r, _ = session(Model(tool("a", "b")), t, gate=gate)
    assert r.status == "halted" and t.ran == ["a"]


def test_step_budget():
    m = Model(*[tool("a") for _ in range(5)])
    r, _ = session(m, budgets=Budgets(max_steps=2))
    assert r.status == "budget" and len(m.calls) == 2


def test_token_budget():
    m = Model(*[tool("a") for _ in range(5)])
    r, _ = session(m, budgets=Budgets(max_tokens=30))
    assert r.status == "budget" and len(m.calls) == 2


def test_time_budget():
    ticks = iter([0, 0, 100, 100, 100])
    m = Model(tool("a"), text())
    r, _ = session(m, budgets=Budgets(max_seconds=50), clock=lambda: next(ticks))
    assert r.status == "budget" and len(m.calls) == 1


def test_refusal_stops_without_running_anything():
    t = Tools()
    r, _ = session(Model(text(stop="refusal")), t)
    assert r.status == "refused" and t.ran == []


def test_truncated_tool_call_is_not_run():
    resp = tool("a")
    resp.stop_reason = "max_tokens"
    t = Tools()
    r, _ = session(Model(resp), t)
    assert r.status == "truncated" and t.ran == []


def test_pause_turn_continues():
    r, _ = session(Model(text(stop="pause_turn"), text("done")))
    assert (r.status, r.steps) == ("done", 2)


def test_unknown_stop_reason_fails():
    assert session(Model(text(stop="mystery")))[0].status == "failed"


def test_model_error_fails_cleanly_and_reports_only_the_type():
    r, _ = session(Model(RuntimeError("secret detail")))
    assert r.status == "failed" and "secret" not in r.reason


def test_trace_records_steps_and_the_end():
    _, events = session(Model(tool("a"), text()))
    assert [k for k, _ in events] == ["model", "tool", "model", "end"]
