"""Ingestion for national registers published as bulk files (lc_ingest.bulk): one image, one job per register.

The job's LC_SOURCE names its register (ie-cro, ...). Each run processes the register's file once per
ISO week, through the conduct gateway, and only after the system is running and the owner has approved
that source.

A source on trial (config/sources.yaml stage; docs/source-trials.md) runs this same image, deployed by the
same workflow, so it always runs production's code. It only measures: it keeps no records, only its quality
report, in trials/<source>/ (deleted by the bucket's lifecycle after 60 days), and measures a publication
again only when the code changes (LC_CODE_VERSION, the deployed commit) or a new publication appears.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

from lc_core.allowlist import AllowList
from lc_core.approvals import require_approved
from lc_core.control import require_running
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.fetch import Gateway
from lc_core.lease import Busy, lease
from lc_ingest import health
from lc_ingest.runner import run_ingest
from lc_ingest.bulk import PROFILES, Profile
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


TRIALS = "trials"  # the data bucket's prefix for trial reports (infra/terraform/storage.tf deletes them after 60 days)


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

    statuses = load_statuses()
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
                             maker=f"ingestion@{profile.store}", allowlist=allowlist, log=log, quality=quality)
        if outcome not in ("done", "already-done"):
            break
    return outcome


def main() -> int:
    from google.cloud import storage

    profile = profile_for(os.environ.get("LC_SOURCE", ""))
    gateway = Gateway(os.environ["LC_GATEWAY_URL"], f"job:{profile.job}")
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    from lc_core.sources import stage as stage_of

    record = Recorder(f"job:{profile.job}", log)
    on_trial = stage_of(profile.source) == "trial"
    # A trial keeps only its reports, apart from every store; production writes the source's store.
    store = GcsStore(bucket, f"{TRIALS}/{profile.source}" if on_trial else profile.store)
    try:
        with lease(client.bucket(bucket), profile.job):
            outcome = run(profile, gateway, store, allowlist=allowlist, log=record)
        health.note(store, outcome=outcome, log=record)  # a browser fallback reads it (ingest_browser)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        failed = failure(exc)
        record(failed)
        health.note(store, failure=failed, log=record)
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
