"""Which changes need the owner's approval on the admin page (config/approvals.yaml).

A pull request that changes a sensitive path gets the commit status ``context`` set to pending until the owner
approves it, with a fresh authenticator code, for its exact head commit (owner decision 2026-11-21). The
owner-approval workflow and the site's Approvals page both decide with this module, from main's copy of the
config, so a pull request can't change the rules it is judged by.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "approvals.yaml"


def _regex(pattern: str) -> re.Pattern:
    """A path pattern as a regex: ``**`` crosses directories, ``*`` and ``?`` stay within one.

    >>> bool(_regex(".github/**").match(".github/workflows/ci.yml"))
    True
    >>> bool(_regex("infra/*.tf").match("infra/terraform/site.tf"))
    False
    >>> bool(_regex("constitution.md").match("docs/constitution.md"))
    False
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


@dataclass(frozen=True)
class Rules:
    context: str
    owner_login: str
    app_name: str
    app_id: int
    installation_id: int
    key_secret: str
    patterns: tuple

    def sensitive(self, paths) -> list:
        """The paths, of those given, that need the owner's approval (sorted)."""
        regexes = [_regex(p) for p in self.patterns]
        return sorted({p for p in paths if any(r.match(p) for r in regexes)})


def load(path=None) -> Rules:
    """Read and check config/approvals.yaml (LC_APPROVALS_CONFIG overrides the path). Raises ValueError."""
    path = Path(path or os.environ.get("LC_APPROVALS_CONFIG") or DEFAULT_PATH)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    app = data.get("github_app") or {}
    patterns = data.get("sensitive_paths") or []
    if not (isinstance(data.get("context"), str) and data["context"]):
        raise ValueError("approvals.yaml: context must be a name")
    if not (isinstance(patterns, list) and patterns and all(isinstance(p, str) and p and not p.startswith("/")
                                                             for p in patterns)):
        raise ValueError("approvals.yaml: sensitive_paths must list paths from the repository root")
    for key in ("app_id", "installation_id"):
        if not isinstance(app.get(key), int) or app[key] <= 0:
            raise ValueError(f"approvals.yaml: github_app.{key} must be a positive number")
    if not (isinstance(app.get("key_secret"), str) and app["key_secret"]):
        raise ValueError("approvals.yaml: github_app.key_secret must name the secret")
    if not (isinstance(data.get("owner_login"), str) and data["owner_login"]):
        raise ValueError("approvals.yaml: owner_login must be the owner's GitHub login")
    if not (isinstance(app.get("name"), str) and app["name"]):
        raise ValueError("approvals.yaml: github_app.name must be the App's name")
    return Rules(data["context"], data["owner_login"], app["name"], app["app_id"], app["installation_id"],
                 app["key_secret"], tuple(patterns))
