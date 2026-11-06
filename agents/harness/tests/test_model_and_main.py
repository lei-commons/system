from pathlib import Path

import pytest

from lc_harness.__main__ import build_system, main
from lc_harness.model import CLAUDE_MODELS, ClaudeVertex


class Stream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return "message"


class Client:
    def __init__(self):
        self.messages = self
        self.kwargs = None

    def stream(self, **kwargs):
        self.kwargs = kwargs
        return Stream()


def test_request_uses_adaptive_thinking_and_explicit_effort():
    c = Client()
    assert ClaudeVertex("p", client=c).create(system="s", tools=[], messages=[]) == "message"
    assert c.kwargs["model"] == "claude-opus-5-5"
    assert c.kwargs["thinking"] == {"type": "adaptive"}
    assert c.kwargs["output_config"] == {"effort": "high"}
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(c.kwargs)


def test_every_claude_class_in_models_yaml_is_mapped():
    import re

    text = (Path(__file__).resolve().parents[3] / "config" / "models.yaml").read_text()
    assert set(re.findall(r"model: (claude-[a-z]+)", text)) <= set(CLAUDE_MODELS)


def test_unknown_model_class_refused():
    with pytest.raises(ValueError):
        ClaudeVertex("p", "claude-unknown", client=Client())


def test_constitution_comes_first(tmp_path):
    (tmp_path / "agents" / "roles").mkdir(parents=True)
    (tmp_path / "constitution.md").write_text("RULES")
    (tmp_path / "agents" / "roles" / "x.md").write_text("ROLE")
    assert build_system(tmp_path, "x").index("RULES") < build_system(tmp_path, "x").index("ROLE")


def test_no_constitution_no_session(tmp_path):
    (tmp_path / "agents" / "roles").mkdir(parents=True)
    (tmp_path / "agents" / "roles" / "x.md").write_text("ROLE")
    with pytest.raises(FileNotFoundError):
        build_system(tmp_path, "x")


@pytest.mark.parametrize("env", [{"SYSTEM_STATE": "paused"}, {}])
def test_paused_or_unknown_state_never_builds_a_client(tmp_path, monkeypatch, env):
    monkeypatch.delenv("SYSTEM_STATE", raising=False)
    monkeypatch.delenv("LC_CONTROL_STATE_URI", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("lc_harness.__main__.ClaudeVertex", lambda *a: pytest.fail("client built"))
    assert main(["--role", "x", "--task-file", "t", "--trace", str(tmp_path / "t.jsonl")]) == 3
