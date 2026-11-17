"""Copy a built day to the private snapshots bucket, its state to the data bucket, and point latest.json at it.

Order matters, so a reader never sees a pointer to something half-copied:
parts (content-addressed, each stored once) -> the day's change files -> NOTICE -> manifest
-> tomorrow's state -> latest.json. The bucket enforces public access prevention, so
nothing copied here can be made public by mistake.
"""

from __future__ import annotations

import json
from pathlib import Path

from lc_core.snapshot_format import ENTITY_KIND, RELATIONSHIP_KIND, SCHEMA_VERSION

STATE_PREFIX = "publish/state"
DAY_EXTRAS = ("manifest.json", "NOTICE.txt")


class UploadError(Exception):
    """An object already in the bucket isn't what this build expects."""


def upload_day(bucket, out: Path, manifest: dict) -> dict:
    """Upload a built day. Returns {uploaded, reused, bytes}."""
    from google.api_core.exceptions import PreconditionFailed

    date = manifest["date"]
    uploaded = reused = sent = 0
    for f in manifest["files"]:
        if f["kind"] not in (ENTITY_KIND, RELATIONSHIP_KIND):
            continue
        existing = bucket.get_blob(f["object"])
        if existing is not None:  # the same content is already stored: share it
            if existing.size != f["bytes"]:
                raise UploadError(f"{f['object']} is stored with another size")
            reused += 1
            continue
        try:
            bucket.blob(f["object"]).upload_from_filename(str(out / "parts" / f["name"]), if_generation_match=0)
        except PreconditionFailed:  # stored meanwhile, by the same content's name
            reused += 1
            continue
        uploaded += 1
        sent += f["bytes"]
    keep = set(DAY_EXTRAS)
    for f in manifest["files"]:
        if f["object"].startswith(f"{date}/"):
            bucket.blob(f["object"]).upload_from_filename(str(out / "day" / f["name"]))
            keep.add(f["name"])
            uploaded += 1
            sent += f["bytes"]
    bucket.blob(f"{date}/NOTICE.txt").upload_from_filename(str(out / "NOTICE.txt"))
    bucket.blob(f"{date}/manifest.json").upload_from_filename(str(out / "manifest.json"), content_type="application/json")
    # A rebuild of the same day: drop files the new manifest doesn't list, and the old signature.
    for blob in bucket.list_blobs(prefix=f"{date}/"):
        if blob.name.split("/", 1)[1] not in keep:
            blob.delete()
    return {"uploaded": uploaded, "reused": reused, "bytes": sent}


def upload_state(data_bucket, out: Path, date: str) -> None:
    """Tomorrow's state, so tomorrow's build can find what changed. state.json goes last."""
    state = out / "state"
    meta = json.loads((state / "state.json").read_text(encoding="utf-8"))
    for name in meta["files"]:
        data_bucket.blob(f"{STATE_PREFIX}/{date}/{name}").upload_from_filename(str(state / name))
    data_bucket.blob(f"{STATE_PREFIX}/{date}/state.json").upload_from_filename(str(state / "state.json"),
                                                                               content_type="application/json")


def point_latest(bucket, manifest: dict) -> dict:
    latest = {"schemaVersion": SCHEMA_VERSION, "date": manifest["date"], "prefix": manifest["date"],
              "kind": manifest["kind"], "anchor": manifest["anchor"], "snapshotRoot": manifest["snapshotRoot"],
              "manifest": f"{manifest['date']}/manifest.json"}
    # Written last, so readers never see a pointer to a half-copied snapshot.
    bucket.blob("latest.json").upload_from_string(json.dumps(latest, indent=2), content_type="application/json")
    return latest
