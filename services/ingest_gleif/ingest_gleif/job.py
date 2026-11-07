"""The daily GLEIF run: the LastDay delta, through the shared ingestion runner."""

from __future__ import annotations

import json
import os
import sys

from lc_core.allowlist import AllowList
from lc_core.control import require_running
from lc_core.fetch import Gateway
from lc_ingest.gleif import to_record
from lc_ingest.gleif_csv import read_zip
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore

COMPONENT = "job:ingest-gleif"
LISTING = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
DELTA = "LastDay"


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def find(gateway):
    listing = gateway.get_json(LISTING)["data"]
    publish = listing["publish_date"]
    name = publish.replace(" ", "T").replace(":", "")
    return publish, listing["lei2"]["delta_files"][DELTA]["csv"]["url"], f"{name}-{DELTA}.zip"


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
