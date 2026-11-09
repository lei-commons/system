"""The Singapore run: ACRA's corporate entities from data.gov.sg.

The collection holds 27 datasets, one per initial letter. A run lists them,
asks data.gov.sg to prepare each download, waits for its signed link (served
from S3 in Singapore; host approved by the owner on 2026-11-09), and joins
the files into one input for the runner. A collection update is processed
once, keyed by its last-updated date. Sole proprietorships and partnerships
are dropped in the mapper.
"""

from __future__ import annotations

import json
import os
import sys
import time

from lc_core.allowlist import AllowList
from lc_core.control import require_running
from lc_core.decisions import Recorder, log_bucket_name
from lc_core.fetch import FetchError, Gateway
from lc_core.lease import Busy, lease
from lc_ingest.registers import read_csv, sg_record
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore

COMPONENT = "job:ingest-sg"
COLLECTION = "https://api-production.data.gov.sg/v2/public/api/collections/2/metadata"
DOWNLOAD = "https://api-open.data.gov.sg/v1/public/api/datasets/{}/{}"
POLLS, POLL_WAIT = 30, 3


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def collection(gateway) -> dict:
    return gateway.get_json(COLLECTION)["data"]["collectionMetadata"]


def find(gateway, manifest=None):
    meta = collection(gateway)
    key = f"acra-{meta['lastUpdatedAt'][:10]}"
    return key, COLLECTION, f"{key}.csv"


def fetch_all(sleep=time.sleep):
    def fetch(gateway, _url, out) -> int:
        size = 0
        datasets = collection(gateway)["childDatasets"]
        for dataset in datasets:
            gateway.get_json(DOWNLOAD.format(dataset, "initiate-download"))
            for _ in range(POLLS):
                status = gateway.get_json(DOWNLOAD.format(dataset, "poll-download"))["data"]
                if status.get("status") == "DOWNLOAD_SUCCESS" and status.get("url"):
                    break
                sleep(POLL_WAIT)
            else:
                raise FetchError(f"dataset {dataset} wasn't ready to download")
            size += gateway.download(status["url"], out)
            out.write(b"\n")
        if len(datasets) < 20:
            raise FetchError(f"the ACRA collection lists only {len(datasets)} datasets")
        return size
    return fetch


def run(gateway, store, gate=lambda: require_running(COMPONENT), allowlist=None, log=log, sleep=time.sleep) -> str:
    return run_ingest(gateway=gateway, store=store, gate=gate, find=find, read=read_csv, mapper=sg_record,
                      source="sg-acra", maker="ingestion@sg", allowlist=allowlist or AllowList.load(), log=log,
                      fetch=fetch_all(sleep))


def main() -> int:
    from google.cloud import storage

    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    record = Recorder(COMPONENT, log)
    try:
        with lease(client.bucket(bucket), "ingest-sg"):
            outcome = run(gateway, GcsStore(bucket, "sg"), allowlist=allowlist, log=record)
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
