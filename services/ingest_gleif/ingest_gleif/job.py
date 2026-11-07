"""The daily GLEIF run, through the shared ingestion runner.

The first run loads the full golden copy, about 3.5 million LEIs. Later runs
take the smallest delta that covers the gap since the last one processed, so a
missed day loses nothing; a gap over a month reloads the full file.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

from lc_core.allowlist import AllowList
from lc_core.control import require_running
from lc_core.fetch import Gateway
from lc_ingest.gleif import to_record
from lc_ingest.gleif_csv import read_zip
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore

COMPONENT = "job:ingest-gleif"
LISTING = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
DELTAS = [(timedelta(days=1), "LastDay"), (timedelta(days=7), "LastWeek"), (timedelta(days=31), "LastMonth")]


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def _when(publish: str) -> datetime:
    return datetime.strptime(publish, "%Y-%m-%d %H:%M:%S")


def choose(manifest: dict, publish: str) -> str:
    """Which file covers everything since the last publish processed."""
    last = manifest.get("lastKey")
    if not manifest.get("records") or not last:
        return "full"
    gap = _when(publish) - _when(last)
    for limit, name in DELTAS:
        if gap <= limit:
            return name
    return "full"


def find(gateway, manifest):
    listing = gateway.get_json(LISTING)["data"]
    publish = listing["publish_date"]
    kind = choose(manifest, publish)
    files = listing["lei2"]
    url = files["full_file"]["csv"]["url"] if kind == "full" else files["delta_files"][kind]["csv"]["url"]
    name = publish.replace(" ", "T").replace(":", "")
    return publish, url, f"{name}-{kind}.zip"


def run(gateway, store, gate=lambda: require_running(COMPONENT), allowlist=None, log=log) -> str:
    return run_ingest(gateway=gateway, store=store, gate=gate, find=find, read=read_zip, mapper=to_record,
                      source="gleif", maker="ingestion@gleif", allowlist=allowlist or AllowList.load(), log=log)


def main() -> int:
    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    # An explicit path: a missing allow-list must stop the job, never fall back to nothing.
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    outcome = run(gateway, GcsStore(os.environ["LC_DATA_BUCKET"], "gleif"), allowlist=allowlist)
    return 0 if outcome in ("done", "already-done", "halted") else 1


if __name__ == "__main__":
    sys.exit(main())
