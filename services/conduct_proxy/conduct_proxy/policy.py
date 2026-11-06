"""Conduct policy: decides whether one outbound request may leave.

Pure logic with no network access, so every rule can be tested directly.
Anything not positively allowed is denied, and a config that would weaken
Article 2 refuses to load rather than running with the weaker setting.

The profile comes from the target host, never from the caller:

  model      Vertex AI hosts. POST, credentials allowed.
  owner-key  Registry APIs needing an owner-entered credential. Denied outright
             until the owner has approved that source.
  registry   Open official sources. GET/HEAD, no credentials.
  crawler    Any other public host. GET/HEAD, no credentials, robots.txt.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

MODEL, OWNER_KEY, REGISTRY, CRAWLER = "model", "owner-key", "registry", "crawler"

ARTICLE_MAX_RPS = 1  # Article 2.4: at most one request per second per host.

CREDENTIAL_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token"}

# Only these request headers are forwarded. Cookies are absent by design.
FORWARD_HEADERS = {
    "accept", "accept-encoding", "accept-language", "if-modified-since",
    "if-none-match", "range", "content-type",
}

# A floor that config can add to but never remove.
BUILTIN_DENY_PATHS = (
    r"(?i)/(log-?in|log-?on|sign-?in|sign-?on|sign-?up|register|registration"
    r"|auth|oauth2?|sso|saml|password|passwd|forgot|session|my-?account)(/|$)",
)

CREDENTIAL_QUERY = re.compile(
    r"(?i)(^|&)(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|password|passwd|secret)=")


class ConfigError(Exception):
    """The policy config is missing or would weaken Article 2."""


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule: str
    reason: str
    profile: str = ""
    scheme: str = ""
    host: str = ""
    port: int = 0
    target: str = ""      # path and query, as sent upstream
    log_path: str = ""    # path only: queries can carry credentials
    headers: dict = field(default_factory=dict)


def _deny(rule: str, reason: str, **kw) -> Decision:
    return Decision(False, rule, reason, **kw)


def normalise_path(path: str) -> str:
    """Reduce a path to what deny rules should see.

    Undoes percent-encoding (twice, for double encoding), drops ``;params``
    and file extensions, and collapses slashes, so that /%6cogin, /login;a=1,
    //login and /login.php all read as /login.
    """
    p = unquote(unquote(path)).replace("\\", "/")
    segments = []
    for seg in p.split("/"):
        seg = seg.split(";", 1)[0]
        if "." in seg.strip("."):
            seg = seg.split(".", 1)[0]
        if seg:
            segments.append(seg)
    return "/" + "/".join(segments)


class Policy:
    def __init__(self, cfg: dict, approved_sources: frozenset = frozenset()):
        try:
            self.registry = {h.lower() for h in cfg["registry_hosts"]}
            self.model = {h.lower() for h in cfg["model_hosts"]}
            self.owner_key = {e["host"].lower(): e["source"] for e in cfg["owner_key_hosts"]}
            crawler = cfg["crawler_policy"]
            self.methods = {m.upper() for m in crawler["methods"]}
            self.max_rps = crawler["max_rps_per_host"]
            self.user_agent = crawler["user_agent"]
            deny = list(crawler.get("deny_paths_matching", []))
            honour, cookies, auth = (crawler["honour_robots_txt"], crawler["send_cookies"],
                                     crawler["send_auth_headers"])
        except (KeyError, TypeError, AttributeError) as exc:
            raise ConfigError(f"egress config incomplete: {exc!r}") from exc
        if not self.methods or not self.methods <= {"GET", "HEAD"}:
            raise ConfigError("crawler methods must be GET and/or HEAD (Article 2)")
        if not 0 < self.max_rps <= ARTICLE_MAX_RPS:
            raise ConfigError("max_rps_per_host must be above 0 and at most 1 (Article 2.4)")
        if honour is not True or cookies is not False or auth is not False:
            raise ConfigError("crawler must honour robots.txt and send no cookies or auth (Article 2)")
        self.approved = frozenset(approved_sources)
        self.min_interval = 1.0 / self.max_rps
        try:
            self.deny_paths = [re.compile(p) for p in (*BUILTIN_DENY_PATHS, *deny)]
        except re.error as exc:
            raise ConfigError(f"bad deny path pattern: {exc}") from exc

    @classmethod
    def from_file(cls, path, approved_sources=frozenset()) -> "Policy":
        try:
            cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read egress config: {exc}") from exc
        if not isinstance(cfg, dict):
            raise ConfigError("egress config is not a mapping")
        return cls(cfg, frozenset(approved_sources))

    def profile_for(self, host: str) -> str:
        if host in self.model:
            return MODEL
        if host in self.owner_key:
            return OWNER_KEY
        if host in self.registry:
            return REGISTRY
        return CRAWLER

    def rate_limited(self, profile: str) -> bool:
        # Vertex AI is our own paid API with its own quotas, not a third party's site.
        return profile != MODEL

    def evaluate(self, method: str, url: str, headers: dict) -> Decision:
        method = method.upper()
        if not url or any(ord(c) <= 0x20 or ord(c) >= 0x7F or c == "\\" for c in url):
            return _deny("bad-url", "URL has spaces, control or non-ASCII characters")
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port
        except ValueError:
            return _deny("bad-url", "URL cannot be parsed")
        if parts.scheme not in ("http", "https") or not host:
            return _deny("bad-url", "only http and https URLs with a host are allowed")
        if parts.username is not None or parts.password is not None:
            return _deny("url-credentials", "credentials in a URL are not allowed (Article 2.2)")
        host = host.lower().rstrip(".")
        if not host:
            return _deny("bad-url", "URL has no usable host")
        if _is_ip_literal(host):
            return _deny("ip-literal", "targets must be named hosts, not IP addresses (Article 2.3)")
        default_port = 443 if parts.scheme == "https" else 80
        if port not in (None, default_port):
            return _deny("bad-port", "only the standard web ports are allowed (Article 2.3)")

        profile = self.profile_for(host)
        log_path = parts.path or "/"
        ctx = dict(profile=profile, scheme=parts.scheme, host=host, port=default_port, log_path=log_path)

        if profile == OWNER_KEY and self.owner_key[host] not in self.approved:
            return _deny("source-not-approved",
                         f"source {self.owner_key[host]} has no owner-approved credential (Article 2.2)", **ctx)
        if profile != CRAWLER and parts.scheme != "https":
            return _deny("https-required", "official APIs are reached over https only", **ctx)

        allowed = {MODEL: {"POST"}, OWNER_KEY: {"GET", "HEAD", "POST"}}.get(profile, self.methods)
        if method not in allowed:
            return _deny("method-not-allowed", f"{method} is not allowed for {profile} traffic", **ctx)

        lowered = {k.lower(): v for k, v in headers.items()}
        if profile in (REGISTRY, CRAWLER):
            sent = sorted(CREDENTIAL_HEADERS & set(lowered))
            if sent:
                return _deny("credentials-not-allowed", f"{sent[0]} header sent to {profile} host (Article 2.2)", **ctx)
            if CREDENTIAL_QUERY.search(unquote(parts.query)):
                return _deny("credentials-not-allowed", f"credential in query to {profile} host (Article 2.2)", **ctx)
        if profile != MODEL:
            norm = normalise_path(parts.path)
            if any(p.search(norm) for p in self.deny_paths):
                return _deny("denied-path", "login, account and similar paths are off limits (Article 2.1)", **ctx)

        keep = FORWARD_HEADERS | (CREDENTIAL_HEADERS - {"proxy-authorization"} if profile in (MODEL, OWNER_KEY) else set())
        forward = {k: v for k, v in lowered.items() if k in keep}
        forward["user-agent"] = self.user_agent
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return Decision(True, "allowed", "within policy", target=target, headers=forward, **ctx)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # Numeric forms that resolvers accept as IPv4: 2130706433, 0x7f.1, 127.1
    return bool(re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}", host))
