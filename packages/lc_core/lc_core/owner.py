"""Carrying out the owner's actions: shared by the site (inline on its pages) and the owner console.

Each action needs, at the moment it is taken: the owner's IAP login (checked by
the caller), a reason, and a fresh authenticator code (lc_core.totp; a code
works once, system-wide). Then it writes the control state or the source
approvals, only if they haven't changed since they were read, or starts a job.
Every attempt that reaches a decision is recorded in the decision log under
``owner-control/``.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field

from .actions import SOURCE_ACTIONS, Refused, apply, catalogue, known, source_catalogue
from .approvals import decide, effective

MAX_REASON = 300


class Conflict(Exception):
    """What the action changes was changed meanwhile; try again."""


@dataclass
class Outcome:
    status: int  # an HTTP status: 200, 400, 403, 404, 409, 502
    message: str
    event: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200


class GoogleBackend:
    """The control state, the source approvals, the cycle's jobs and the decision log, in GCP."""

    def __init__(self, project: str, region: str):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import storage

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self._http = AuthorizedSession(credentials)
        self._storage = storage.Client(project=project)
        self.project, self.region = project, region

    def _control(self, name):
        return self._storage.bucket(f"{self.project}-control").blob(name)

    def _read(self, name, missing):
        from google.api_core.exceptions import NotFound

        blob = self._control(name)
        try:
            blob.reload()
        except NotFound:
            return missing, 0
        try:
            return json.loads(blob.download_as_bytes(if_generation_match=blob.generation)), blob.generation
        except ValueError:
            return None, blob.generation

    def _write(self, name, data, generation):
        from google.api_core.exceptions import PreconditionFailed

        try:
            self._control(name).upload_from_string(json.dumps(data, indent=2, sort_keys=True) + "\n",
                                                   content_type="application/json", if_generation_match=generation)
        except PreconditionFailed as exc:
            raise Conflict("it changed meanwhile; try again") from exc

    def read_state(self):
        return self._read("state.json", None)

    def write_state(self, state, generation):
        self._write("state.json", state, generation)

    def read_approvals(self):
        data, generation = self._read("source-approvals.json", {})
        return (data or {}), generation

    def write_approvals(self, decisions, generation):
        self._write("source-approvals.json", decisions, generation)

    def run_job(self, job: str) -> str:
        url = f"https://run.googleapis.com/v2/projects/{self.project}/locations/{self.region}/jobs/{job}:run"
        response = self._http.post(url, json={}, timeout=30)
        if response.status_code == 403:
            raise PermissionError(f"not allowed to start {job}")
        response.raise_for_status()
        return ((response.json().get("metadata") or {}).get("name") or "").rsplit("/", 1)[-1]

    def record(self, event: dict) -> None:
        name = f"owner-control/{event['ts']}-{secrets.token_hex(3)}.json"
        self._storage.bucket(f"{self.project}-decision-log").blob(name).upload_from_string(
            json.dumps(event, indent=2, sort_keys=True), content_type="application/json", if_generation_match=0)


def offered(backend, sources: list, registry: list, action: str):
    """The action as the catalogue describes it now, or None if it doesn't apply now."""
    if action.partition(":")[0] in SOURCE_ACTIONS:
        decisions, _ = backend.read_approvals()
        choices = source_catalogue(registry, effective(registry, decisions))
    else:
        state, _ = backend.read_state()
        choices = catalogue(sources, state)
    return next((a for a in choices if a["id"] == action), None)


def perform(*, backend, verifier, secret: str, sources: list, registry: list, action: str, reason: str, code: str,
            email: str, now: str, trigger: str, log=lambda record: None) -> Outcome:
    """Check and carry out one owner action. ``now`` is an ISO time, ``trigger`` says where it came from."""
    reason = " ".join((reason or "").split())[:MAX_REASON]
    if not known(sources, action, registry):
        return Outcome(404, "That isn't an action this system offers.")
    if not reason:
        return Outcome(400, "Say why, for the decision log.")
    if not secret:
        return Outcome(403, "The second factor isn't set up yet: enrol in the owner console first.")
    try:
        verified = verifier.verify(secret, (code or "").strip())
    except Exception as exc:  # e.g. the shared record of used codes can't be written: fail closed, and say why
        log({"event": "action-code-unchecked", "action": action, "error": type(exc).__name__})
        return Outcome(503, f"The code couldn't be checked ({type(exc).__name__}: {str(exc).splitlines()[0][:120]}), "
                            "so nothing was done.")
    if not verified:
        log({"event": "action-code-refused", "action": action})
        if verifier.locked():
            return Outcome(403, "Too many wrong codes. Wait five minutes, then try again.")
        return Outcome(403, "That code didn't work, or it was already used. Enter the code your app shows now.")
    actor = f"owner:{email}"
    kind, _, name = action.partition(":")
    event = {"ts": now, "actor": actor, "action": action, "subject": "control state", "trigger": trigger,
             "reason": reason, "decision": "done", "component": name}
    try:
        if kind in SOURCE_ACTIONS:
            decisions, generation = backend.read_approvals()
            status = SOURCE_ACTIONS[kind]
            backend.write_approvals(decide(decisions, name, status, actor, now, reason), generation)
            event.update(subject=f"source {name}")
            title = next((r.get("name") for r in registry if r["id"] == name), None) or name
            message = f"{title} is now {status}."
        elif kind == "run":
            execution = backend.run_job(name)
            event.update(subject=name, run_id=execution)
            message = f"Started {name}" + (f" ({execution})" if execution else "") + "."
        else:
            current, generation = backend.read_state()
            new = apply(action, current, actor, now)
            backend.write_state(new, generation)
            paused = ", ".join(new.get("paused_components") or []) or "none"
            message = f"The system is {new['state']}; paused jobs: {paused}."
    except Refused as exc:
        event.update(decision="rejected", reason=f"{reason} (refused: {exc})")
        _record(backend, event, log)
        return Outcome(409, str(exc), event)
    except Conflict as exc:
        event.update(decision="failed", reason=f"{reason} (conflict)")
        _record(backend, event, log)
        return Outcome(409, f"Nothing changed: {exc}", event)
    except Exception as exc:
        event.update(decision="failed", reason=f"{reason} ({type(exc).__name__})")
        _record(backend, event, log)
        log({"event": "action-failed", "action": action, "error": type(exc).__name__})
        return Outcome(502, f"It didn't work: {type(exc).__name__}: {str(exc)[:200]}", event)
    _record(backend, event, log)
    return Outcome(200, message, event)


def _record(backend, event, log):
    try:
        backend.record(event)
    except Exception as exc:  # the action stands; failing to log it is itself logged
        log({"event": "decision-unrecorded", "action": event["action"], "error": type(exc).__name__})
