import json

import pytest

from lc_core.control import SystemHalted, require_running


def test_running_allows():
    st = require_running("job:ingest-gleif", {"SYSTEM_STATE": "running", "MONTHLY_BUDGET_USD": "1500"})
    assert st.monthly_budget_usd == 1500


@pytest.mark.parametrize("state", ["paused", "stopped", "manual"])
def test_non_running_halts(state):
    with pytest.raises(SystemHalted):
        require_running("job:ingest-gleif", {"SYSTEM_STATE": state})


def test_missing_state_fails_closed():
    with pytest.raises(SystemHalted):
        require_running("job:x", {})


def test_unknown_state_fails_closed():
    with pytest.raises(SystemHalted):
        require_running("job:x", {"SYSTEM_STATE": "turbo"})


def test_component_pause_and_wildcard():
    env = {"SYSTEM_STATE": "running", "PAUSED_COMPONENTS": "agent:resolver-*,job:crawl-br"}
    with pytest.raises(SystemHalted):
        require_running("agent:resolver-in", env)
    with pytest.raises(SystemHalted):
        require_running("job:crawl-br", env)
    require_running("job:crawl-gb", env)


def test_state_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"state": "running", "paused_components": ["agent:*"], "monthly_budget_usd": 900}))
    env = {"LC_CONTROL_STATE_URI": str(p), "SYSTEM_STATE": "stopped"}  # file wins
    assert require_running("job:ingest-gb", env).monthly_budget_usd == 900
    with pytest.raises(SystemHalted):
        require_running("agent:maker-gb", env)


def test_corrupt_state_file_fails_closed(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    with pytest.raises(SystemHalted):
        require_running("job:x", {"LC_CONTROL_STATE_URI": str(p)})
