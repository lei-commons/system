"""Set a pull request's owner-approval status (config/approvals.yaml), as .github/workflows/owner-approval.yml.

    python3 scripts/owner_approval_status.py

Reads GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER and PR_SHA. Lists the pull request's changed files and, with
main's copy of the rules (lc_core.approval_rules):
- no sensitive path: the status is success at once;
- only the promotion of sources that passed their trial (lc_core.approval_rules.promotion_only, owner decision
  2026-11-22): success at once too, naming the promoted sources;
- a sensitive path: the status is pending, the pull request is assigned to the owner, and one comment lists the
  sensitive files and links to the admin page, where the owner approves it with a fresh authenticator code;
  the site then sets the status to success as the GitHub App (lc_site.approvals).
An approval the App already gave this exact commit is left as it is. Prints counts and paths only.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "lc_core"))

from lc_core.approval_rules import PROMOTION_FILES, load, promotion_only  # noqa: E402

API = "https://api.github.com"
MARKER = "<!-- owner-approval -->"
MAX_FILES = 3000  # GitHub lists at most 3,000 changed files


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, path: str, body=None):
        request = urllib.request.Request(API + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.token}",
                                                  "Accept": "application/vnd.github+json",
                                                  "X-GitHub-Api-Version": "2022-11-28",
                                                  "User-Agent": "lei-commons-owner-approval"})
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read()
        return json.loads(text) if text else None


UNLISTED = "(GitHub can't list this pull request's files: its diff is too large)"


def changed_paths(gh, repo: str, number: int) -> list | None:
    """The changed paths, or None when GitHub can't list them (a diff too large: HTTP 422)."""
    import urllib.error

    paths, page = [], 1
    while len(paths) < MAX_FILES:
        try:
            batch = gh.call("GET", f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}")
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                return None
            raise
        if not batch:
            break
        for f in batch:
            paths.append(f["filename"])
            if f.get("previous_filename"):  # a rename moves a file out of a sensitive path too
                paths.append(f["previous_filename"])
        if len(batch) < 100:
            break
        page += 1
    return paths


def file_text(gh, repo: str, path: str, ref: str):
    """A file's text at ``ref``, or None if it isn't there."""
    import urllib.error

    try:
        item = gh.call("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not item or item.get("encoding") != "base64":
        raise ValueError(f"{path}: the contents API gave no text (too big?)")
    return base64.b64decode(item["content"]).decode("utf-8")


def promoted_sources(gh, repo: str, number: int, sha: str, paths) -> list | None:
    """The sources this pull request promotes, if that's all it does (compared with its merge base)."""
    if not set(PROMOTION_FILES) & set(paths):
        return None
    pull = gh.call("GET", f"/repos/{repo}/pulls/{number}")
    merge_base = gh.call("GET", f"/repos/{repo}/compare/{pull['base']['ref']}...{sha}")["merge_base_commit"]["sha"]
    base = {p: file_text(gh, repo, p, merge_base) for p in PROMOTION_FILES}
    head = {p: file_text(gh, repo, p, sha) for p in PROMOTION_FILES}
    return promotion_only(paths, base, head)


def already_approved(gh, repo: str, sha: str, rules) -> bool:
    statuses = gh.call("GET", f"/repos/{repo}/commits/{sha}/statuses?per_page=100") or []
    return any(s.get("context") == rules.context and s.get("state") == "success"
               and (s.get("creator") or {}).get("login") == f"{rules.app_name}[bot]" for s in statuses)


def run(gh, repo: str, number: int, sha: str, rules, admin_url: str, log=print) -> str:
    """Set the status; return what was decided: not-needed, promotion, approved or pending."""
    paths = changed_paths(gh, repo, number)
    # Files that can't be listed can't be judged: the owner approves (fail closed).
    sensitive = [UNLISTED] if paths is None else rules.sensitive(paths)
    if not sensitive:
        gh.call("POST", f"/repos/{repo}/statuses/{sha}", {"state": "success", "context": rules.context,
                                                          "description": "No sensitive paths: no approval needed"})
        log("not-needed: no sensitive paths")
        return "not-needed"
    promoted = promoted_sources(gh, repo, number, sha, paths) if paths is not None else None
    if promoted:
        gh.call("POST", f"/repos/{repo}/statuses/{sha}", {
            "state": "success", "context": rules.context,
            "description": f"Promotes trial sources only ({', '.join(promoted)}): no approval needed"[:140]})
        log(f"promotion: {len(promoted)} sources promoted from trial, nothing else changed")
        return "promotion"
    if already_approved(gh, repo, sha, rules):
        log(f"approved: the owner already approved {sha[:7]}")
        return "approved"
    gh.call("POST", f"/repos/{repo}/statuses/{sha}", {
        "state": "pending", "context": rules.context, "target_url": admin_url,
        "description": f"Needs the owner's approval on the admin page ({len(sensitive)} sensitive files)"[:140]})
    gh.call("POST", f"/repos/{repo}/issues/{number}/assignees", {"assignees": [rules.owner_login]})
    comments = gh.call("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100") or []
    if not any(MARKER in (c.get("body") or "") for c in comments):
        shown = "\n".join(f"- `{p}`" for p in sensitive[:30]) + ("\n- …" if len(sensitive) > 30 else "")
        gh.call("POST", f"/repos/{repo}/issues/{number}/comments", {"body": (
            f"{MARKER}\n**Needs the owner's approval.** This pull request changes sensitive paths "
            f"(`config/approvals.yaml`):\n\n{shown}\n\nApprove it on the admin page with a fresh authenticator code: "
            f"{admin_url}\n\nThe approval covers this pull request's head commit; a new push needs a new one.")})
    log(f"pending: {len(sensitive)} sensitive paths; assigned to the owner")
    return "pending"


def admin_url() -> str:
    import yaml

    domain = yaml.safe_load((ROOT / "config" / "site.yaml").read_text(encoding="utf-8"))["domain"]
    return f"https://{domain}/admin/#/approvals"


def main() -> int:
    env = os.environ
    repo, number, sha = env["GITHUB_REPOSITORY"], int(env["PR_NUMBER"]), env["PR_SHA"]
    run(GitHub(env["GITHUB_TOKEN"]), repo, number, sha, load(), admin_url())
    return 0


if __name__ == "__main__":
    sys.exit(main())
