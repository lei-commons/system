"""The New York run: the Department of State's active corporations, once a week.

data.ny.gov refreshes the dataset daily, but the list barely changes from one
day to the next, and comparing 4.3 million records takes most of an hour. So
a run processes the dataset once per ISO week ("2026-W46"). Only the columns
in lc_ingest.registers.NY_COLUMNS are requested, so no agent, process or
chairman name or address is ever downloaded.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from datetime import date

from lc_core.allowlist import AllowList
from lc_core.control import require_running
from lc_core.approvals import require_approved
from lc_core.decisions import Recorder, log_bucket_name
from lc_core.fetch import Gateway
from lc_core.lease import Busy, lease
from lc_ingest.registers import NY_COLUMNS, ny_record, read_csv
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore

COMPONENT = "job:ingest-ny"
SOURCE_ID = "us-ny-dos"  # its entry in config/sources.yaml


def gate():
    """Run only while the system is running and the owner has approved this source."""
    require_running(COMPONENT)
    require_approved(SOURCE_ID)

DATASET = "https://data.ny.gov/resource/n9v6-gdp6.csv"
LIMIT = 20_000_000  # above the whole dataset (4.3 million in November 2026), so one request returns it all


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def url() -> str:
    query = urllib.parse.urlencode({"$select": ",".join(NY_COLUMNS), "$order": "dos_id", "$limit": str(LIMIT)}, safe="$,")
    return f"{DATASET}?{query}"


def find(today):
    def find(gateway, manifest=None):
        year, week, _ = today.isocalendar()
        key = f"{year}-W{week:02d}"
        return key, url(), f"ny-active-corporations-{key}.csv"
    return find


def run(gateway, store, today=None, gate=gate, allowlist=None, log=log) -> str:
    return run_ingest(gateway=gateway, store=store, gate=gate, find=find(today or date.today()), read=read_csv,
                      mapper=ny_record, source="us-ny-dos", maker="ingestion@us-ny", allowlist=allowlist or AllowList.load(),
                      log=log)


def main() -> int:
    from google.cloud import storage

    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    record = Recorder(COMPONENT, log)
    try:
        with lease(client.bucket(bucket), "ingest-ny"):
            outcome = run(gateway, GcsStore(bucket, "us-ny"), allowlist=allowlist, log=record)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        record({"event": "failed", "error": type(exc).__name__})
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
