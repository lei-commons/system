"""The admin area's Approvals page (lc_site.approvals): sensitive pull requests and the owner's approval."""

import io
import json
from datetime import datetime, timezone

import pytest

from lc_core.approval_rules import load
from lc_core.totp import MemorySteps, Verifier, code_at
from lc_site.approvals import Approvals, GitHubApp

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
RULES = load()


class Clock:
    t = 1_800_000_000.0

    def __call__(self):
        return self.t


class FakeGitHub:
    """Two repositories; system has a sensitive PR (#5) and an ordinary one (#6)."""

    def __init__(self):
        self.heads = {("lei-commons/system", 5): "aaa1111", ("lei-commons/system", 6): "bbb2222"}
        self.states = {("lei-commons/system", 5): "open", ("lei-commons/system", 6): "open"}
        self.files = {5: ["infra/terraform/site_lb.tf", "docs/admin.md"], 6: ["docs/admin.md"]}
        self.statuses, self.posted, self.fail_post = {}, [], False

    def call(self, method, path, body=None):
        if path.startswith("/installation/repositories"):
            return {"repositories": [{"full_name": "lei-commons/system"}, {"full_name": "lei-commons/releases"}]}
        if path.startswith("/repos/lei-commons/releases/pulls"):
            return []
        if path.startswith("/repos/lei-commons/system/pulls?"):
            return [{"number": n, "title": f"PR {n}", "html_url": f"https://github.test/{n}", "user": {"login": "o"},
                     "head": {"sha": self.heads[("lei-commons/system", n)]}} for n in (5, 6)]
        if "/files" in path:
            n = int(path.split("/pulls/")[1].split("/")[0])
            return [{"filename": f} for f in self.files[n]] if "page=1" in path else []
        if "/commits/" in path and path.endswith("per_page=100"):
            sha = path.split("/commits/")[1].split("/")[0]
            return self.statuses.get(sha, [])
        if method == "GET" and "/pulls/" in path:
            n = int(path.rsplit("/", 1)[1])
            return {"number": n, "state": self.states[("lei-commons/system", n)],
                    "head": {"sha": self.heads[("lei-commons/system", n)]}}
        if method == "POST" and "/statuses/" in path:
            if self.fail_post:
                raise OSError("github down")
            sha = path.rsplit("/", 1)[1]
            self.posted.append((sha, body))
            self.statuses.setdefault(sha, []).append({**body, "creator": {"login": "lei-commons-guardian[bot]"}})
            return {}
        raise AssertionError(path)


@pytest.fixture
def page():
    clock, gh, events = Clock(), FakeGitHub(), []
    approvals = Approvals(github=gh, rules=RULES, verifier=Verifier(MemorySteps(), clock=clock), read_secret=lambda: SECRET,
                          record=events.append, admin_url="https://site.test/admin/#/approvals",
                          clock=lambda: datetime.fromtimestamp(clock.t, timezone.utc), log=lambda r: None)

    def post(body, headers=SAME_ORIGIN):
        return approvals.handle("o@x.test", headers, json.dumps(body).encode())

    def code():
        clock.t += 30
        return code_at(SECRET, int(clock.t) // 30)

    return approvals, gh, events, post, code


def test_only_sensitive_pull_requests_are_listed(page):
    approvals, gh, events, post, code = page
    listed = approvals.pending(fresh=True)["pullRequests"]
    assert [(p["repo"], p["number"], p["sensitive"], p["approved"]) for p in listed] == [
        ("lei-commons/system", 5, ["infra/terraform/site_lb.tf"], False)]


def test_a_promotion_the_workflow_passed_is_not_listed(page):
    approvals, gh, events, post, code = page
    gh.files[5] = ["config/sources.yaml", "infra/workflows/daily-cycle.yaml"]
    gh.statuses["aaa1111"] = [{"context": "owner-approval", "state": "success", "creator": {"login": "github-actions[bot]"},
                               "description": "Promotes trial sources only (tw-mof-tax): no approval needed"}]
    assert approvals.pending(fresh=True)["pullRequests"] == []
    gh.statuses["aaa1111"][0]["description"] = "No sensitive paths: no approval needed"  # not a promotion: listed
    assert [p["number"] for p in approvals.pending(fresh=True)["pullRequests"]] == [5]


def test_approving_sets_the_apps_status_on_that_commit(page):
    approvals, gh, events, post, code = page
    out = post({"repo": "lei-commons/system", "number": 5, "sha": "aaa1111", "reason": "reviewed the LB change",
                "code": code()})
    assert out.status == 200 and "aaa1111" in out.message
    assert gh.posted == [("aaa1111", {"state": "success", "context": "owner-approval",
                                      "description": "Approved by the owner with a fresh code (1 sensitive files)",
                                      "target_url": "https://site.test/admin/#/approvals"})]
    assert events[-1]["decision"] == "approved" and events[-1]["subject"] == "pull request lei-commons/system#5@aaa1111"
    assert approvals.pending()["pullRequests"][0]["approved"] is True  # the cache was cleared


def test_a_new_push_or_a_closed_pr_is_refused(page):
    approvals, gh, events, post, code = page
    out = post({"repo": "lei-commons/system", "number": 5, "sha": "0ld0000", "reason": "x", "code": code()})
    assert out.status == 409 and "new commits" in out.message and gh.posted == []
    gh.states[("lei-commons/system", 5)] = "closed"
    assert post({"repo": "lei-commons/system", "number": 5, "sha": "aaa1111", "reason": "x", "code": code()}).status == 409


def test_codes_reasons_and_origin_are_checked(page):
    approvals, gh, events, post, code = page
    pr = {"repo": "lei-commons/system", "number": 5, "sha": "aaa1111", "reason": "ok"}
    assert post({**pr, "code": "000000"}).status == 403 and events[-1]["decision"] == "rejected"
    assert "000000" not in str(events)
    assert post({**pr, "reason": " ", "code": code()}).status == 400
    assert post({**pr, "code": code()}, headers={"Content-Type": "application/json"}).status == 403
    assert post({"repo": "x"}).status == 400
    assert gh.posted == []


def test_github_refusing_the_status_approves_nothing(page):
    approvals, gh, events, post, code = page
    gh.fail_post = True
    out = post({"repo": "lei-commons/system", "number": 5, "sha": "aaa1111", "reason": "x", "code": code()})
    assert out.status == 502 and events[-1]["decision"] == "failed"


def test_the_app_signs_in_with_a_jwt_and_reuses_its_token():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(request):
        seen.append((request.get_method(), request.full_url, request.headers["Authorization"]))
        if request.full_url.endswith("/access_tokens"):
            return Response(json.dumps({"token": "ghs_x", "expires_at": "2027-01-15T09:00:00Z"}).encode())
        return Response(b'{"ok": true}')

    clock = Clock()
    app = GitHubApp(5655569, 179552998, lambda: pem, clock=clock, opener=opener)
    assert app.call("GET", "/installation/repositories") == {"ok": True}
    assert app.call("GET", "/installation/repositories") == {"ok": True}
    token_calls = [s for s in seen if s[1].endswith("/access_tokens")]
    assert len(token_calls) == 1 and token_calls[0][1] == "https://api.github.com/app/installations/179552998/access_tokens"
    # The signature holds against the App's public key (the test clock is in the future, hence the skew).
    claims = jwt.decode(token_calls[0][2].split(" ", 1)[1], certs=public, clock_skew_in_seconds=10**10)
    assert claims["iss"] == "5655569" and claims["exp"] - claims["iat"] <= 600
    assert [s[2] for s in seen if not s[1].endswith("/access_tokens")] == ["Bearer ghs_x", "Bearer ghs_x"]


def test_a_pull_request_whose_files_github_cant_list_is_listed_for_the_owner(page):
    import urllib.error

    approvals, gh, events, post, code = page
    plain = gh.call

    def call(method, path, body=None):
        if "/pulls/6/files" in path:
            raise urllib.error.HTTPError(path, 422, "Unprocessable Entity", {}, None)  # a diff too large
        return plain(method, path, body)

    gh.call = call
    listed = approvals.pending(fresh=True)["pullRequests"]
    assert [(p["number"], p["sensitive"]) for p in listed] == [
        (5, ["infra/terraform/site_lb.tf"]), (6, ["(GitHub can't list this pull request's files: its diff is too large)"])]
