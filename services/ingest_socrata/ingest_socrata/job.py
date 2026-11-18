"""Ingestion for the US state registers on Socrata portals (lc_ingest.socrata): one image, one job per register.

The job's LC_SOURCE names its register (us-co-sos, us-or-sos, us-ct-sots). Like New York's, each run
processes the register once per ISO week, through the conduct gateway, and only after the system is
running and the owner has approved that source.
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
from lc_ingest.registers import read_csv
from lc_ingest.runner import run_ingest
from lc_ingest.socrata import PROFILES, Profile
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


def find_for(profile: Profile, today: date):
    def find(gateway, manifest=None):
        key = profile.key(today)
        return key, profile.url(), f"{profile.store}-{key}.csv"
    return find


def run(profile: Profile, gateway, store, today=None, gate=None, allowlist=None, log=log) -> str:
    return run_ingest(gateway=gateway, store=store, gate=gate or gate_for(profile), find=find_for(profile, today or date.today()),
                      read=read_csv, mapper=profile.record, source=profile.source, maker=f"ingestion@{profile.store}",
                      allowlist=allowlist or AllowList.load(), log=log)


def main() -> int:
    from google.cloud import storage

    profile = profile_for(os.environ.get("LC_SOURCE", ""))
    gateway = Gateway(os.environ["LC_GATEWAY_URL"], f"job:{profile.job}")
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    record = Recorder(f"job:{profile.job}", log)
    try:
        with lease(client.bucket(bucket), profile.job):
            outcome = run(profile, gateway, GcsStore(bucket, profile.store), allowlist=allowlist, log=record)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        record(failure(exc))
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
