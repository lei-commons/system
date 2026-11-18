"""Ingestion for sources whose data is offered only through a web page or form, fetched with a real browser.

The job's LC_SOURCE names its source (jp-nta, ...). Each run first reads which publication is current
with one plain request; if it's already processed, it stops there and no browser starts. Otherwise a
headless browser follows the source's flow (lc_browser: every request through the conduct gateway, at a
person's pace) and the downloaded file goes through the usual runner: file checks, retries, the volume
guard, change files. Only while the system is running and the owner has approved the source.
"""

from __future__ import annotations

import json
import os
import sys

from lc_core.allowlist import AllowList
from lc_core.approvals import require_approved
from lc_core.control import require_running
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.fetch import FetchError, Gateway
from lc_core.lease import Busy, lease
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore

from .sources import PROFILES, Profile


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


def fetch_for(profile: Profile, key: str, log=log, download=None):
    """The runner's fetch hook: the browser downloads the file into the runner's file."""
    def fetch(gateway, _url, out) -> int:
        if download is None:
            from lc_browser.runner import download as browse
        else:
            browse = download
        names = []
        size = browse(profile.flow, gateway, out, profile.hosts, log=lambda e: (names.append(e.get("file")), log(e)))
        name = next((n for n in names if n), "")
        expected = profile.expect_in_name(key)
        if expected not in name:
            raise FetchError(f"the page offered {name or 'no file'}, not the publication of {key}")
        return size
    return fetch


def run(profile: Profile, gateway, store, gate=None, allowlist=None, log=log, download=None, sleep=None) -> str:
    found = {}

    def find(gw, manifest=None):
        found["key"], url, filename = profile.current(gw)
        return found["key"], url, filename

    def fetch(gw, url, out):
        return fetch_for(profile, found["key"], log=log, download=download)(gw, url, out)

    return run_ingest(gateway=gateway, store=store, gate=gate or gate_for(profile), find=find, read=profile.read,
                      mapper=profile.record, source=profile.source, maker=f"ingestion@{profile.store}",
                      allowlist=allowlist or AllowList.load(), log=log, fetch=fetch,
                      **({"sleep": sleep} if sleep else {}))


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
