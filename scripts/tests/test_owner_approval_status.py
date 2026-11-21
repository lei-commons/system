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
    def __init__(self, files, statuses=(), comments=()):
        self.files, self.statuses, self.comments, self.calls = files, list(statuses), list(comments), []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
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
    assert list(triggers) == ["pull_request_target"]
    steps = wf["jobs"]["owner-approval"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    text = (ROOT / ".github" / "workflows" / "owner-approval.yml").read_text()
    assert "head.ref" not in text and "pull_request.head.repo" not in text
    assert set(wf["permissions"]) == {"contents", "statuses", "pull-requests", "issues"}
