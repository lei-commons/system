"""scripts/record_change.py: every merge, deploy and Terraform apply goes into the decision log."""

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("record_change", ROOT / "scripts" / "record_change.py")
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

HEAD = rc._git("rev-parse", "HEAD")


def test_a_deploy_is_recorded_with_its_commit_and_how_it_ended():
    env = {"GITHUB_SHA": HEAD, "GITHUB_RUN_ID": "77", "OUTCOME": "success"}
    e = rc.event("deploy", ["site"], env)
    assert e["actor"] == "github:deploy-site" and e["action"] == "deployed" and e["decision"] == "done"
    assert e["subject"] == f"site at {HEAD[:7]}" and e["commit"] == HEAD and e["run_id"] == "77"
    assert e["reason"] == rc._git("log", "-1", "--format=%s", HEAD)[:200]
    assert rc.event("deploy", ["site"], {**env, "OUTCOME": "failure"})["decision"] == "failed"
    assert rc.event("deploy", ["site"], {**env, "OUTCOME": "cancelled"})["decision"] == "stopped"
    assert "(the deploy failure)" in rc.event("deploy", ["site"], {**env, "OUTCOME": "failure"})["reason"]


def test_a_merge_names_its_pull_request_and_counts_sensitive_files(monkeypatch):
    monkeypatch.setattr(rc, "_commit", lambda env: {"sha": "abc1234def", "short": "abc1234", "pr": 125,
                                                     "title": "Every change in the decision log (#125)"})
    monkeypatch.setattr(rc, "_changed_paths", lambda sha: ["docs/admin.md", "infra/terraform/iam.tf", ".github/workflows/changes.yml"])
    e = rc.event("merge", [], {"GITHUB_REPOSITORY": "lei-commons/system"})
    assert e["actor"] == "github:system" and e["subject"] == "system#125" and e["decision"] == "merged"
    assert e["files"] == 3 and e["sensitiveFiles"] == 2 and e["trigger"] == "push:main"


def test_a_terraform_apply_is_summarised_by_counts_and_resource_types():
    plan = {"resource_changes": [
        {"type": "google_storage_bucket_iam_member", "change": {"actions": ["create"]}},
        {"type": "google_cloud_run_v2_service", "change": {"actions": ["update"]}},
        {"type": "google_compute_backend_service", "change": {"actions": ["delete", "create"]}},
        {"type": "google_project_service", "change": {"actions": ["no-op"]}}]}
    summary = rc.plan_summary("day0.plan", show=lambda p: json.dumps(plan))
    assert summary == ("2 added, 1 changed, 1 destroyed "
                       "(cloud_run_v2_service, compute_backend_service, storage_bucket_iam_member)")
    assert rc.plan_summary("x", show=lambda p: "not json") == "plan applied"


def test_the_event_is_a_valid_chained_run_object():
    from lc_core.decisions import chain_text, object_name
    from lc_core.events import verify

    e = rc.event("deploy", ["load-db"], {"GITHUB_SHA": HEAD, "GITHUB_RUN_ID": "5", "OUTCOME": "success"},
                 now="2026-11-22T04:00:00Z")
    lines = chain_text([e]).splitlines()
    assert verify(lines)[0] and object_name([e], "5") == "runs/2026-11-22/2026-11-22T040000.000Z-github-deploy-load-db-5.jsonl"


def test_recording_never_stops_anything(monkeypatch, capsys):
    assert rc.main(["deploy", "site"], {"GITHUB_SHA": HEAD}) == 0  # no project: warns
    assert "not recorded" in capsys.readouterr().out

    def broken(record, project, label):
        raise RuntimeError("AccessDenied")
    monkeypatch.setattr(rc, "upload", broken)
    assert rc.main(["deploy", "site"], {"GITHUB_SHA": HEAD, "GCP_PROJECT_ID": "p"}) == 0
    assert "not recorded in the decision log (RuntimeError: AccessDenied)" in capsys.readouterr().out


def test_the_changes_workflow_records_every_deploy_workflow_and_every_merge():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "changes.yml").read_text())
    triggers = wf.get("on") or wf.get(True)
    deploys = sorted(yaml.safe_load(p.read_text())["name"] for p in (ROOT / ".github" / "workflows").glob("deploy-*.yml"))
    assert sorted(triggers["workflow_run"]["workflows"]) == deploys
    assert triggers["workflow_run"]["types"] == ["completed"] and triggers["push"]["branches"] == ["main"]
    assert set(wf["permissions"]) == {"contents", "id-token"} and wf["permissions"]["contents"] == "read"
    text = (ROOT / ".github" / "workflows" / "changes.yml").read_text()
    assert "${{ github.event.workflow_run.name }}" not in text.split("run: |", 1)[1]  # passed through env only


def test_the_bootstrap_records_its_terraform_apply_and_the_deployer_may_write_the_log():
    boot = (ROOT / "bootstrap" / "bootstrap.sh").read_text()
    apply_at = boot.index("terraform apply -input=false day0.plan")
    assert "scripts/record_change.py terraform day0.plan || true" in boot[apply_at:apply_at + 400]
    iam = (ROOT / "infra" / "terraform" / "iam.tf").read_text()
    line = next(x for x in iam.splitlines() if '"roles/storage.objectCreator"' in x and '"site"' in x and '"loader"' in x)
    assert '"deployer"' in line


def test_the_upload_creates_one_object_and_needs_create_permission_only(monkeypatch):
    import urllib.error
    import urllib.parse

    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def urlopen(request, timeout):
        sent.append(request)
        return Response()
    monkeypatch.setattr(rc.urllib.request, "urlopen", urlopen)
    e = rc.event("deploy", ["site"], {"GITHUB_SHA": HEAD, "GITHUB_RUN_ID": "9", "OUTCOME": "success"}, now="2026-11-22T04:30:00Z")
    name = rc.upload(e, "p", "9", token="t")
    url = urllib.parse.urlparse(sent[0].full_url)
    query = dict(urllib.parse.parse_qsl(url.query))
    assert url.path == "/upload/storage/v1/b/p-decision-log/o" and sent[0].get_method() == "POST"
    assert query == {"uploadType": "media", "name": name, "ifGenerationMatch": "0"}  # create, never overwrite
    assert sent[0].data.decode() == rc.chain_text([e]) and sent[0].get_header("Authorization") == "Bearer t"

    def denied(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(rc.urllib.request, "urlopen", denied)
    try:
        rc.upload(e, "p", "9", token="t")
    except RuntimeError as exc:
        assert str(exc) == "HTTP 403 Forbidden"  # no account names in the public log
    else:
        raise AssertionError("a refused upload must raise")
