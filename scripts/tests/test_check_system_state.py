"""Deploys run only while the owner's control state (the cloud copy, the only one) says running."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_system_state", ROOT / "scripts" / "check_system_state.py")
css = importlib.util.module_from_spec(spec)
spec.loader.exec_module(css)


def state(word, paused=()):
    return json.dumps({"state": word, "paused_components": list(paused), "monthly_budget_usd": 1500})


def test_running_deploys():
    ok, line = css.decide("deploy:site", state("running"), enforced=True)
    assert ok and "running" in line


@pytest.mark.parametrize("word", ["paused", "stopped", "manual"])
def test_anything_but_running_stops(word):
    ok, line = css.decide("deploy:site", state(word), enforced=False)
    assert not ok and line.startswith("::error::") and word in line


@pytest.mark.parametrize("paused", [["deploy:site"], ["deploy:*"]])
def test_a_paused_deploy_stops(paused):
    assert not css.decide("deploy:site", state("running", paused), enforced=False)[0]
    assert css.decide("deploy:gateway", state("running", ["deploy:site"]), enforced=False)[0]


@pytest.mark.parametrize("text", ["{not json", "", json.dumps({"state": "turbo"})])
def test_a_malformed_state_fails_closed_even_before_enforcement(text):
    assert not css.decide("deploy:site", text, enforced=False)[0]


def test_unreadable_state_warns_until_enforced():
    ok, line = css.decide("deploy:site", None, enforced=False)
    assert ok and line.startswith("::warning::")
    ok, line = css.decide("deploy:site", None, enforced=True)
    assert not ok and line.startswith("::error::")


def test_the_log_never_shows_the_budget():
    for enforced in (False, True):
        assert "1500" not in css.decide("deploy:site", state("paused"), enforced)[1]


def test_main_reads_the_project_control_bucket(monkeypatch, capsys):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=state("running"), stderr="")

    monkeypatch.setattr(css.subprocess, "run", fake_run)
    monkeypatch.setenv("GCP_PROJECT_ID", "lei-commons-prod")
    monkeypatch.delenv("STATE_CHECK_ENFORCED", raising=False)
    assert css.main(["deploy:site"]) == 0
    assert seen == [["gcloud", "storage", "cat", "gs://lei-commons-prod-control/state.json"]]


def test_main_unreadable_then_enforced(monkeypatch):
    monkeypatch.setattr(css.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="403"))
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("STATE_CHECK_ENFORCED", "false")
    assert css.main(["deploy:site"]) == 0
    monkeypatch.setenv("STATE_CHECK_ENFORCED", "true")
    assert css.main(["deploy:site"]) == 1


def test_main_needs_a_deploy_component():
    assert css.main([]) == 2
    assert css.main(["job:ingest-gb"]) == 2


def test_every_deploy_workflow_checks_the_state_after_signing_in():
    flows = sorted((ROOT / ".github" / "workflows").glob("deploy-*.yml"))
    assert flows
    for f in flows:
        text = f.read_text()
        name = f.stem.removeprefix("deploy-")
        check = text.find(f"python3 scripts/check_system_state.py deploy:{name}\n")
        assert check != -1, f"{f.name} doesn't check the control state"
        assert text.find("google-github-actions/setup-gcloud") < check, f"{f.name} checks before signing in"
        for step in ("docker build", "gcloud run ", "gcloud workflows deploy"):
            first = text.find(step)
            assert first == -1 or first > check, f"{f.name} runs `{step}` before the check"
        assert "STATE_CHECK_ENFORCED: ${{ vars.STATE_CHECK_ENFORCED }}" in text
        assert "workflow_dispatch" not in text, f"{f.name} can still be started by hand"


def test_no_workflow_runs_cloud_jobs_by_hand():
    for f in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = f.read_text()
        if "workflow_dispatch" in text:
            assert f.name == "status.yml", f"{f.name} can be started by hand"
