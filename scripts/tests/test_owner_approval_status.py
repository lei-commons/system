"""The owner-approval workflow's script: pending and assigned for sensitive changes, success otherwise."""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("owner_approval_status", ROOT / "scripts" / "owner_approval_status.py")
oas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oas)
RULES = oas.load()
REPO, SHA = "lei-commons/system", "abc1234def"


class FakeGitHub:
    def __init__(self, files, statuses=(), comments=(), contents=None):
        self.files, self.statuses, self.comments, self.calls = files, list(statuses), list(comments), []
        self.contents = contents or {}  # {(path, ref): text}

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == f"/repos/{REPO}/pulls/7":
            return {"base": {"ref": "main"}}
        if method == "GET" and "/compare/" in path:
            return {"merge_base_commit": {"sha": "base999"}}
        if method == "GET" and "/contents/" in path:
            name, ref = path.split("/contents/", 1)[1].split("?ref=")
            text = self.contents.get((name, ref))
            if text is None:
                import urllib.error
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
            import base64
            return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        if method == "GET" and "/files" in path:
            page = int(path.rsplit("page=", 1)[1])
            return self.files[(page - 1) * 100: page * 100]
        if method == "GET" and "/statuses" in path:
            return self.statuses
        if method == "GET" and "/comments" in path:
            return self.comments
        if method == "POST" and "/comments" in path:
            self.comments.append(body)
        return {}

    def posted(self, kind):
        return [b for m, p, b in self.calls if m == "POST" and f"/{kind}" in p]


def run(gh):
    return oas.run(gh, REPO, 7, SHA, RULES, "https://site.test/admin/#/approvals", log=lambda *a: None)


def test_ordinary_changes_pass_at_once():
    gh = FakeGitHub([{"filename": "docs/admin.md"}, {"filename": "services/ingest_ny/ingest_ny/job.py"}])
    assert run(gh) == "not-needed"
    assert gh.posted("statuses") == [{"state": "success", "context": "owner-approval",
                                      "description": "No sensitive paths: no approval needed"}]
    assert gh.posted("assignees") == [] and gh.posted("comments") == []


def test_sensitive_changes_wait_for_the_owner_and_comment_once():
    gh = FakeGitHub([{"filename": "docs/admin.md"}, {"filename": "infra/terraform/site_lb.tf"}])
    assert run(gh) == "pending"
    (status,) = gh.posted("statuses")
    assert status["state"] == "pending" and status["target_url"].endswith("/admin/#/approvals")
    assert gh.posted("assignees") == [{"assignees": [RULES.owner_login]}]
    (comment,) = gh.posted("comments")
    assert "`infra/terraform/site_lb.tf`" in comment["body"] and "docs/admin.md" not in comment["body"]
    run(gh)  # a new push: pending again, but no second comment
    assert len(gh.posted("comments")) == 1 and len(gh.posted("statuses")) == 2


def test_a_rename_out_of_a_sensitive_path_counts():
    gh = FakeGitHub([{"filename": "docs/old-ci.yml", "previous_filename": ".github/workflows/ci.yml"}])
    assert run(gh) == "pending"


def test_only_the_apps_approval_of_this_commit_counts():
    files = [{"filename": "config/sources.yaml"}]
    by_app = {"context": "owner-approval", "state": "success", "creator": {"login": "lei-commons-guardian[bot]"}}
    assert run(FakeGitHub(files, statuses=[by_app])) == "approved"
    by_someone = {**by_app, "creator": {"login": "pramodkvarma"}}  # a token holder can't approve for the owner
    assert run(FakeGitHub(files, statuses=[by_someone])) == "pending"


def test_many_files_are_read_page_by_page():
    files = [{"filename": f"docs/f{i}.md"} for i in range(250)] + [{"filename": "constitution.md"}]
    gh = FakeGitHub(files)
    assert run(gh) == "pending"
    assert len([1 for m, p, b in gh.calls if "/files" in p]) == 3


def test_the_workflow_runs_mains_rules_never_the_pull_requests_code():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "owner-approval.yml").read_text())
    triggers = wf.get("on") or wf.get(True)
    assert list(triggers) == ["pull_request_target", "workflow_call"]
    steps = wf["jobs"]["owner-approval"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["repository"] == "lei-commons/system" and checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] is False
    text = (ROOT / ".github" / "workflows" / "owner-approval.yml").read_text()
    assert "head.ref" not in text and "pull_request.head.repo" not in text
    assert set(wf["permissions"]) == {"contents", "statuses", "pull-requests", "issues"}


def test_the_private_repositories_call_system_mains_gate():
    wf = yaml.safe_load((ROOT / "private-repos" / "owner-approval.yml").read_text())
    triggers = wf.get("on") or wf.get(True)
    assert list(triggers) == ["pull_request_target"]
    assert wf["jobs"] == {"owner-approval": {"uses": "lei-commons/system/.github/workflows/owner-approval.yml@main"}}
    # The called workflow can only use the permissions given here, and needs all of them.
    called = yaml.safe_load((ROOT / ".github" / "workflows" / "owner-approval.yml").read_text())
    assert wf["permissions"] == called["permissions"]
    install = (ROOT / "private-repos" / "install.sh").read_text()
    repos = install.split('REPOS="', 1)[1].split('"', 1)[0].split()
    assert "control" not in repos and {"releases", "intake"} <= set(repos)
    from lc_core.approval_rules import load
    rules = load()
    assert rules.sensitive(["private-repos/install.sh", "ledger/sync.yml", ".github/workflows/snapshot.yml"]) == [
        ".github/workflows/snapshot.yml", "ledger/sync.yml", "private-repos/install.sh"]


def test_a_promotion_of_trial_sources_passes_without_the_owner():
    registry = (ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(registry)
    trial = next(s for s in data["sources"] if (s.get("pipeline") or {}).get("stage") == "trial")
    trial["pipeline"]["stage"] = "production"
    promoted = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    contents = {("config/sources.yaml", "base999"): registry, ("config/sources.yaml", SHA): promoted}
    gh = FakeGitHub([{"filename": "config/sources.yaml"}], contents=contents)
    assert run(gh) == "promotion"
    (status,) = gh.posted("statuses")
    assert status["state"] == "success" and status["description"].startswith("Promotes trial sources only")
    assert trial["id"] in status["description"]
    assert gh.posted("assignees") == [] and gh.posted("comments") == []


def test_a_registry_change_beyond_promotion_still_waits_for_the_owner():
    registry = (ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")
    contents = {("config/sources.yaml", "base999"): registry,
                ("config/sources.yaml", SHA): registry.replace("stage: trial", "stage: production", 1) + "\n# x\n"}
    gh = FakeGitHub([{"filename": "config/sources.yaml"}, {"filename": "scripts/render_cycle.py"}], contents=contents)
    assert run(gh) == "pending"
