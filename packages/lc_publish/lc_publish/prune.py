"""Retention for the snapshots bucket and the build state (docs/snapshots-v2.md).

- Keep every day from the last ``retain_days`` days, the latest day, and every earlier day a
  kept day needs to be rebuilt (back through its ``base`` links to its full snapshot).
- Delete every other day folder.
- Delete parts no kept full snapshot uses, once older than ``prune_grace_days``.
- Keep the newest ``state_kept`` days of build state, and the latest day's and its base's.
- If a kept manifest can't be read, or a kept day's chain is broken, delete nothing.

The bucket is versioned: a delete leaves an old version, which the bucket's lifecycle rule
deletes after ``noncurrent_retain_days``.
"""

from __future__ import annotations

import json
from datetime import date as Date
from datetime import datetime, timedelta, timezone

from lc_core.snapshot_format import DATE, OBJECT, FormatError, from_anchor, is_v2

from .upload import STATE_PREFIX


class PruneAborted(Exception):
    """Something kept can't be read, so nothing is deleted."""


def _days(bucket, prefix=""):
    listing = bucket.list_blobs(prefix=prefix, delimiter="/")
    list(listing)  # the prefixes fill in as the listing is read
    return sorted(p[len(prefix):].rstrip("/") for p in listing.prefixes if DATE.match(p[len(prefix):].rstrip("/")))


def plan(bucket, today: str, latest_date: str | None, settings) -> dict:
    """What to keep and delete. Raises PruneAborted."""
    days = _days(bucket)
    cutoff = (Date.fromisoformat(today) - timedelta(days=settings.retain_days)).isoformat()
    cache = {}

    def read(date):
        if date not in cache:
            blob = bucket.blob(f"{date}/manifest.json")
            try:
                cache[date] = json.loads(blob.download_as_bytes()) if blob.exists() else None
            except Exception as exc:
                raise PruneAborted(f"{date}'s manifest can't be read ({type(exc).__name__})") from exc
        return cache[date]

    keep = {d for d in days if d >= cutoff}
    if latest_date:
        keep.add(latest_date)
    for day in sorted(keep):
        manifest = read(day)
        if manifest is None or not is_v2(manifest):
            continue  # a day without a manifest, or a v1 day, stands alone
        try:
            keep.update(m["date"] for m in from_anchor(read, manifest))
        except FormatError as exc:
            raise PruneAborted(f"{day} can't be traced to its full snapshot ({exc})") from exc
    referenced = set()
    for day in keep:
        manifest = read(day)
        if manifest and is_v2(manifest) and manifest["kind"] == "full":
            referenced.update(p["object"] for p in manifest["parts"])
    return {"keep": sorted(keep), "delete_days": [d for d in days if d not in keep], "referenced": referenced}


def prune(bucket, data_bucket, today: str, latest: dict | None, settings, now=None, log=print) -> dict:
    """Apply the plan (or only report it, with ``prune_dry_run``). Returns counts."""
    now = now or datetime.now(timezone.utc)
    latest_date = (latest or {}).get("date")
    decided = plan(bucket, today, latest_date, settings)
    dry = settings.prune_dry_run
    result = {"days_kept": len(decided["keep"]), "days_deleted": 0, "parts_deleted": 0, "parts_kept": 0,
              "bytes_deleted": 0, "states_deleted": 0, "dry_run": dry}
    for day in decided["delete_days"]:
        for blob in bucket.list_blobs(prefix=f"{day}/"):
            result["bytes_deleted"] += blob.size or 0
            if not dry:
                blob.delete()
        result["days_deleted"] += 1
    grace = timedelta(days=settings.prune_grace_days)
    for blob in bucket.list_blobs(prefix="parts/"):
        if not OBJECT.match(blob.name) or blob.name in decided["referenced"]:
            result["parts_kept"] += 1
            continue
        if blob.time_created and now - blob.time_created < grace:
            result["parts_kept"] += 1  # perhaps a build still writing its manifest
            continue
        result["parts_deleted"] += 1
        result["bytes_deleted"] += blob.size or 0
        if not dry:
            blob.delete()
    states = _days(data_bucket, f"{STATE_PREFIX}/")
    keep_states = set(states[-settings.state_kept:])
    if latest_date:
        keep_states.add(latest_date)
        blob = bucket.blob(f"{latest_date}/manifest.json")
        base = (json.loads(blob.download_as_bytes()).get("base") or {}) if blob.exists() else {}
        if base.get("date"):
            keep_states.add(base["date"])
    for day in states:
        if day in keep_states:
            continue
        for blob in data_bucket.list_blobs(prefix=f"{STATE_PREFIX}/{day}/"):
            if not dry:
                blob.delete()
        result["states_deleted"] += 1
    log({"event": "pruned", **result})
    return result
