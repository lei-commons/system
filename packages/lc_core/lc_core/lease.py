"""One run at a time: a lease held as an object in Cloud Storage.

Taking the lease creates the object only if it doesn't exist (generation 0),
which Cloud Storage does atomically, so two runs can't both take it.

While a run holds the lease it renews it (a heartbeat) every `heartbeat_seconds`.
A lease whose heartbeat is older than `stale_seconds` belongs to a run that is no
longer running (crashed, killed, timed out) and is taken over at once, instead of
waiting out the whole time to live. Leases written before heartbeats existed carry
none, and fall back to `ttl_seconds` from when they were taken. Taking over is done
with a generation check, so two runs can't both take over the same lease.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from contextlib import contextmanager

# Renew the lease this often while the run holds it.
HEARTBEAT_SECONDS = 60
# A lease not renewed for this long is free: ten missed heartbeats.
STALE_SECONDS = 600
# Leases without a heartbeat (written by older code) are free after this long.
TTL_SECONDS = 3 * 3600


class Busy(Exception):
    """Another run holds the lease."""


def _is_stale(current: dict, now: float, stale_seconds: int, ttl_seconds: int) -> bool:
    if "beat" in current:
        return now - float(current["beat"]) >= stale_seconds
    return now - float(current.get("since", 0)) >= ttl_seconds


@contextmanager
def lease(bucket, name: str, ttl_seconds: int = TTL_SECONDS, clock=time.time,
          heartbeat_seconds: float = HEARTBEAT_SECONDS, stale_seconds: int = STALE_SECONDS):
    from google.api_core.exceptions import NotFound, PreconditionFailed

    who = os.environ.get("CLOUD_RUN_EXECUTION") or socket.gethostname()
    since = clock()

    def body() -> str:
        return json.dumps({"holder": who, "since": since, "beat": clock()})

    blob = bucket.blob(f"leases/{name}")
    try:
        blob.upload_from_string(body(), if_generation_match=0)
    except PreconditionFailed:
        try:
            blob.reload()
            current = json.loads(blob.download_as_bytes(if_generation_match=blob.generation))
        except (NotFound, PreconditionFailed):
            raise Busy(name) from None
        if not _is_stale(current, clock(), stale_seconds, ttl_seconds):
            raise Busy(f"{name} is held by {current.get('holder')}") from None
        try:  # a run that is no longer running: take its lease over, unless someone else just did
            blob.upload_from_string(body(), if_generation_match=blob.generation)
        except PreconditionFailed:
            raise Busy(name) from None
    blob.reload()
    held = {"generation": blob.generation}
    lock = threading.Lock()
    stop = threading.Event()

    def beat():
        while not stop.wait(heartbeat_seconds):
            with lock:
                if held["generation"] is None:
                    return
                renewed = bucket.blob(f"leases/{name}")
                try:
                    renewed.upload_from_string(body(), if_generation_match=held["generation"])
                except PreconditionFailed:  # someone took it over: it's no longer ours
                    held["generation"] = None
                    return
                except Exception:  # a passing storage error: try again at the next beat
                    continue
                if renewed.generation is None:
                    renewed.reload()
                held["generation"] = renewed.generation

    beater = threading.Thread(target=beat, name=f"lease-{name}", daemon=True)
    if heartbeat_seconds and heartbeat_seconds > 0:
        beater.start()
    try:
        yield
    finally:
        stop.set()
        if beater.is_alive():
            beater.join()
        with lock:
            generation = held["generation"]
        if generation is not None:
            try:
                bucket.blob(f"leases/{name}").delete(if_generation_match=generation)  # only our own lease
            except (NotFound, PreconditionFailed):
                pass
