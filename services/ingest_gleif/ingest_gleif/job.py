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

from lc_core import gleif_identity
from lc_core.allowlist import AllowList
from lc_core.control import SystemHalted, require_running
from lc_core.approvals import require_approved
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.lease import Busy, lease
from lc_core.fetch import Gateway
from lc_ingest.gleif import to_record
from lc_ingest.gleif_csv import read_zip
from lc_ingest.gleif_rr import read_zip as read_rr_zip
from lc_ingest.gleif_rr import to_relationship
from lc_ingest.rekey import rekey
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore
from lc_ingest import health

COMPONENT = "job:ingest-gleif"
SOURCE_ID = "gleif"  # its entry in config/sources.yaml


def gate():
    """Run only while the system is running and the owner has approved this source."""
    require_running(COMPONENT)
    require_approved(SOURCE_ID)

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
    if last[:19] == publish:
        return None  # this publication is already loaded
    gap = _when(publish) - _when(last[:19])  # keys are "<publish> <kind>"
    for limit, name in DELTAS:
        if gap <= limit:
            return name
    return "full"


def finder(dataset: str):
    """``dataset`` is "lei2" for entities or "rr" for relationships."""
    def find(gateway, manifest):
        listing = gateway.get_json(LISTING)["data"]
        publish = listing["publish_date"]
        kind = choose(manifest, publish)
        if kind is None:
            return manifest["lastKey"], "", ""  # already processed, so the runner stops here
        files = listing[dataset]
        url = files["full_file"]["csv"]["url"] if kind == "full" else files["delta_files"][kind]["csv"]["url"]
        name = publish.replace(" ", "T").replace(":", "")
        # The key names the file type as well as the publish time: a full load and
        # a delta from the same publication are different work.
        return f"{publish} {kind}", url, f"{name}-{dataset}-{kind}.zip"

    return find


find = finder("lei2")


def run(gateway, store, gate=gate, allowlist=None, log=log) -> str:
    # The identity rules (config/sources.yaml gleif_identity), checked before anything is fetched: a bad entry
    # must stop the run, never turn into records skipped one by one as malformed.
    gleif_identity.default()
    return run_ingest(gateway=gateway, store=store, gate=gate, find=find, read=read_zip, mapper=to_record,
                      source="gleif", maker="ingestion@gleif", allowlist=allowlist or AllowList.load(), log=log)


def run_entities(gateway, store, gate=gate, allowlist=None, log=log, identity=None) -> str:
    """The day's entity file, then, once per set of identity rules (config/sources.yaml gleif_identity),
    the re-key of every stored record (lc_ingest.rekey; event gleif-rekeyed). The re-key runs only after
    the file went in cleanly, so it always works on the newest records."""
    outcome = run(gateway, store, gate=gate, allowlist=allowlist, log=log)
    if outcome not in ("done", "already-done"):
        return outcome
    rekeyed = rekey(store, identity=identity, gate=gate, log=log, maker="ingestion@gleif")
    return rekeyed if rekeyed in ("held", "halted") else outcome


def run_relationships(gateway, store, gate=gate, allowlist=None, log=log) -> str:
    return run_ingest(gateway=gateway, store=store, gate=gate, find=finder("rr"), read=read_rr_zip,
                      mapper=to_relationship, source="gleif-rr", maker="ingestion@gleif",
                      allowlist=allowlist or AllowList.load(), log=log, kind="relationship")


def main() -> int:
    from google.cloud import storage

    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    # An explicit path: a missing allow-list must stop the job, never fall back to nothing.
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    record = Recorder(COMPONENT, log)  # every decision goes to the decision log too
    try:
        with lease(client.bucket(bucket), "ingest-gleif"):
            # Entities and relationships are separate files: one failing doesn't stop the other.
            outcomes, errors = [], []
            entities, relationships = GcsStore(bucket, "gleif"), GcsStore(bucket, "gleif-rr")
            for store, work in ((entities, lambda: run_entities(gateway, entities, allowlist=allowlist, log=record)),
                                (relationships, lambda: run_relationships(gateway, relationships, allowlist=allowlist,
                                                                          log=record))):
                try:
                    outcome = work()
                    outcomes.append(outcome)
                    health.note(store, outcome=outcome, log=record)  # each file's health beside its own store
                except SystemHalted:
                    raise
                except Exception as exc:  # recorded below, after the other part has had its turn
                    errors.append(exc)
                    health.note(store, failure=failure(exc), log=record)
            if errors:
                raise errors[0]
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})  # another run is doing this work
        outcomes = ["busy"]
    except Exception as exc:
        record(failure(exc))
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return 0 if all(o in ("done", "already-done", "halted", "busy") for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
