"""A source's download health, kept beside its store (health.json): consecutive failed runs, and whether
the last failure was the source refusing us. The regular job writes it after every run; a browser
fallback (ingest_browser, owner decision 2026-11-18) reads it to decide whether it may run: only after the
regular download has failed consistently, and never when the source refused us (Article 2.1).
"""

from __future__ import annotations

from datetime import datetime, timezone

HEALTH = "health.json"
OK_OUTCOMES = {"done", "already-done"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def note(store, outcome: str = None, failure: dict = None, now=None, log=None) -> dict | None:
    """Record a run's result: an outcome ("done", "already-done") or a failure record (lc_core.decisions
    ``failure``). Busy, halted or skipped runs change nothing. Never raises: health must not stop a job."""
    if failure is None and outcome not in OK_OUTCOMES:
        return None
    try:
        health = store.read_json(HEALTH) or {}
        when = now or _now()
        if failure is None:
            health.update(consecutiveFailures=0, lastSuccess=when)
        else:
            health["consecutiveFailures"] = int(health.get("consecutiveFailures") or 0) + 1
            health["lastFailure"] = {"at": when, "error": failure.get("error"), "message": failure.get("message", ""),
                                     "refused": bool(failure.get("refused")), "status": failure.get("status")}
        store.write_json(HEALTH, health)
        return health
    except Exception as exc:  # noqa: BLE001
        if log:
            log({"event": "health-unwritten", "error": type(exc).__name__})
        return None


def fallback_decision(health: dict | None, after_failures: int) -> tuple:
    """(may the browser fallback run, why)."""
    last = (health or {}).get("lastFailure")
    failures = int((health or {}).get("consecutiveFailures") or 0)
    if not last or failures == 0:
        return False, "the regular download isn't failing"
    if last.get("refused"):
        return False, f"the source refused us (HTTP {last.get('status')}): a browser is never used to get around a refusal"
    if failures < after_failures:
        return False, f"only {failures} failed run(s) in a row; the fallback waits for {after_failures}"
    return True, f"{failures} failed runs in a row, last: {last.get('error')}"
