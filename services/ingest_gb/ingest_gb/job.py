"""The Companies House run: the monthly basic company data, part 1 of 7.

Part 1 holds about 850,000 companies, the UK sample size in the design. The
download page is checked daily; a month's file is processed once.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

from lc_core.allowlist import AllowList
from lc_core.control import require_running
from lc_core.lease import Busy, lease
from lc_core.fetch import FetchError, Gateway
from lc_ingest.companies_house import read_zip, to_record
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore

COMPONENT = "job:ingest-gb"
SITE = "https://download.companieshouse.gov.uk/"
PAGE = SITE + "en_output.html"
PART = re.compile(r"BasicCompanyData-(\d{4}-\d{2}-\d{2})-part1_7\.zip")


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def find(gateway, manifest=None):
    page = io.BytesIO()
    gateway.download(PAGE, page)
    match = PART.search(page.getvalue().decode("utf-8", "replace"))
    if not match:
        raise FetchError("no part 1 file on the download page")
    return match.group(1), SITE + match.group(0), match.group(0)


def run(gateway, store, gate=lambda: require_running(COMPONENT), allowlist=None, log=log) -> str:
    return run_ingest(gateway=gateway, store=store, gate=gate, find=find, read=read_zip, mapper=to_record,
                      source="gb-coh", maker="ingestion@gb", allowlist=allowlist or AllowList.load(), log=log)


def main() -> int:
    from google.cloud import storage

    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    try:
        with lease(storage.Client().bucket(os.environ["LC_DATA_BUCKET"]), "ingest-gb"):
            outcome = run(gateway, GcsStore(os.environ["LC_DATA_BUCKET"], "gb"), allowlist=allowlist)
    except Busy as exc:
        log({"event": "busy", "reason": str(exc)})  # another run is doing this work
        return 0
    return 0 if outcome in ("done", "already-done", "halted") else 1


if __name__ == "__main__":
    sys.exit(main())
