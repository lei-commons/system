"""Ingestion for national registers published as bulk files (lc_ingest.bulk): one image, one job per register.

The job's LC_SOURCE names its register (ie-cro, ...). Each run processes the register's file once per
ISO week, through the conduct gateway, and only after the system is running and the owner has approved
that source.

A source on trial (config/sources.yaml stage; docs/source-trials.md) runs this same image, deployed by the
same workflow, so it always runs production's code. It only measures: it keeps no records, only its quality
report, in trials/<source>/, and measures a publication again only when the code changes (LC_CODE_VERSION,
the deployed commit) or a new publication appears. Its reports go when the trial ends (lc_ingest.trials):
on promotion, at the start of the first production run; dropped from the registry, by LC_CLEANUP=1.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

from lc_core.allowlist import AllowList
from lc_core.approvals import require_approved
from lc_core.control import SystemHalted, require_running
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.fetch import Gateway
from lc_core.lease import Busy, lease
from lc_ingest import health
from lc_ingest.runner import run_ingest
from lc_ingest.bulk import Profile
from lc_ingest.profiles import PROFILES
from lc_ingest.store import GcsStore


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def profile_for(source: str) -> Profile:
    if source not in PROFILES:
        raise SystemExit(f"LC_SOURCE must be one of {sorted(PROFILES)}, not {source!r}")
    return PROFILES[source]


def gate_for(profile: Profile):
    def gate():
        """Run only while the system is running and the owner has approved this source."""
        require_running(f"job:{profile.job}")
        require_approved(profile.source)
    return gate


TRIALS = "trials"  # the data bucket's prefix for trial reports (lc_ingest.trials)


def code_version() -> str:
    """The deployed commit (deploy-ingest-bulk.yml sets LC_CODE_VERSION), short: a trial re-measures when it changes."""
    return (os.environ.get("LC_CODE_VERSION") or "local")[:12]


def find_for(profile: Profile, today: date, part=None, code: str = None):
    inner = _find_for(profile, today, part)
    if not code:
        return inner

    def find(gateway, manifest=None):  # a trial: the same publication is measured again under new code
        key, url, filename = inner(gateway, manifest)
        return f"{key} @{code}", url, filename
    return find


def _find_for(profile: Profile, today: date, part=None):
    def find(gateway, manifest=None):
        if part:  # one of a register's several files: its own key, so each is processed once a week
            name, url = part
            if callable(url):  # a file that moves: its catalogue is asked now, after the gate
                url = url(gateway)
            key = f"{profile.key(today)} {name}"
            return key, url, f"{profile.filename}-{profile.key(today)}-{name}{profile.extension}"
        if profile.locate:  # the register's file moves with each publication: ask its catalogue where it is
            key, url = profile.locate(gateway)
        else:
            key, url = profile.key(today), profile.url()
        return key, url, f"{profile.filename}-{key}{profile.extension}"
    return find


def quality_for(profile: Profile, stage: str, today: date):
    """For a source on trial: a fresh quality measure per attempt (lc_ingest.quality). Production: none."""
    if stage != "trial":
        return None
    from lc_core.status import load as load_statuses
    from lc_ingest.quality import Quality

    try:
        statuses = load_statuses()
    except Exception:  # noqa: BLE001 - a trial still measures everything else; statusUnmapped is then None
        statuses = None
    return lambda: Quality(profile.source, statuses=statuses, today=today)


def run(profile: Profile, gateway, store, today=None, gate=None, allowlist=None, log=log, stage=None) -> str:
    """Process the register's file, or each of its files in turn. The outcome of the first that isn't done stops the rest.
    A source on trial (config/sources.yaml) only measures its data's quality, into ``store`` (trials/<source>/)."""
    from lc_core.sources import stage as stage_of

    today = today or date.today()
    allowlist = allowlist or AllowList.load()
    stage = stage or stage_of(profile.source)
    quality = quality_for(profile, stage, today)
    code = code_version() if stage == "trial" else None
    outcome = "already-done"
    for part in profile.parts or (None,):
        outcome = run_ingest(gateway=gateway, store=store, gate=gate or gate_for(profile), find=find_for(profile, today, part, code),
                             read=profile.read, mapper=profile.record, source=profile.source,
                             maker=f"ingestion@{profile.store}", allowlist=allowlist, log=log, quality=quality,
                             fetch=profile.fetch)
        if outcome not in ("done", "already-done"):
            break
    return outcome


FAILED_OUTCOMES = ("denied", "held")  # a trial run that ended without measuring; an exception counts too


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def trial_gate(source: str, store, decisions=None, log=log) -> bool:
    """Before a trial run: may it run? A source marked invalid stops here until the owner re-approves it
    (lc_core.validity). The mark is written back on every run, so the trial lifecycle rule never takes it."""
    from lc_core.approvals import load_decisions
    from lc_core.validity import VALIDITY, gate

    decisions = load_decisions() if decisions is None else decisions
    may, validity, event = gate(store.read_json(VALIDITY), decisions.get(source), source)
    if event:
        log(event)
    store.write_json(VALIDITY, validity)
    return may


def trial_note(source: str, store, *, failed: bool, failure: dict = None, now: str = None, threshold: int = None,
               log=log) -> None:
    """After a trial run: count a failed day, or clear the count; mark the source invalid on the day the count
    reaches the threshold. Never raises: this must not hide the run's own result."""
    from lc_core.sources import invalid_after_failed_days
    from lc_core.validity import VALIDITY, note

    try:
        threshold = threshold or invalid_after_failed_days()
        validity, event = note(store.read_json(VALIDITY), failed=failed, failure=failure, now=now or _now(),
                               source=source, threshold=threshold)
        store.write_json(VALIDITY, validity)
        if event:
            log(event)
    except Exception as exc:  # noqa: BLE001
        log({"event": "validity-unwritten", "source": source, "error": type(exc).__name__})


def trial_outcome(source: str, store, outcome: str, ended: dict, log=log, **kw) -> None:
    """Count a trial run that ended without measuring (denied, held); clear the count after one that measured.
    Halted and busy runs say nothing about the source, nor does a denial because the system isn't running."""
    if outcome in ("done", "already-done"):
        trial_note(source, store, failed=False, log=log, **kw)
    elif outcome in FAILED_OUTCOMES and ended.get("rule") != "system-not-running":
        rule = ended.get("rule")
        trial_note(source, store, failed=True, log=log, **kw,
                   failure={"error": f"{outcome} ({rule})" if rule else outcome,
                            "message": ended.get("reason") or f"the run ended {outcome}"})


def cleanup(source: str, bucket, log=log) -> int:
    """LC_CLEANUP=1: a trial that left the registry. Delete its reports, every version, and stop. The deploy
    workflow runs this once, then deletes the job (docs/source-trials.md)."""
    from lc_ingest import trials

    deleted = trials.remove(bucket, source)
    log({"event": "trial-cleaned", "source": source, "objects": deleted, "why": "the trial left the registry"})
    return 0


def main() -> int:
    from google.cloud import storage

    from lc_core.sources import stage as stage_of
    from lc_ingest import trials

    client = storage.Client()
    bucket = os.environ["LC_DATA_BUCKET"]
    if os.environ.get("LC_CLEANUP") == "1":
        return cleanup(os.environ.get("LC_SOURCE", ""), client.bucket(bucket))
    profile = profile_for(os.environ.get("LC_SOURCE", ""))
    gateway = Gateway(os.environ["LC_GATEWAY_URL"], f"job:{profile.job}")
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    record = Recorder(f"job:{profile.job}", log)
    on_trial = stage_of(profile.source) == "trial"
    # A trial keeps only its reports, apart from every store; production writes the source's store.
    store = GcsStore(bucket, f"{TRIALS}/{profile.source}" if on_trial else profile.store)
    try:
        if on_trial and not trial_gate(profile.source, store, log=record):
            return 0  # marked invalid: not processed until the owner re-approves it (lc_core.validity)
        if not on_trial:  # promoted (or never on trial): whatever a trial left is deleted, within a day of promotion
            deleted = trials.remove(client.bucket(bucket), profile.source)
            if deleted:
                record({"event": "trial-cleaned", "source": profile.source, "objects": deleted, "why": "promoted"})
        ended = {}  # the event a run that didn't measure ended with (its rule and reason), for the invalid mark

        def logged(event):
            if event.get("event") in FAILED_OUTCOMES:
                ended.update(event)
            record(event)

        with lease(client.bucket(bucket), profile.job):
            outcome = run(profile, gateway, store, allowlist=allowlist, log=logged)
        health.note(store, outcome=outcome, log=record)  # a browser fallback reads it (ingest_browser)
        if profile.tidy:  # an owner-supplied source: its processed bundles go (lc_ingest.owner_supplied)
            profile.tidy(client.bucket(bucket), profile.source, store.read_manifest()["processed"],
                         production=not on_trial and outcome in ("done", "already-done"), log=record)
        if on_trial:
            trial_outcome(profile.source, store, outcome, ended, log=record)
        if on_trial and outcome in ("done", "already-done"):
            # A live trial's reports are rewritten every run, so the safety-net lifecycle rule never takes them.
            record({"event": "trial-refreshed", "source": profile.source,
                    "objects": trials.refresh(client.bucket(bucket), profile.source)})
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        failed = failure(exc)
        record(failed)
        health.note(store, failure=failed, log=record)
        if on_trial and not isinstance(exc, SystemHalted):  # a system problem (e.g. approvals unreadable), not the source's
            trial_note(profile.source, store, failed=True, failure=failed, log=record)
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
