"""The admin area's Approvals page: sensitive pull requests waiting for the owner (config/approvals.yaml).

GET  /api/v1/admin/approvals  open pull requests, in every repository the guardian App can see, that change a
                              sensitive path: title, link, head commit, the sensitive files, and whether the
                              owner has approved that commit.
POST /api/v1/admin/approvals  {"repo", "number", "sha", "reason", "code"}, same-origin JSON: the owner approves
                              that pull request's head commit with a fresh authenticator code. The commit must
                              still be the head (what the owner saw is what gets approved). The site then sets
                              the commit's owner-approval status to success as the guardian App, and records
                              the decision. A new push needs a new approval.

The site talks to GitHub only as the guardian App (statuses: write; pull requests and contents: read), whose
private key is in Secret Manager; it never holds a token that can merge, push or change settings.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone

from lc_core.owner import Outcome

API = "https://api.github.com"
MAX_BODY = 4096
CACHE_SECONDS = 60
MAX_FILES = 3000


class GitHubApp:
    """Calls GitHub as one installation of a GitHub App: a JWT signed with the App's key buys a one-hour token."""

    def __init__(self, app_id: int, installation_id: int, read_key, clock=time.time, opener=None):
        self.app_id, self.installation_id, self._read_key, self._clock = app_id, installation_id, read_key, clock
        self._open = opener or (lambda request: urllib.request.urlopen(request, timeout=30))
        self._token, self._expires = None, 0.0
        self._lock = threading.Lock()

    def _jwt(self) -> str:
        from google.auth import crypt, jwt

        now = int(self._clock())
        signer = crypt.RSASigner.from_string(self._read_key())
        return jwt.encode(signer, {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}).decode()

    def _request(self, method, path, token, body=None, scheme="Bearer"):
        request = urllib.request.Request(API + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"{scheme} {token}",
                                                  "Accept": "application/vnd.github+json",
                                                  "X-GitHub-Api-Version": "2022-11-28",
                                                  "User-Agent": "lei-commons-admin"})
        with self._open(request) as response:
            text = response.read()
        return json.loads(text) if text else None

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() > self._expires - 300:
                answer = self._request("POST", f"/app/installations/{self.installation_id}/access_tokens", self._jwt())
                self._token = answer["token"]
                self._expires = datetime.fromisoformat(answer["expires_at"].replace("Z", "+00:00")).timestamp()
            return self._token

    def call(self, method, path, body=None):
        return self._request(method, path, self.token(), body)


class Approvals:
    def __init__(self, *, github, rules, verifier, read_secret, record, admin_url, clock=None, log=None):
        self.github, self.rules, self.verifier, self._read_secret = github, rules, verifier, read_secret
        self._record, self.admin_url = record, admin_url
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = log or (lambda r: print(json.dumps(r, sort_keys=True), flush=True))
        self._cache, self._cached_at = None, 0.0
        self._lock = threading.Lock()

    # ---- reading ----
    def _files(self, repo, number):
        paths, page = [], 1
        while len(paths) < MAX_FILES:
            batch = self.github.call("GET", f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
            for f in batch:
                paths.append(f["filename"])
                if f.get("previous_filename"):
                    paths.append(f["previous_filename"])
            if len(batch) < 100:
                break
            page += 1
        return paths

    def _approved(self, repo, sha) -> bool:
        statuses = self.github.call("GET", f"/repos/{repo}/commits/{sha}/statuses?per_page=100") or []
        return any(s.get("context") == self.rules.context and s.get("state") == "success"
                   and (s.get("creator") or {}).get("login") == f"{self.rules.app_name}[bot]" for s in statuses)

    def pending(self, fresh=False) -> dict:
        with self._lock:
            if not fresh and self._cache is not None and time.time() - self._cached_at < CACHE_SECONDS:
                return self._cache
        repos = (self.github.call("GET", "/installation/repositories?per_page=100") or {}).get("repositories", [])
        out = []
        for r in sorted(repos, key=lambda r: r["full_name"]):
            for pr in self.github.call("GET", f"/repos/{r['full_name']}/pulls?state=open&per_page=100") or []:
                sensitive = self.rules.sensitive(self._files(r["full_name"], pr["number"]))
                if not sensitive:
                    continue
                sha = pr["head"]["sha"]
                out.append({"repo": r["full_name"], "number": pr["number"], "title": pr["title"],
                            "url": pr["html_url"], "author": (pr.get("user") or {}).get("login"),
                            "draft": bool(pr.get("draft")), "sha": sha, "updatedAt": pr.get("updated_at"),
                            "sensitive": sensitive, "approved": self._approved(r["full_name"], sha)})
        result = {"pullRequests": out, "context": self.rules.context, "checkedAt": self.clock().isoformat()}
        with self._lock:
            self._cache, self._cached_at = result, time.time()
        return result

    # ---- approving ----
    def _event(self, email, subject, decision, reason, run_id=None):
        event = {"ts": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": f"owner:{email}",
                 "action": f"approve-pr:{subject}", "subject": f"pull request {subject}", "trigger": "site",
                 "reason": reason, "decision": decision, "component": "approvals"}
        if run_id:
            event["run_id"] = run_id
        try:
            self._record(event)
        except Exception as exc:
            self.log({"event": "decision-unrecorded", "action": event["action"], "error": type(exc).__name__})
        return event

    def handle(self, email: str, headers, body: bytes) -> Outcome:
        if headers.get("Sec-Fetch-Site") != "same-origin":
            return Outcome(403, "Approvals must come from this site's own pages.")
        if not (headers.get("Content-Type") or "").startswith("application/json"):
            return Outcome(415, "Send the approval as JSON.")
        try:
            data = json.loads(body or b"{}")
            repo, number, sha = str(data["repo"]), int(data["number"]), str(data["sha"])
            reason, code = " ".join(str(data.get("reason") or "").split())[:300], str(data.get("code") or "").strip()
        except (ValueError, KeyError, TypeError):
            return Outcome(400, "Send repo, number, sha and code.")
        if not reason:
            return Outcome(400, "Say why, for the decision log.")
        subject = f"{repo}#{number}@{sha[:7]}"
        try:
            pr = self.github.call("GET", f"/repos/{repo}/pulls/{number}")
        except Exception as exc:
            return Outcome(502, f"GitHub couldn't be asked about {repo}#{number} ({type(exc).__name__}), so nothing changed.")
        if not pr or pr.get("state") != "open":
            return Outcome(409, f"{repo}#{number} isn't open any more.")
        if pr["head"]["sha"] != sha:
            return Outcome(409, f"{repo}#{number} has new commits since you looked. Reload and review them first.")
        sensitive = self.rules.sensitive(self._files(repo, number))
        try:
            secret = self._read_secret()
            ok = bool(secret) and self.verifier.verify(secret, code)
        except Exception as exc:
            return Outcome(503, f"The code couldn't be checked ({type(exc).__name__}), so nothing changed.")
        if not ok:
            self.log({"event": "approval-code-refused", "subject": subject})
            self._event(email, subject, "rejected", "the code was wrong or already used")
            if self.verifier.locked():
                return Outcome(403, "Too many wrong codes. Wait five minutes, then try again.")
            return Outcome(403, "That code didn't work, or it was already used. Enter the code your app shows now.")
        description = (f"Approved by the owner with a fresh code ({len(sensitive)} sensitive files)"
                       if sensitive else "Approved by the owner with a fresh code")
        try:
            self.github.call("POST", f"/repos/{repo}/statuses/{sha}", {
                "state": "success", "context": self.rules.context, "description": description[:140],
                "target_url": self.admin_url})
        except Exception as exc:
            self._event(email, subject, "failed", f"{reason} (GitHub refused the status: {type(exc).__name__})")
            return Outcome(502, f"GitHub didn't take the approval ({type(exc).__name__}). Nothing was approved.")
        event = self._event(email, subject, "approved", reason, run_id=sha)
        with self._lock:
            self._cache = None
        return Outcome(200, f"Approved {repo}#{number} at {sha[:7]}. It can merge once its other checks pass.", event)
