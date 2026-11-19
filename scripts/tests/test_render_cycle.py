"""The daily cycle: every source independent, failures retried then skipped, all numbers from config/cycle.yaml."""

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("render_cycle", ROOT / "scripts" / "render_cycle.py")
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

REGISTRY = {"sources": [{"id": "a", "pipeline": {"job": "ingest-aa"}}, {"id": "b"}, {"id": "c", "pipeline": {"job": "ingest-cc"}}]}
CONFIG = {"schedule_utc": "02:00", "sources": {"retry_wait_minutes": 10, "retries": 5, "browser_fallback_after_failures": 3},
          "gate": {"clean_days": 4}, "watchdogs": {"dedi_check_utc": "14:17"}}


def steps(doc):
    return {k: v for step in doc["main"]["steps"] for k, v in step.items()}


def all_step_names(node, out):
    if isinstance(node, dict):
        if "steps" in node and isinstance(node["steps"], list):
            for step in node["steps"]:
                if isinstance(step, dict):
                    out.extend(step.keys())
        for v in node.values():
            all_step_names(v, out)
    elif isinstance(node, list):
        for v in node:
            all_step_names(v, out)
    return out


def test_every_source_is_tried_and_a_failure_is_caught_not_fatal():
    s = steps(rc.render(REGISTRY, CONFIG))
    init = {k: v for a in s["init"]["assign"] for k, v in a.items()}
    assert init["sources"] == ["ingest-aa", "ingest-cc"] and init["failed"] == []
    run = s["first_try"]["for"]["steps"][0]["first_run"]
    assert run["try"]["call"] == "googleapis.run.v2.projects.locations.jobs.run"
    assert "except" in run and "list.concat(failed, job)" in str(run["except"])
    assert "source-failed" in str(run["except"])


def test_retries_and_wait_come_from_the_config():
    s = steps(rc.render(REGISTRY, CONFIG))
    loop = s["retry_rounds"]["for"]
    assert loop["range"] == [1, 5]
    inner = {k: v for step in loop["steps"] for k, v in step.items()}
    assert inner["wait"]["args"]["seconds"] == 600
    assert inner["all_done"]["switch"][0]["next"] == "break"  # stop as soon as nothing is failing
    other = copy.deepcopy(CONFIG)
    other["sources"] = {"retry_wait_minutes": 3, "retries": 2, "browser_fallback_after_failures": 3}
    s2 = steps(rc.render(REGISTRY, other))
    assert s2["retry_rounds"]["for"]["range"] == [1, 2]
    other["sources"]["retries"] = 0
    assert "retry_rounds" not in steps(rc.render(REGISTRY, other))


def test_snapshot_and_load_run_after_the_sources_then_the_cycle_fails_if_any_source_never_succeeded():
    names = [next(iter(step)) for step in rc.render(REGISTRY, CONFIG)["main"]["steps"]]
    assert names == ["init", "first_try", "retry_rounds", "build_snapshot", "load_db", "report", "done"]
    s = steps(rc.render(REGISTRY, CONFIG))
    assert "try" not in s["build_snapshot"] and "try" not in s["load_db"]  # these still stop the cycle
    report = s["report"]["switch"][0]
    assert report["condition"] == "${len(failed) > 0}"
    assert report["steps"][0]["fail"]["raise"]["code"] == "sources-failed"


def test_step_names_are_unique():
    names = all_step_names(rc.render(REGISTRY, CONFIG), [])
    assert len(names) == len(set(names)), names


def test_the_selftest_uses_the_same_logic_on_a_real_and_two_missing_jobs_without_snapshot_or_load():
    s = steps(rc.render(REGISTRY, CONFIG, selftest=True))
    init = {k: v for a in s["init"]["assign"] for k, v in a.items()}
    assert init["sources"] == rc.SELFTEST_JOBS and "build_snapshot" not in s and "load_db" not in s
    assert s["retry_rounds"]["for"]["range"] == [1, 1]
    assert init["fallbacks"] == {"ingest-selftest-missing": "egress-check"} and "browser_fallbacks" in s


def test_approved_fallbacks_run_after_the_retries_and_before_the_snapshot():
    doc = rc.render(REGISTRY, CONFIG, fallbacks={"ingest-aa": "ingest-aa-browser"})
    names = [next(iter(step)) for step in doc["main"]["steps"]]
    assert names == ["init", "first_try", "retry_rounds", "browser_fallbacks", "build_snapshot", "load_db", "report", "done"]
    init = {k: v for a in steps(doc)["init"]["assign"] for k, v in a.items()}
    assert init["fallbacks"] == {"ingest-aa": "ingest-aa-browser"}
    text = str(steps(doc)["browser_fallbacks"])
    assert "${job in fallbacks}" in text and "fallbacks[job]" in text and "source-recovered-by-browser" in text
    assert len(all_step_names(doc, [])) == len(set(all_step_names(doc, [])))
    assert "browser_fallbacks" not in steps(rc.render(REGISTRY, CONFIG))  # none approved: no fallback round


@pytest.mark.parametrize("change,message", [
    ({"sources": {"retry_wait_minutes": 0, "retries": 5, "browser_fallback_after_failures": 3}}, "retry_wait_minutes"),
    ({"sources": {"retry_wait_minutes": 10, "retries": 13, "browser_fallback_after_failures": 3}}, "retries"),
    ({"sources": {"retry_wait_minutes": "10", "retries": 5, "browser_fallback_after_failures": 3}}, "retry_wait_minutes"),
    ({"sources": {"retry_wait_minutes": 10, "retries": True, "browser_fallback_after_failures": 3}}, "retries"),
    ({"gate": {"clean_days": 0}}, "clean_days"),
    ({"sources": {"retry_wait_minutes": 10, "retries": 5, "browser_fallback_after_failures": 0}}, "browser_fallback_after_failures"),
    ({"schedule_utc": "2:00"}, "schedule_utc"),
    ({"schedule_utc": "24:00"}, "schedule_utc"),
])
def test_bad_settings_are_refused(change, message):
    config = {**copy.deepcopy(CONFIG), **change}
    with pytest.raises(rc.ConfigError, match=message):
        rc.render(REGISTRY, config)


def test_a_duplicate_or_odd_job_name_is_refused():
    with pytest.raises(rc.ConfigError):
        rc.render({"sources": [{"pipeline": {"job": "ingest-aa"}}, {"pipeline": {"job": "ingest-aa"}}]}, CONFIG)
    with pytest.raises(rc.ConfigError):
        rc.render({"sources": [{"pipeline": {"job": "ingest-a; rm"}}]}, CONFIG)


def test_the_committed_cycle_matches_the_registry_and_config():
    assert rc.main(["--check"]) == 0
    doc = yaml.safe_load((ROOT / "infra" / "workflows" / "daily-cycle.yaml").read_text())
    registry = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
    jobs = [s["pipeline"]["job"] for s in registry["sources"] if (s.get("pipeline") or {}).get("job")]
    init = {k: v for a in steps(doc)["init"]["assign"] for k, v in a.items()}
    assert init["sources"] == jobs


def test_terraform_deploys_the_rendered_file_and_takes_the_schedule_from_the_config():
    tf = (ROOT / "infra" / "terraform" / "cycle.tf").read_text()
    assert 'file("${path.module}/../workflows/daily-cycle.yaml")' in tf
    assert "local.cycle_cron" in tf and '"0 2 * * *"' not in tf



def test_the_dedi_check_runs_after_the_load_and_never_fails_the_cycle():
    doc = rc.render(REGISTRY, CONFIG, dedi=True)
    names = [next(iter(step)) for step in doc["main"]["steps"]]
    assert names[names.index("load_db") + 1] == "dedi_check" and names.index("dedi_check") < names.index("report")
    step = steps(doc)["dedi_check"]
    assert "try" in step and "except" in step and "dedi-check-failed" in str(step["except"])
    assert "list.concat(failed" not in str(step["except"])  # never adds to the failed sources
    assert "dedi_check" not in steps(rc.render(REGISTRY, CONFIG))  # DeDi off: no step
    assert "dedi_check" not in steps(rc.render(REGISTRY, CONFIG, selftest=True, dedi=True))  # the self-test never
