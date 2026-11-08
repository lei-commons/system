"""One run at a time: a lease held as an object in Cloud Storage.

Taking the lease creates the object only if it doesn't exist (generation 0),
which Cloud Storage does atomically, so two runs can't both take it. A lease
older than its time to live belongs to a run that crashed, and is taken over
with a generation check, so two runs can't both take over the same one either.
"""

from __future__ import annotations

import json
import os
import socket
import time
from contextlib import contextmanager


class Busy(Exception):
    """Another run holds the lease."""


@contextmanager
def lease(bucket, name: str, ttl_seconds: int = 3 * 3600, clock=time.time):
    from google.api_core.exceptions import NotFound, PreconditionFailed

    blob = bucket.blob(f"leases/{name}")
    holder = json.dumps({"holder": os.environ.get("CLOUD_RUN_EXECUTION") or socket.gethostname(), "since": clock()})
    try:
        blob.upload_from_string(holder, if_generation_match=0)
    except PreconditionFailed:
        try:
            blob.reload()
            current = json.loads(blob.download_as_bytes(if_generation_match=blob.generation))
        except (NotFound, PreconditionFailed):
            raise Busy(name) from None
        if clock() - float(current.get("since", 0)) < ttl_seconds:
            raise Busy(f"{name} is held by {current.get('holder')}") from None
        try:  # a crashed run's lease: take it over, unless someone else just did
            blob.upload_from_string(holder, if_generation_match=blob.generation)
        except PreconditionFailed:
            raise Busy(name) from None
    blob.reload()
    generation = blob.generation
    try:
        yield
    finally:
        try:
            blob.delete(if_generation_match=generation)  # only our own lease
        except (NotFound, PreconditionFailed):
            pass
