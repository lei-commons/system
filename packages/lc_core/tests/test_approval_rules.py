"""config/approvals.yaml: which changes need the owner's approval (lc_core.approval_rules)."""

import pytest
import yaml

from pathlib import Path

from lc_core.approval_rules import CYCLE, REGISTRY, load, promotion_only


def test_the_real_rules_load_and_cover_the_sensitive_parts():
    rules = load()
    assert rules.context == "owner-approval" and rules.app_name == "lei-commons-guardian"
    sensitive = [
        ".github/workflows/deploy-site.yml", "infra/terraform/site_lb.tf", "bootstrap/bootstrap.sh",
        "config/sources.yaml", "config/approvals.yaml", "constitution.md", "scripts/check_system_state.py",
        "services/conduct_proxy/conduct_proxy/upstream.py",
        "packages/lc_core/lc_core/totp.py", "packages/lc_core/lc_core/second_factor.py",
        "packages/lc_core/lc_core/approval_rules.py", "services/site/lc_site/server.py",
        "services/site/lc_site/approvals.py", "services/site/Dockerfile", "agents/harness/main.py",
    ]
    ordinary = [
        "services/site/lc_site/static/views/search.js", "packages/lc_publish/lc_publish/snapshot.py",
        "services/ingest_ny/ingest_ny/job.py", "docs/admin.md", "README.md", "packages/lc_core/lc_core/sources.py",
        "packages/lc_core/tests/test_totp.py", "scripts/render_cycle.py",
    ]
    assert rules.sensitive(sensitive) == sorted(sensitive)
    assert rules.sensitive(ordinary) == []


def test_patterns_respect_directories():
    rules = load()
    assert rules.sensitive(["docs/constitution.md", "x/.github/workflows/a.yml", "configs/x.yaml"]) == []


@pytest.mark.parametrize("change,error", [
    ({"context": ""}, "context"),
    ({"sensitive_paths": []}, "sensitive_paths"),
    ({"sensitive_paths": ["/absolute"]}, "sensitive_paths"),
    ({"github_app": {"name": "x", "app_id": 0, "installation_id": 1, "key_secret": "s"}}, "app_id"),
    ({"github_app": {"name": "x", "app_id": 1, "installation_id": 1, "key_secret": ""}}, "key_secret"),
    ({"owner_login": ""}, "owner_login"),
])
def test_a_bad_config_is_refused(tmp_path, change, error):
    good = {"context": "owner-approval", "owner_login": "o", "sensitive_paths": ["infra/**"],
            "github_app": {"name": "x", "app_id": 1, "installation_id": 2, "key_secret": "s"}}
    path = tmp_path / "approvals.yaml"
    path.write_text(yaml.safe_dump({**good, **change}))
    with pytest.raises(ValueError, match=error):
        load(path)


# ---- promotion of trial sources: no approval needed (owner decision 2026-11-22) ----

ROOT = Path(__file__).resolve().parents[3]
REAL_REGISTRY = (ROOT / REGISTRY).read_text(encoding="utf-8")
REAL_CYCLE = (ROOT / CYCLE).read_text(encoding="utf-8")


def _a_trial(registry_text):
    data = yaml.safe_load(registry_text)
    return next(s for s in data["sources"] if (s.get("pipeline") or {}).get("stage") == "trial")


def _promote(source_id, registry_text=REAL_REGISTRY):
    data = yaml.safe_load(registry_text)
    for s in data["sources"]:
        if s["id"] == source_id:
            s["pipeline"]["stage"] = "production"
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _cycle_moving(job, cycle_text=REAL_CYCLE):
    data = yaml.safe_load(cycle_text)
    for step in data["main"]["steps"]:
        if "init" in step:
            for item in step["init"]["assign"]:
                if "sources" in item:
                    item["sources"].append(job)
        if "trials" in step:
            step["trials"]["for"]["in"].remove(job)
    data["main"]["steps"] = [s for s in data["main"]["steps"] if not ("trials" in s and not s["trials"]["for"]["in"])]
    return yaml.safe_dump(data, sort_keys=False)


def test_a_pure_promotion_needs_no_approval():
    trial = _a_trial(REAL_REGISTRY)
    base = {REGISTRY: REAL_REGISTRY, CYCLE: REAL_CYCLE}
    head = {REGISTRY: _promote(trial["id"]), CYCLE: _cycle_moving(trial["pipeline"]["job"])}
    changed = [REGISTRY, CYCLE, "services/ingest_bulk/tests/test_x.py", "docs/source-trials.md"]
    assert promotion_only(changed, base, head) == [trial["id"]]
    assert promotion_only([REGISTRY], base, {**head, CYCLE: REAL_CYCLE}) == [trial["id"]]  # cycle left alone


def test_anything_more_than_a_promotion_needs_the_owner():
    trial = _a_trial(REAL_REGISTRY)
    job = trial["pipeline"]["job"]
    base = {REGISTRY: REAL_REGISTRY, CYCLE: REAL_CYCLE}
    head = {REGISTRY: _promote(trial["id"]), CYCLE: _cycle_moving(job)}
    for extra in ("scripts/render_cycle.py", "packages/lc_ingest/lc_ingest/bulk.py", "config/egress-allowlist.yaml",
                  "infra/terraform/cycle.tf", "docs/tool.py"):
        assert promotion_only([REGISTRY, CYCLE, extra], base, head) is None, extra
    data = yaml.safe_load(head[REGISTRY])  # another field of the registry changed too
    next(s for s in data["sources"] if s["id"] == trial["id"])["pipeline"]["job"] = "something-else"
    assert promotion_only([REGISTRY, CYCLE], base, {**head, REGISTRY: yaml.safe_dump(data)}) is None
    demoted = yaml.safe_load(REAL_REGISTRY)  # production to trial is not a promotion
    prod = next(s for s in demoted["sources"] if (s.get("pipeline") or {}).get("stage") == "production")
    prod["pipeline"]["stage"] = "trial"
    assert promotion_only([REGISTRY], base, {REGISTRY: yaml.safe_dump(demoted), CYCLE: REAL_CYCLE}) is None
    assert promotion_only([REGISTRY], base, base) is None  # nothing promoted
    cycle = yaml.safe_load(head[CYCLE])  # the cycle changed beyond moving the job
    cycle["main"]["steps"].append({"extra": {"call": "sys.log"}})
    assert promotion_only([REGISTRY, CYCLE], base, {**head, CYCLE: yaml.safe_dump(cycle)}) is None
    kept = yaml.safe_load(REAL_CYCLE)  # the job still in the trials step
    for step in kept["main"]["steps"]:
        if "init" in step:
            for item in step["init"]["assign"]:
                if "sources" in item:
                    item["sources"].append(job)
    assert promotion_only([REGISTRY, CYCLE], base, {**head, CYCLE: yaml.safe_dump(kept)}) is None
    assert promotion_only([REGISTRY, CYCLE], base, {**head, REGISTRY: "not: [yaml"}) is None
