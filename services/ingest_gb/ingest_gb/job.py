"""The Companies House run: the monthly basic company data, all 7 parts.

Each run processes the next part of the newest month that isn't in yet, so a
month's register comes in over a week of daily runs. Part 1 keeps the key it
had when it was the only part ("2026-11-01"); the others are "2026-11-01 part2"
and so on.

The file lists live companies only. A company that drops out of every part of
a month has been dissolved or removed. Once all 7 parts of a month are in,
``reconcile`` labels the stored records that no part listed ``absent-from-source:gb-coh``.
The label goes through the same change file, ledger and volume guard as any
other change. A company that comes back loses the label at its next ingest,
because the file's record carries no such label. The legal status isn't
touched: absence alone doesn't say why.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import sys
import tempfile

from lc_core.allowlist import AllowList
from lc_core.control import require_running
from lc_core.approvals import require_approved
from lc_core.decisions import Recorder, log_bucket_name
from lc_core.lease import Busy, lease
from lc_core.fetch import FetchError, Gateway
from lc_ingest.companies_house import read_zip, to_record
from lc_ingest.ledger import VolumeGuardTripped, diff, volume_guard
from lc_ingest.runner import run_ingest
from lc_ingest.store import SHARDS, GcsStore, encode_records

COMPONENT = "job:ingest-gb"
SOURCE_ID = "gb-coh"  # its entry in config/sources.yaml


def gate():
    """Run only while the system is running and the owner has approved this source."""
    require_running(COMPONENT)
    require_approved(SOURCE_ID)

SITE = "https://download.companieshouse.gov.uk/"
PAGE = SITE + "en_output.html"
PARTS = 7
PART = re.compile(r"BasicCompanyData-(\d{4}-\d{2}-\d{2})-part(\d)_7\.zip")
ABSENT = "absent-from-source:gb-coh"


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def part_key(date: str, part: int) -> str:
    return date if part == 1 else f"{date} part{part}"


def find(gateway, manifest=None):
    """The newest month's first part not yet processed: (key, url, filename)."""
    page = io.BytesIO()
    gateway.download(PAGE, page)
    found = PART.findall(page.getvalue().decode("utf-8", "replace"))
    if not found:
        raise FetchError("no basic company data files on the download page")
    latest = max(date for date, _ in found)
    parts = sorted({int(k) for date, k in found if date == latest})
    if parts != list(range(1, PARTS + 1)):
        raise FetchError(f"expected {PARTS} parts for {latest}, the page lists {parts}")
    processed = set((manifest or {}).get("processed", []))
    for part in parts:
        if part_key(latest, part) not in processed:
            name = f"BasicCompanyData-{latest}-part{part}_7.zip"
            return part_key(latest, part), SITE + name, name
    return part_key(latest, PARTS), "", ""  # every part is in: the runner reports already-done


def run(gateway, store, gate=gate, allowlist=None, log=log) -> str:
    return run_ingest(gateway=gateway, store=store, gate=gate, find=find, read=read_zip, mapper=to_record,
                      source="gb-coh", maker="ingestion@gb", allowlist=allowlist or AllowList.load(), log=log,
                      seen=True)


def reconcile(store, log=log) -> str:
    """Label the records that no part of the newest complete month listed. Returns the outcome."""
    manifest = store.read_manifest()
    dates = sorted({k.split(" ")[0] for k in manifest.get("processed", []) if re.fullmatch(r"\d{4}-\d{2}-\d{2}( part\d)?", k)})
    if not dates:
        return "nothing"
    date = dates[-1]
    if store.has_marker(f"reconciled-{date}"):
        return "already-done"
    keys = [part_key(date, part) for part in range(1, PARTS + 1)]
    if not all(k in manifest["processed"] for k in keys):
        return "waiting"
    seen = set()
    for key in keys:
        ids = store.read_seen(key)
        if ids is None:  # loaded before the IDs were kept: this month can't be checked
            store.write_marker(f"reconciled-{date}", {"skipped": f"no list of IDs for {key}"})
            log({"event": "reconcile-skipped", "source": "gb", "publish": date,
                 "reason": f"{key} was loaded before its IDs were kept"})
            return "skipped"
        seen |= ids

    source_id, maker = f"src:gb-coh-reconcile-{date}", "reconcile@gb"
    absent = total = changes = 0
    with tempfile.TemporaryDirectory(prefix="reconcile-") as work:
        changes_path, staged = os.path.join(work, "changes.jsonl.gz"), {}
        with gzip.open(changes_path, "wt", encoding="utf-8") as out:
            for n in range(SHARDS):
                current = store.read_shard(n)
                total += len(current)
                updated = {}
                for directory_id, record in current.items():
                    labels = set(record.get("labels") or [])
                    if directory_id in seen or ABSENT in labels:
                        continue
                    new = {**record, "labels": sorted(labels | {ABSENT})}
                    for change in diff(record, new, source_id, maker):
                        out.write(json.dumps(change, sort_keys=True) + "\n")
                        changes += 1
                    updated[directory_id] = new
                    absent += 1
                if updated:
                    path = os.path.join(work, f"{n:03d}.jsonl.gz")
                    with open(path, "wb") as f:
                        f.write(encode_records({**current, **updated}))
                    staged[n] = path
        try:
            volume_guard(absent, total)
        except VolumeGuardTripped as exc:
            log({"event": "held", "source": "gb", "publish": f"{date} reconcile", "reason": str(exc)})
            return "held"
        store.write_changes_file(f"reconcile-{date}", changes_path)
        for n in sorted(staged):
            store.write_shard_file(n, staged[n])
    manifest = store.read_manifest()
    manifest["processed"] = (manifest["processed"] + [f"{date} reconcile"])[-100:]
    store.write_manifest(manifest)
    store.write_marker(f"reconciled-{date}", {"absent": absent, "total": total, "changes": changes})
    log({"event": "reconciled", "source": "gb", "publish": date, "absent": absent, "total": total, "changes": changes})
    return "reconciled"


def main() -> int:
    from google.cloud import storage

    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    record = Recorder(COMPONENT, log)  # every decision goes to the decision log too
    try:
        with lease(client.bucket(bucket), "ingest-gb"):
            store = GcsStore(bucket, "gb")
            outcome = run(gateway, store, allowlist=allowlist, log=record)
            if outcome in ("done", "already-done"):
                reconciled = reconcile(store, log=record)
                if reconciled == "held":
                    outcome = "held"
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})  # another run is doing this work
        outcome = "busy"
    except Exception as exc:
        record({"event": "failed", "error": type(exc).__name__})
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
