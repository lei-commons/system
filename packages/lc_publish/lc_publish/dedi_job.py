"""The DeDi steps of the daily cycle, and the check that watches them (docs/dedi.md).

    python -m lc_publish.dedi_job publish          # build, verify, write and re-verify the DeDi set
    python -m lc_publish.dedi_job check [--repair]  # verify the published set independently

``publish`` runs at the end of build-snapshot (lc_publish.job) and never fails the snapshot: the previous
set stays valid until its next_update (36 hours). ``check`` runs as the job ``dedi-check``: as a step of
the daily cycle after load-db, and again by itself in the afternoon. It writes dedi/check.json (the System
page's card shows it) and records dedi-check-ok or dedi-check-failed in the decision log. With --repair, a
failing check republishes from the latest snapshot and checks again; this also re-issues the set on a
day when the cycle itself failed, so the files don't go stale. A check that still fails exits non-zero,
so the job fails and its alert emails the owner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from lc_core.approvals import effective, load_decisions, registry
from lc_core.control import SystemHalted, require_running
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.lease import Busy, lease

from . import dedi

COMPONENT = "job:dedi"
CHECK_OBJECT = dedi.PREFIX + "check.json"
MIN_HOURS_LEFT = 12  # a set that goes stale within this many hours counts as failing


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def publish_after_build(snapshots, record, cfg=None, signer=None, now=None, **publish_options) -> dict | None:
    """Called by build-snapshot once the day is published. Records what happened; never raises."""
    try:
        cfg = cfg or dedi.load_config()
        if not cfg.enabled:
            record({"event": "dedi-disabled"})
            return None
        signer = signer or dedi.make_signer(cfg, record=record)
        sources = registry()
        return dedi.publish(snapshots, cfg, signer, sources=sources, effective=effective(sources, load_decisions()),
                            now=now, record=record, **publish_options)
    except Exception as exc:  # dedi.publish already recorded dedi-failed; anything earlier is recorded here
        if not isinstance(exc, dedi.PublishError):
            record({"event": "dedi-failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        return None


def run_check(snapshots, record, *, repair: bool, cfg=None, signer=None, now=None, **publish_options) -> dict:
    cfg = cfg or dedi.load_config()
    clock = now or (lambda: datetime.now(timezone.utc))
    report = dedi.check(snapshots, cfg, now=clock(), min_hours_left=MIN_HOURS_LEFT)
    repaired = False
    if not report["ok"] and repair and cfg.enabled:
        record({"event": "dedi-check-failed", "problems": report["problems"][:10], "repairing": True})
        publish_after_build(snapshots, record, cfg=cfg, signer=signer, now=now, **publish_options)
        report = dedi.check(snapshots, cfg, now=clock(), min_hours_left=MIN_HOURS_LEFT)
        repaired = report["ok"]
    report["repaired"] = repaired
    try:
        snapshots.blob(CHECK_OBJECT).upload_from_string(json.dumps(report, indent=2, sort_keys=True),
                                                        content_type="application/json")
    except Exception as exc:
        record({"event": "dedi-check-unsaved", "error": type(exc).__name__})
    record({"event": "dedi-check-ok" if report["ok"] else "dedi-check-failed", "repaired": repaired,
            "problems": report["problems"][:10]})
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="lei-commons DeDi publication")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("publish")
    c = sub.add_parser("check")
    c.add_argument("--repair", action="store_true", help="republish if the check fails, then check again")
    args = ap.parse_args(argv)
    try:
        require_running("job:dedi")
    except SystemHalted as exc:
        log({"event": "halted", "reason": str(exc)})
        return 0
    from google.cloud import storage

    client = storage.Client()
    data = client.bucket(os.environ["LC_DATA_BUCKET"])
    snapshots = client.bucket(os.environ["LC_SNAPSHOT_BUCKET"])
    decisions = client.bucket(log_bucket_name(os.environ["LC_DATA_BUCKET"]))
    record = Recorder(COMPONENT, log)
    try:
        with lease(data, "dedi"):  # one publication at a time (build-snapshot takes the same lease for its step)
            if args.command == "publish":
                ok = publish_after_build(snapshots, record) is not None
            else:
                ok = run_check(snapshots, record, repair=args.repair)["ok"]
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        ok = True
    except Exception as exc:
        record(failure(exc))
        ok = False
    record.write(decisions)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
