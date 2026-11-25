"""The daily cycle's settings, config/cycle.yaml: one place for every number the cycle and its pages use.

Read by scripts/render_cycle.py (the Workflows definition), Terraform (the schedule) and the site (the
schedule shown on the System page). Nothing else hard-codes them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class CycleConfigError(ValueError):
    pass


def _whole(value, low, high, name):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise CycleConfigError(f"{name} must be a whole number from {low} to {high}")
    return value


def settings(config: dict) -> dict:
    """Validated settings: retry_wait_minutes, retries, schedule_utc ("HH:MM"), cron."""
    config = config or {}
    sources = config.get("sources") or {}
    timeouts = config.get("timeouts_minutes") or {}
    if "default" not in timeouts:
        raise CycleConfigError("timeouts_minutes needs a default")
    timeout_minutes = {str(job): _whole(v, 10, 1440, f"timeouts_minutes.{job}") for job, v in timeouts.items()}
    watchdogs = config.get("watchdogs") or {}
    dedi_check = str(watchdogs.get("dedi_check_utc", ""))
    if not TIME.match(dedi_check):
        raise CycleConfigError("watchdogs.dedi_check_utc must be a time of day, HH:MM (UTC)")
    when = str(config.get("schedule_utc", ""))
    match = TIME.match(when)
    if not match:
        raise CycleConfigError("schedule_utc must be a time of day, HH:MM (UTC)")
    return {
        "retry_wait_minutes": _whole(sources.get("retry_wait_minutes"), 1, 120, "sources.retry_wait_minutes"),
        "retries": _whole(sources.get("retries"), 0, 12, "sources.retries"),
        "browser_fallback_after_failures": _whole(sources.get("browser_fallback_after_failures"), 1, 20,
                                                  "sources.browser_fallback_after_failures"),
        "schedule_utc": when,
        "dedi_check_utc": dedi_check,
        "timeout_minutes": timeout_minutes,
        "cron": f"{int(match.group(2))} {int(match.group(1))} * * *",
    }


def default_path() -> Path:
    env = os.environ.get("LC_CYCLE_CONFIG")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:  # the repository in development; images set LC_CYCLE_CONFIG
        candidate = parent / "config" / "cycle.yaml"
        if candidate.exists():
            return candidate
    raise CycleConfigError("config/cycle.yaml not found (set LC_CYCLE_CONFIG)")


def load(path=None) -> dict:
    return settings(yaml.safe_load(Path(path or default_path()).read_text()))


def timeout_seconds(settings: dict, job: str) -> int:
    """How long ``job`` may run (config/cycle.yaml timeouts_minutes), in seconds."""
    minutes = settings["timeout_minutes"]
    return 60 * minutes.get(job, minutes["default"])
