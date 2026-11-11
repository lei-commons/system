"""The owner's approval of each data source: approved, denied or pending.

The registry (config/sources.yaml) gives each source a starting status: the
sources in use on 2026-11-11 were approved by the owner then; every other or
new source starts pending. The owner's later decisions, made in the owner
console with a fresh second-factor code, are kept next to the control state
as ``source-approvals.json``. A decision there overrides the registry, and the
newest decision for a source wins, so the owner can toggle at any time.

Everything that touches a source's data checks this first:
- ingestion jobs before they fetch anything (``require_approved``);
- the snapshot, which includes only approved sources, so denying a source
  also removes its records from the next snapshot, database and site.

Fail closed: an unknown source is not approved, and an unreadable decisions
file stops the work (a missing file just means no decisions yet).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from .control import SystemHalted

STATUSES = ("approved", "denied", "pending")
APPROVALS_NAME = "source-approvals.json"
MAX_HISTORY = 50


class SourceNotApproved(SystemHalted):
    """The source isn't approved, so its job stops before touching it."""


def registry(path=None) -> list:
    """Every source in the registry, built or not, in file order."""
    path = path or os.environ.get("LC_SOURCES_CONFIG") or _default_path()
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return list(data.get("sources", []))


def _default_path():
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "sources.yaml"
        if candidate.exists():
            return candidate
    return here.parent / "sources.yaml"


def approvals_uri(env=None) -> str:
    env = os.environ if env is None else env
    explicit = env.get("LC_SOURCE_APPROVALS_URI")
    if explicit:
        return explicit
    state = env.get("LC_CONTROL_STATE_URI", "")
    if not state:
        return ""
    return state.rsplit("/", 1)[0] + "/" + APPROVALS_NAME


def load_decisions(uri: str = None) -> dict:
    """The owner's decisions: {source id: {status, by, at, reason, history}}. {} if none yet."""
    uri = approvals_uri() if uri is None else uri
    if not uri:
        return {}
    try:
        if uri.startswith("gs://"):
            from google.api_core.exceptions import NotFound
            from google.cloud import storage

            bucket, _, name = uri.removeprefix("gs://").partition("/")
            try:
                data = storage.Client().bucket(bucket).blob(name).download_as_bytes()
            except NotFound:
                return {}
        else:
            path = Path(uri)
            if not path.exists():
                return {}
            data = path.read_bytes()
        decisions = json.loads(data)
    except Exception as exc:  # unreadable: nobody may assume approval
        raise SystemHalted(f"source approvals unreadable ({type(exc).__name__}); failing closed") from exc
    if not isinstance(decisions, dict):
        raise SystemHalted("source approvals malformed; failing closed")
    return decisions


def effective(sources: list, decisions: dict) -> dict:
    """{source id: {status, by, at, reason, decided}} where ``decided`` says whether the owner decided it later."""
    out = {}
    for source in sources:
        sid = source["id"]
        decided = decisions.get(sid)
        if decided and decided.get("status") in STATUSES:
            out[sid] = {k: decided.get(k) for k in ("status", "by", "at", "reason")} | {"decided": True}
        else:
            start = source.get("approval") if source.get("approval") in STATUSES else "pending"
            out[sid] = {"status": start, "by": source.get("approved_by"), "at": source.get("approved_at"),
                        "reason": source.get("approval_note"), "decided": False}
    return out


def status_of(source_id: str, sources: list = None, decisions: dict = None) -> str:
    sources = registry() if sources is None else sources
    decisions = load_decisions() if decisions is None else decisions
    return effective(sources, decisions).get(source_id, {"status": "pending"})["status"]


def require_approved(source_id: str, sources: list = None, decisions: dict = None) -> None:
    """Raise SourceNotApproved unless the owner has approved ``source_id``."""
    status = status_of(source_id, sources, decisions)
    if status != "approved":
        raise SourceNotApproved(f"source {source_id} is {status}, not approved by the owner")


def approved_record_sources(sources: list = None, decisions: dict = None) -> set:
    """The ids records carry (pipeline.records_as) for every approved, built source."""
    sources = registry() if sources is None else sources
    eff = effective(sources, load_decisions() if decisions is None else decisions)
    return {s["pipeline"]["records_as"] for s in sources
            if s.get("pipeline") and eff[s["id"]]["status"] == "approved"}


def decide(decisions: dict, source_id: str, status: str, actor: str, now: str, reason: str) -> dict:
    """The decisions after the owner sets ``source_id`` to ``status``; keeps a short history."""
    if status not in STATUSES:
        raise ValueError(f"not a status: {status}")
    new = json.loads(json.dumps(decisions))  # a copy
    previous = new.get(source_id) or {}
    history = (previous.get("history") or [])[-(MAX_HISTORY - 1):]
    if previous.get("status"):
        history.append({k: previous.get(k) for k in ("status", "by", "at", "reason")})
    new[source_id] = {"status": status, "by": actor, "at": now, "reason": reason, "history": history}
    return new
