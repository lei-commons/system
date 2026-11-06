"""Owner control gate (constitution Article 1).

Every job and agent calls ``require_running(component)`` before each step. The
state is set only by the owner, through the owner console, which writes:

  * the GitHub org variable ``SYSTEM_STATE`` (read by Actions workflows), and
  * ``state.json`` in the control bucket (read by Cloud Run jobs and services).

Agents have read-only access to both. If the state cannot be read, the gate
fails closed: the step does not run.

state.json shape::

    {
      "state": "running",            # running | paused | stopped | manual
      "paused_components": ["agent:resolver-in"],
      "monthly_budget_usd": 1500,
      "updated_by": "owner",
      "updated_at": "2026-11-06T09:00:00Z"
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

STATES = ("running", "paused", "stopped", "manual")


class SystemHalted(RuntimeError):
    """Raised when the owner has paused, stopped or taken manual control."""


@dataclass(frozen=True)
class ControlState:
    state: str
    paused_components: tuple[str, ...] = field(default_factory=tuple)
    monthly_budget_usd: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ControlState":
        state = str(d.get("state", "")).lower()
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        budget = d.get("monthly_budget_usd")
        return cls(
            state=state,
            paused_components=tuple(d.get("paused_components", ()) or ()),
            monthly_budget_usd=float(budget) if budget is not None else None,
        )


def _read_gcs(uri: str) -> dict:
    from google.cloud import storage  # imported lazily; not needed in CI

    bucket, _, blob = uri.removeprefix("gs://").partition("/")
    data = storage.Client().bucket(bucket).blob(blob).download_as_bytes()
    return json.loads(data)


def load_state(env: dict | None = None) -> ControlState:
    """Load the control state. Order: LC_CONTROL_STATE_URI (gs:// or file path), then SYSTEM_STATE."""
    env = os.environ if env is None else env
    uri = env.get("LC_CONTROL_STATE_URI", "")
    if uri:
        if uri.startswith("gs://"):
            return ControlState.from_dict(_read_gcs(uri))
        return ControlState.from_dict(json.loads(Path(uri).read_text(encoding="utf-8")))
    if "SYSTEM_STATE" in env:
        paused = [c for c in env.get("PAUSED_COMPONENTS", "").split(",") if c]
        budget = env.get("MONTHLY_BUDGET_USD")
        return ControlState.from_dict({
            "state": env["SYSTEM_STATE"],
            "paused_components": paused,
            "monthly_budget_usd": budget,
        })
    raise SystemHalted("control state not available; failing closed")


def require_running(component: str, env: dict | None = None, allow_manual: bool = False) -> ControlState:
    """Return the state if ``component`` may run; raise ``SystemHalted`` otherwise.

    In manual mode agents keep working and proposing, but nothing merges or deploys
    without the owner. Work that only proposes passes ``allow_manual=True``; anything
    that merges, deploys or publishes keeps the default and stops.
    """
    try:
        st = load_state(env)
    except SystemHalted:
        raise
    except Exception as exc:  # unreadable or malformed state: fail closed
        raise SystemHalted(f"control state unreadable ({exc}); failing closed") from exc
    if st.state != "running" and not (allow_manual and st.state == "manual"):
        raise SystemHalted(f"system is {st.state}; {component} will not run")
    if component in st.paused_components or _prefix_paused(component, st.paused_components):
        raise SystemHalted(f"{component} is paused by the owner")
    return st


def _prefix_paused(component: str, paused: tuple[str, ...]) -> bool:
    # "agent:*" pauses every agent; "agent:resolver-*" pauses all resolvers.
    return any(p.endswith("*") and component.startswith(p[:-1]) for p in paused)
