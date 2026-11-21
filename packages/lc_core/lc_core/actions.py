"""The owner's actions: what the site's pages offer, inline, and what the site and the owner console perform.

One catalogue, so the pages, the site and the console can't disagree (lc_core.owner carries them out). Jobs come from the
sources registry, so a new source gets its actions with no other change.

The state rules: pause and stop always work, even on a missing or corrupt state; everything else refuses to
guess. ``set-budget:<amount>`` records the owner's monthly cap in USD in the control state (it lived in the
control repository and a GitHub variable until 2026-11-21; control parameters now live only in the cloud).
"""

from __future__ import annotations

import re

STATES = ("running", "paused", "stopped", "manual")
AFTER_INGEST = ["build-snapshot", "load-db"]
# The component name each job checks before working (lc_core.control.require_running).
GATES = {"build-snapshot": "job:publish", "load-db": "job:load-db"}


# Data source approvals: the action names and the status each sets (see lc_core.approvals).
SOURCE_ACTIONS = {"source-approve": "approved", "source-deny": "denied", "source-pending": "pending"}
SOURCE_LABELS = {"source-approve": "Approve", "source-deny": "Deny", "source-pending": "Keep pending"}
# Changes to the registry itself (lc_core.source_edits): add, edit or remove a source, or leave Claude a suggestion.
EDIT_ACTIONS = ("source-add", "source-edit", "source-remove", "source-hint")
# Tools: one-off runs with their command overridden (they replaced the gateway-probe and load-test workflows when
# GitHub lost its manual triggers, 2026-11-21). probe:<https URL> asks a source for its first kilobyte through the
# gateway (allow-list, pacing, fixed address; ingest_gleif.probe); load-test:<records>:<seconds> builds a synthetic
# copy of the database in its own schema, times the site's queries and drops it (load_db.loadtest).
TOOLS = ("probe", "load-test")
PROBE_URL = re.compile(r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~%/?=&+-]*)?$")
PROBE_MAX = 2000
LOAD_TEST_RECORDS = (1_000, 200_000_000)
LOAD_TEST_SECONDS = (10, 3_600)
MAX_BUDGET_USD = 1_000_000  # a typo guard, not a policy: the billing budget itself is set in Terraform


def probe_url(text: str) -> str:
    """The URL in ``probe:<url>``: a plain https address. Raises Refused. (The gateway still refuses any host
    that isn't allow-listed.)"""
    if not isinstance(text, str) or len(text) > PROBE_MAX or not PROBE_URL.match(text):
        raise Refused("the probe needs a plain https URL")
    return text


def load_test_settings(text: str) -> tuple[int, int]:
    """(records, seconds) in ``load-test:<records>:<seconds>``. Raises Refused."""
    records, _, seconds = str(text).partition(":")
    if not (records.isdigit() and seconds.isdigit()):
        raise Refused("the load test needs whole numbers: records and seconds")
    r, s = int(records), int(seconds)
    if not LOAD_TEST_RECORDS[0] <= r <= LOAD_TEST_RECORDS[1] or not LOAD_TEST_SECONDS[0] <= s <= LOAD_TEST_SECONDS[1]:
        raise Refused(f"records must be {LOAD_TEST_RECORDS[0]:,} to {LOAD_TEST_RECORDS[1]:,} and seconds "
                      f"{LOAD_TEST_SECONDS[0]} to {LOAD_TEST_SECONDS[1]:,}")
    return r, s


def budget_amount(text: str) -> float:
    """The amount in ``set-budget:<amount>``. Raises Refused unless it is a number above zero."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise Refused(f"the budget must be a number, not {text!r}") from None
    if not (value == value) or value <= 0 or value > MAX_BUDGET_USD:  # value == value is False for NaN
        raise Refused(f"the budget must be above zero and at most {MAX_BUDGET_USD:,}")
    return value


class Refused(Exception):
    """The action can't be applied. Nothing is written."""


def jobs(sources: list) -> list:
    return [s["job"] for s in sources] + AFTER_INGEST


def gate(job: str) -> str:
    return GATES.get(job, f"job:{job}")


def catalogue(sources: list, state: dict | None) -> list:
    """The actions that apply now, as dicts: id, group, label, tone, help."""
    current = (state or {}).get("state")
    paused = set((state or {}).get("paused_components") or ())
    out = []

    def add(id_, group, label, tone="", help_=""):
        out.append({"id": id_, "group": group, "label": label, "tone": tone, "help": help_})

    if current != "running" and current != "stopped":
        add("resume", "System", "Resume", "primary", "Switch the system to running.")
    if current == "stopped":
        add("restart", "System", "Restart", "primary", "Move a stopped system to paused, to look before resuming.")
    if current not in ("paused", "stopped"):
        add("pause", "System", "Pause", "", "Every job stops at its next check; nothing is lost.")
    if current not in ("manual", "stopped"):
        add("manual", "System", "Manual mode", "", "Agents keep working; nothing is published without you.")
    if current != "stopped":
        add("stop", "System", "Stop", "danger", "Stop everything. Restart, then resume, to come back.")
    labels = {s["job"]: s.get("label", s["job"]) for s in sources}
    labels.update({"build-snapshot": "Snapshot", "load-db": "Database load"})
    for job in jobs(sources):
        add(f"run:{job}", "Run now", labels[job], "", f"Start {job} now, outside the daily cycle. Safe to repeat.")
    for job in jobs(sources):
        component = gate(job)
        if component in paused:
            add(f"resume-component:{component}", "Paused jobs", f"Resume {labels[job]}", "primary", f"Let {job} run again.")
        else:
            add(f"pause-component:{component}", "Pause one job", labels[job], "", f"{job} stops at its next check.")
    return out


def source_catalogue(registry: list, statuses: dict) -> list:
    """Approve, deny or keep pending, for every source in the registry: the choices other than its status now."""
    out = []
    for source in registry:
        current = (statuses.get(source["id"]) or {}).get("status", "pending")
        for kind, status in SOURCE_ACTIONS.items():
            if status != current:
                out.append({"id": f"{kind}:{source['id']}", "group": "Data sources",
                            "label": f"{SOURCE_LABELS[kind]} {source.get('name') or source['id']}",
                            "tone": "primary" if status == "approved" else ("danger" if status == "denied" else ""),
                            "help": {"approved": "Its jobs may fetch from it, and its records reach the snapshot, database and site.",
                                     "denied": "Its jobs stop before fetching, and its records leave the next snapshot, database and site.",
                                     "pending": "Not used until you decide: its jobs stop and its records leave the next snapshot."}[status]})
    return out


def known(sources: list, action: str, registry: list = ()) -> bool:
    """Whether ``action`` is one this system could ever offer (whatever the state now)."""
    if action in ("pause", "stop", "resume", "restart", "manual"):
        return True
    kind, _, name = action.partition(":")
    if action == "source-add":
        return True
    if kind in SOURCE_ACTIONS or (kind in EDIT_ACTIONS and kind != "source-add"):
        return name in {s["id"] for s in registry}
    if kind == "run":
        return name in jobs(sources)
    if kind in ("pause-component", "resume-component"):
        return name in {gate(j) for j in jobs(sources)}
    checks = {"set-budget": budget_amount, "probe": probe_url, "load-test": load_test_settings}
    if kind in checks:
        try:
            checks[kind](name)
        except Refused:
            return False
        return True
    return False


def apply(action: str, current: dict | None, actor: str, now: str) -> dict:
    """The new control state for a state action. Raises Refused."""
    safety = {"pause": "paused", "stop": "stopped"}
    if current is not None and current.get("state") not in STATES:
        current = None
    if current is None and action not in safety:
        raise Refused("the current state is missing or unreadable; pause or stop first")
    new = dict(current or {"paused_components": []})
    state = new.get("state", "")
    kind, _, component = action.partition(":")
    if action in safety:
        new["state"] = safety[action]
    elif action == "resume":
        if state == "stopped":
            raise Refused("the system is stopped; restart first")
        new["state"] = "running"
    elif action == "restart":
        if state != "stopped":
            raise Refused(f"restart only applies to a stopped system, not {state}")
        new["state"] = "paused"
    elif action == "manual":
        if state == "stopped":
            raise Refused("the system is stopped; restart first")
        new["state"] = "manual"
    elif kind in ("pause-component", "resume-component") and component:
        paused = [c for c in new.get("paused_components", []) if c != component]
        if kind == "pause-component":
            paused.append(component)
        new["paused_components"] = paused
    elif kind == "set-budget":
        new["monthly_budget_usd"] = budget_amount(component)
    else:
        raise Refused(f"{action} doesn't change the control state")
    new["updated_by"] = actor
    new["updated_at"] = now
    return new
