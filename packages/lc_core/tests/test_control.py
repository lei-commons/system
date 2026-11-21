import json

import pytest

from lc_core.control import SystemHalted, require_running


@pytest.fixture
def state(tmp_path):
    """An env naming a state.json with the given contents (the cloud copy is the only source)."""
    def make(word, paused=(), budget=None):
        p = tmp_path / "state.json"
        d = {"state": word, "paused_components": list(paused)}
        if budget is not None:
            d["monthly_budget_usd"] = budget
        p.write_text(json.dumps(d))
        return {"LC_CONTROL_STATE_URI": str(p)}
    return make


def test_running_allows(state):
    st = require_running("job:ingest-gleif", state("running", budget=1500))
    assert st.monthly_budget_usd == 1500


@pytest.mark.parametrize("word", ["paused", "stopped", "manual"])
def test_non_running_halts(state, word):
    with pytest.raises(SystemHalted):
        require_running("job:ingest-gleif", state(word))


def test_missing_state_fails_closed():
    with pytest.raises(SystemHalted):
        require_running("job:x", {})


def test_github_variables_are_no_longer_a_source():
    # Retired 2026-11-21: the org variables never count, even when present.
    with pytest.raises(SystemHalted, match="LC_CONTROL_STATE_URI"):
        require_running("job:x", {"SYSTEM_STATE": "running", "MONTHLY_BUDGET_USD": "1500"})


def test_unknown_state_fails_closed(state):
    with pytest.raises(SystemHalted):
        require_running("job:x", state("turbo"))


def test_component_pause_and_wildcard(state):
    env = state("running", ["agent:resolver-*", "job:crawl-br"])
    with pytest.raises(SystemHalted):
        require_running("agent:resolver-in", env)
    with pytest.raises(SystemHalted):
        require_running("job:crawl-br", env)
    require_running("job:crawl-gb", env)


def test_state_file(state):
    env = state("running", ["agent:*"], budget=900)
    assert require_running("job:ingest-gb", env).monthly_budget_usd == 900
    with pytest.raises(SystemHalted):
        require_running("agent:maker-gb", env)


def test_missing_state_file_fails_closed(tmp_path):
    with pytest.raises(SystemHalted):
        require_running("job:x", {"LC_CONTROL_STATE_URI": str(tmp_path / "absent.json")})


def test_corrupt_state_file_fails_closed(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    with pytest.raises(SystemHalted):
        require_running("job:x", {"LC_CONTROL_STATE_URI": str(p)})


def test_manual_mode_lets_proposing_work_continue(state):
    assert require_running("agent:engineering", state("manual"), allow_manual=True).state == "manual"


def test_manual_mode_still_stops_merging_and_deploying(state):
    with pytest.raises(SystemHalted):
        require_running("job:publish", state("manual"))


@pytest.mark.parametrize("word", ["paused", "stopped"])
def test_allow_manual_does_not_get_past_pause_or_stop(state, word):
    with pytest.raises(SystemHalted):
        require_running("agent:engineering", state(word), allow_manual=True)


def test_allow_manual_still_honours_a_paused_component(state):
    env = state("manual", ["agent:engineering"])
    with pytest.raises(SystemHalted):
        require_running("agent:engineering", env, allow_manual=True)
