"""Whether a source on trial is still worth trying (owner decision 2026-11-22; docs/source-trials.md).

"After few trials/tries across few days (say 3 days), if it is established that a particular data source is
invalid, then it must be marked so and not to be processed anymore. ... Allow admin to explicitly re-approve such
invalid ones. If so, it should be tried again over few days (say 3 days) and then if it is still erroring out,
mark again as "Invalid" with date and reason against that data source."

A trial's job keeps ``validity.json`` beside its reports (``trials/<source>/``):

- every failed run adds its UTC day to ``failedDays`` (retries on the same day count once); a successful run
  empties it;
- on the day the count reaches the threshold (``trials.invalid_after_failed_days`` in config/sources.yaml), the
  source is marked ``invalid`` with the date and the reason, and from then on its job stops at once;
- the owner re-approves it on the Data sources tab (action ``source-retry``, a fresh second-factor code), which
  writes an approval newer than the mark. The job then starts over with an empty count, so it gets the full
  number of days again, and is marked invalid again, with the new date and reason, if it still fails. Earlier
  marks are kept in ``history``.

The job, not the site, writes the mark, and only in the data bucket: jobs never write the owner's approvals, so a
job can never approve anything. The site reads both and shows the result (``state``).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

VALIDITY = "validity.json"
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")  # as lc_ingest.trials
DEFAULT_FAILED_DAYS = 3
MAX_HISTORY = 20
MAX_TEXT = 300


def validity_name(source: str) -> str:
    """``trials/<source>/validity.json`` in the data bucket; refuses anything that isn't a plain source id."""
    if not SOURCE_ID.fullmatch(source or ""):
        raise ValueError(f"not a source id: {source!r}")
    return f"trials/{source}/{VALIDITY}"


def _time(value):
    """An ISO time (with Z or an offset) as an aware datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reapproved(validity: dict | None, decision: dict | None) -> bool:
    """Whether the owner approved the source after it was marked invalid."""
    mark = (validity or {}).get("invalid")
    if not mark or (decision or {}).get("status") != "approved":
        return False
    approved, marked = _time(decision.get("at")), _time(mark.get("at"))
    return bool(approved and marked and approved > marked)


def gate(validity: dict | None, decision: dict | None, source: str):
    """Before a trial run: (may it run, the validity to write back, an event or None).

    Invalid and not re-approved since: it may not run. Re-approved since: the mark moves to the history and the
    count starts again. The validity is always written back, so the trial bucket's lifecycle rule (objects older
    than two days) never takes the mark of a source that is still in the registry.
    """
    current = dict(validity or {})
    mark = current.get("invalid")
    if not mark:
        return True, current, None
    if reapproved(current, decision):
        history = (list(current.get("history") or []) + [{**mark, "reapprovedAt": decision.get("at"),
                                                          "reapprovedBy": decision.get("by")}])[-MAX_HISTORY:]
        fresh = {"failedDays": [], "retrialSince": decision.get("at"), "history": history}
        return True, fresh, {"event": "trial-retried", "source": source, "invalidSince": mark.get("at"),
                             "reapprovedAt": decision.get("at")}
    return False, current, {"event": "trial-invalid", "source": source, "since": mark.get("at"),
                            "why": "marked invalid; it runs again only once the owner re-approves it"}


def note(validity: dict | None, *, failed: bool, now: str, source: str, threshold: int = DEFAULT_FAILED_DAYS,
         failure: dict | None = None):
    """After a trial run: (the validity to write, an event or None). ``now`` is an ISO UTC time."""
    current = dict(validity or {})
    if not failed:
        current.update(failedDays=[], lastSuccess=now)
        return current, None
    failure = failure or {}
    error = str(failure.get("error") or "failed")[:80]
    message = " ".join(str(failure.get("message") or "").split())[:MAX_TEXT]
    days = list(current.get("failedDays") or [])
    day = str(now)[:10]
    if day not in days:
        days.append(day)
    current["failedDays"] = days
    current["lastFailure"] = {"at": now, "error": error, "message": message}
    if len(days) < max(1, int(threshold)) or current.get("invalid"):
        return current, None
    last = f"{error}: {message}" if message else error
    reason = (f"Failed on {len(days)} separate days with no successful run between ({', '.join(days)}). "
              f"Last error: {last}")[:MAX_TEXT + 200]
    current["invalid"] = {"at": now, "reason": reason, "failedDays": days}
    # Public logs: counts and the error's type only, never the message.
    return current, {"event": "trial-invalidated", "source": source, "failedDays": len(days), "error": error}


def state(validity: dict | None, decision: dict | None, threshold: int = DEFAULT_FAILED_DAYS) -> dict:
    """What the admin pages show: {state, at, reason, failedDays, threshold, history}.

    ``state`` is ``invalid`` (marked and not re-approved since), ``retrying`` (re-approved after a mark; the job
    clears the mark on its next run), ``failing`` (failed days counting towards the threshold) or ``ok``.
    """
    current = validity or {}
    mark = current.get("invalid")
    out = {"threshold": threshold, "failedDays": list(current.get("failedDays") or []),
           "history": list(reversed(current.get("history") or []))[:10], "retrialSince": current.get("retrialSince")}
    if mark and not reapproved(current, decision):
        return {**out, "state": "invalid", "at": mark.get("at"), "reason": mark.get("reason")}
    if mark:
        return {**out, "state": "retrying", "at": (decision or {}).get("at"), "reason": mark.get("reason"),
                "failedDays": []}
    if out["failedDays"]:
        last = current.get("lastFailure") or {}
        return {**out, "state": "failing", "at": last.get("at"),
                "reason": f"{last.get('error')}: {last.get('message')}" if last.get("message") else last.get("error")}
    return {**out, "state": "ok", "at": current.get("lastSuccess"), "reason": None}
