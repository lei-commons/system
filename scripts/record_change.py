"""Record a change to the system in the decision log: a deploy, a merge into main, or a Terraform apply.

    python3 scripts/record_change.py deploy <name>          # .github/workflows/changes.yml, when a deploy finishes
    python3 scripts/record_change.py merge                  # .github/workflows/changes.yml, on every push to main
    python3 scripts/record_change.py terraform <plan file>  # bootstrap/bootstrap.sh, after terraform apply

Every change goes into the same chained log as the jobs' decisions and the owner's actions (owner, 2026-11-22):
one new run object, ``runs/<day>/…``, in ``gs://<project>-decision-log``, sealed into the day's chain the next
morning (lc_core.decisions). Reads GCP_PROJECT_ID and, from GitHub Actions, GITHUB_SHA, GITHUB_RUN_ID,
GITHUB_REPOSITORY and OUTCOME (how the deploy ended: success, failure, cancelled or skipped).

Recording never stops a deploy: if the log can't be written (for example before the bootstrap rerun lets the
deployer write it), this warns and exits 0. It prints the object's name only, because the logs are public.
Standard library plus lc_core from this checkout, and gcloud for the upload.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "lc_core"))

from lc_core.decisions import chain_text, object_name  # noqa: E402

DECISION = {"success": "done", "failure": "failed", "cancelled": "stopped", "skipped": "skipped"}


def _git(*args) -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _commit(env) -> dict:
    """The commit being deployed or merged: its short hash, its title, and its pull request if the title names one."""
    sha = env.get("GITHUB_SHA") or _git("rev-parse", "HEAD")
    title = _git("log", "-1", "--format=%s", sha) if sha else ""
    pr = re.search(r"\(#(\d+)\)\s*$", title)
    return {"sha": sha, "short": sha[:7], "title": title[:200], "pr": int(pr.group(1)) if pr else None}


def _changed_paths(sha: str) -> list:
    out = _git("diff", "--name-only", f"{sha}~1", sha) if sha else ""
    return [p for p in out.splitlines() if p.strip()]


def plan_summary(plan: str, show=None) -> str:
    """What a saved Terraform plan changed, as Terraform counts it: "3 added, 2 changed, 1 destroyed" (a
    replacement counts as both added and destroyed). Resource types only, never values."""
    import json

    try:
        text = show(plan) if show else subprocess.run(["terraform", "show", "-json", plan], capture_output=True,
                                                      text=True, timeout=300, check=True).stdout
        changes = [r for r in json.loads(text).get("resource_changes", []) if r["change"]["actions"] != ["no-op"]]
    except Exception:
        return "plan applied"
    count = lambda action: sum(1 for r in changes if action in r["change"]["actions"])  # noqa: E731
    kinds = sorted({r["type"].removeprefix("google_") for r in changes})
    return (f"{count('create')} added, {count('update')} changed, {count('delete')} destroyed"
            + (f" ({', '.join(kinds[:12])}{', …' if len(kinds) > 12 else ''})" if kinds else ""))


def event(kind: str, args: list, env, now=None) -> dict:
    """The event to record for ``kind`` (deploy, merge or terraform)."""
    repo = (env.get("GITHUB_REPOSITORY") or "lei-commons/system").split("/")[-1]
    base = {"trigger": "push:main" if kind != "terraform" else "owner:bootstrap", "run_id": env.get("GITHUB_RUN_ID", "")}
    if now:
        base["ts"] = now
    if kind == "deploy":
        if len(args) != 1:
            raise SystemExit("usage: record_change.py deploy <name>")
        c, outcome = _commit(env), (env.get("OUTCOME") or "success").lower()
        return base | {"actor": f"github:deploy-{args[0]}", "action": "deployed", "subject": f"{args[0]} at {c['short']}",
                       "decision": DECISION.get(outcome, "failed"),
                       "reason": (c["title"] or "a push to main") + ("" if outcome == "success" else f" (the deploy {outcome})"),
                       "commit": c["sha"], "pullRequest": c["pr"]}
    if kind == "merge":
        c = _commit(env)
        paths = _changed_paths(c["sha"])
        try:
            from lc_core.approval_rules import load

            sensitive = len(load().sensitive(paths))
        except Exception:  # the rules can't be read: record the merge anyway, without the count
            sensitive = None
        subject = f"{repo}#{c['pr']}" if c["pr"] else f"{repo}@{c['short']}"
        return base | {"actor": f"github:{repo}", "action": "merged into main", "subject": subject, "decision": "merged",
                       "reason": c["title"] or "a push to main", "commit": c["sha"], "files": len(paths),
                       "sensitiveFiles": sensitive}
    if kind == "terraform":
        summary = plan_summary(args[0]) if args else ""
        outcome = (env.get("OUTCOME") or "success").lower()
        return base | {"actor": "owner", "action": "applied Terraform", "subject": "cloud configuration",
                       "decision": DECISION.get(outcome, "failed"),
                       "reason": f"bootstrap rerun: {summary or 'plan applied'}", "commit": _git("rev-parse", "HEAD")}
    raise SystemExit(f"unknown kind {kind!r}: deploy, merge or terraform")


def upload(record: dict, project: str, label: str) -> str:
    name = object_name([record], label)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(chain_text([record]))
        path = f.name
    try:
        out = subprocess.run(["gcloud", "storage", "cp", path, f"gs://{project}-decision-log/{name}",
                              "--if-generation-match=0", "--content-type=application/x-ndjson", "--quiet"],
                             capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(path)
    if out.returncode != 0:
        raise RuntimeError((out.stderr.strip().splitlines() or ["gcloud failed"])[-1][:160])
    return name


def main(argv=None, env=None) -> int:
    argv, env = list(sys.argv[1:] if argv is None else argv), os.environ if env is None else env
    if not argv:
        raise SystemExit(__doc__)
    record = event(argv[0], argv[1:], env)
    project = env.get("GCP_PROJECT_ID", "")
    if not project:
        print("::warning::not recorded in the decision log: GCP_PROJECT_ID is not set")
        return 0
    try:
        name = upload(record, project, label=env.get("GITHUB_RUN_ID") or argv[0])
    except Exception as exc:
        print(f"::warning::not recorded in the decision log ({type(exc).__name__}: {exc})")
        return 0
    print(f"recorded: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
