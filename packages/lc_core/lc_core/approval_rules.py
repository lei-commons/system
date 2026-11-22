"""Which changes need the owner's approval on the admin page (config/approvals.yaml).

A pull request that changes a sensitive path gets the commit status ``context`` set to pending until the owner
approves it, with a fresh authenticator code, for its exact head commit (owner decision 2026-11-21). The
owner-approval workflow and the site's Approvals page both decide with this module, from main's copy of the
config, so a pull request can't change the rules it is judged by.

One exception (owner decision 2026-11-22): promoting sources that passed their trial needs no approval each
time. ``promotion_only`` recognises such a pull request strictly: it changes only the sources registry, the
rendered cycle and tests or docs; the registry is unchanged except for pipelines whose stage goes from trial to
production; and the cycle is unchanged except for the promoted jobs moving out of the trials step. Anything
else in the same pull request, or any other change to those files, needs the owner's approval as before.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

REGISTRY = "config/sources.yaml"
CYCLE = "infra/workflows/daily-cycle.yaml"
PROMOTION_FILES = (REGISTRY, CYCLE)
PROMOTION_EXTRAS = (re.compile(r"^(?:.+/)?tests/[^/].*$"), re.compile(r"^docs/[^/].*\.md$"))
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


def _pipelines(registry) -> dict:
    """{source id: its pipeline} for every built source of a parsed registry."""
    sources = (registry or {}).get("sources") if isinstance(registry, dict) else None
    if not isinstance(sources, list):
        raise ValueError("the registry has no sources list")
    return {s["id"]: s["pipeline"] for s in sources if isinstance(s, dict) and isinstance(s.get("pipeline"), dict)}


def _without(value, jobs: set):
    """The cycle with the given job names taken out of every list, and a trials step left empty dropped."""
    if isinstance(value, dict):
        return {k: _without(v, jobs) for k, v in value.items()}
    if isinstance(value, list):
        out = [_without(v, jobs) for v in value if not (isinstance(v, str) and v in jobs)]
        return [v for v in out if not (isinstance(v, dict) and list(v) == ["trials"]
                                       and not ((v["trials"] or {}).get("for") or {}).get("in"))]
    return value


def promotion_only(changed, base: dict, head: dict) -> list | None:
    """The sources a pull request promotes from trial to production, if that's all it does; otherwise None.

    ``changed`` is the pull request's changed paths; ``base`` and ``head`` map REGISTRY and CYCLE to their text at
    the merge base and at the head (None for a missing file)."""
    changed = set(changed)
    if REGISTRY not in changed:
        return None
    if any(p not in PROMOTION_FILES and not any(r.match(p) for r in PROMOTION_EXTRAS) for p in changed):
        return None
    try:
        before, after = (_pipelines(yaml.safe_load(t[REGISTRY] or "")) for t in (base, head))
        old_registry, new_registry = yaml.safe_load(base[REGISTRY]), yaml.safe_load(head[REGISTRY])
    except (ValueError, KeyError, TypeError, yaml.YAMLError):
        return None
    promoted = sorted(i for i in after if i in before and before[i].get("stage") == "trial"
                      and after[i].get("stage") == "production")
    if not promoted:
        return None
    for source in new_registry["sources"]:  # put the stages back: the rest must be exactly as before
        if isinstance(source, dict) and source.get("id") in promoted:
            source["pipeline"] = dict(source["pipeline"], stage="trial")
    if new_registry != old_registry:
        return None
    if CYCLE in changed:
        jobs = {after[i].get("job") for i in promoted}
        try:
            old_cycle, new_cycle = yaml.safe_load(base[CYCLE] or ""), yaml.safe_load(head[CYCLE] or "")
        except (KeyError, yaml.YAMLError):
            return None
        trials = [v for v in _steps(new_cycle) if isinstance(v, dict) and "trials" in v]
        if any(job in (((t["trials"] or {}).get("for") or {}).get("in") or []) for t in trials for job in jobs):
            return None  # a promoted job still on trial in the new cycle
        if not new_cycle or _without(old_cycle, jobs) != _without(new_cycle, jobs):
            return None
    return promoted


def _steps(cycle) -> list:
    try:
        return cycle["main"]["steps"]
    except (KeyError, TypeError):
        return []
