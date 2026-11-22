"""What a source trial leaves in the data bucket, and its removal (owner, 2026-11-22: once a trial is done, its
leftover data and resources are cleaned up within 24 hours, 48 at most; docs/source-trials.md).

A trial keeps only small reports under ``trials/<source>/``. They are removed when the trial ends:
- promoted: the source's job deletes them, every version, at the start of its first production run (the daily
  cycle runs it within 24 hours);
- dropped from the registry: deploy-ingest-bulk.yml runs the job once with LC_CLEANUP=1, which deletes them,
  then deletes the job.
While a trial is live, each run rewrites its reports (``refresh``), so the bucket's safety-net lifecycle rule
(infra/terraform/storage.tf: trials/ objects older than 2 days, old versions after 1 day) never touches a live
trial and only catches what the two paths above missed.
"""

from __future__ import annotations

import re

PREFIX = "trials"
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,63}")


def prefix(source: str) -> str:
    """``trials/<source>/``; refuses anything that isn't a plain source id, so nothing else can ever be touched."""
    if not SOURCE_ID.fullmatch(source or ""):
        raise ValueError(f"not a source id: {source!r}")
    return f"{PREFIX}/{source}/"


def remove(bucket, source: str) -> int:
    """Delete every object and every old version under trials/<source>/. Returns how many were deleted."""
    deleted = 0
    for blob in bucket.client.list_blobs(bucket, prefix=prefix(source), versions=True):
        bucket.delete_blob(blob.name, generation=blob.generation)
        deleted += 1
    return deleted


def refresh(bucket, source: str) -> int:
    """Rewrite each live object under trials/<source>/ (a few kilobytes), so a live trial's reports are always
    less than a day old. The old version becomes noncurrent and the lifecycle deletes it after a day."""
    rewritten = 0
    for blob in bucket.client.list_blobs(bucket, prefix=prefix(source)):
        data = blob.download_as_bytes()
        bucket.blob(blob.name).upload_from_string(data, content_type=blob.content_type or "application/json")
        rewritten += 1
    return rewritten
