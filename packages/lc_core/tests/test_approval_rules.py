"""config/approvals.yaml: which changes need the owner's approval (lc_core.approval_rules)."""

import pytest
import yaml

from lc_core.approval_rules import load


def test_the_real_rules_load_and_cover_the_sensitive_parts():
    rules = load()
    assert rules.context == "owner-approval" and rules.app_name == "lei-commons-guardian"
    sensitive = [
        ".github/workflows/deploy-site.yml", "infra/terraform/site_lb.tf", "bootstrap/bootstrap.sh",
        "config/sources.yaml", "config/approvals.yaml", "constitution.md", "scripts/check_system_state.py",
        "services/conduct_proxy/conduct_proxy/upstream.py", "services/console/lc_console/server.py",
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
