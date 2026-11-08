"""The decision log, for the private /log page: sealed days, and today's runs.

A sealed day is ``chains/<day>.jsonl`` plus its seal ``chains/<day>.json``. The
page verifies the chain from the seal's previous head before showing it. A day
the snapshot job hasn't sealed yet is shown from its run objects
(``runs/<day>/``), each verified on its own, and marked unsealed.

Reads go through the site's own identity, which may only view the bucket.
Results are cached briefly, so reloading the page doesn't list the bucket each time.
"""

from __future__ import annotations

import json
import re
import time

from lc_core.events import GENESIS, verify

DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_EVENTS = 2000
MAX_DAYS = 90
FIELDS = ("ts", "actor", "action", "subject", "decision", "reason", "trigger", "run_id", "refs", "hash", "prev")


class DecisionLogReader:
    def __init__(self, bucket, ttl: float = 60.0, clock=time.monotonic):
        self._bucket, self._ttl, self._clock, self._cache = bucket, ttl, clock, {}

    def _cached(self, key, fn):
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < self._ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (self._clock(), value)
        return value

    def days(self) -> dict:
        return self._cached("days", self._days)

    def _days(self) -> dict:
        seals = {}
        for blob in self._bucket.list_blobs(prefix="chains/"):
            if blob.name.endswith(".json"):
                seal = json.loads(blob.download_as_bytes())
                seals[seal["day"]] = seal
        logged = set()
        listing = self._bucket.list_blobs(prefix="runs/", delimiter="/")
        list(listing)  # the prefixes fill in as the listing is read
        for prefix in listing.prefixes:
            day = prefix.rstrip("/").split("/")[-1]
            if DAY.match(day):
                logged.add(day)
        out = []
        for day in sorted(set(seals) | logged, reverse=True)[:MAX_DAYS]:
            seal = seals.get(day)
            out.append({"day": day, "sealed": seal is not None, "events": seal["events"] if seal else None,
                        "head": seal["head"] if seal else None})
        return {"days": out}

    def day(self, day: str):
        if not DAY.match(day or ""):
            return None
        return self._cached(f"day:{day}", lambda: self._day(day))

    def _day(self, day: str) -> dict:
        seal_blob = self._bucket.blob(f"chains/{day}.json")
        if seal_blob.exists():
            seal = json.loads(seal_blob.download_as_bytes())
            lines = []
            if seal["events"]:
                lines = self._bucket.blob(f"chains/{day}.jsonl").download_as_text().splitlines()
            ok, count, head = verify(lines, start=seal.get("previousHead") or GENESIS)
            verified = ok and count == seal["events"] and head == seal["head"]
            events = [json.loads(line) for line in lines if line.strip()]
            return {"day": day, "sealed": True, "verified": verified, "seal": seal,
                    "events": [_public(e) for e in events[:MAX_EVENTS]], "truncated": len(events) > MAX_EVENTS}
        events, verified = [], True
        for blob in self._bucket.list_blobs(prefix=f"runs/{day}/"):
            lines = blob.download_as_text().splitlines()
            ok, _, _ = verify(lines)  # each run object is chained on its own until the day is sealed
            verified = verified and ok
            events += [json.loads(line) for line in lines if line.strip()]
        events.sort(key=lambda e: e["ts"])
        return {"day": day, "sealed": False, "verified": verified, "seal": None,
                "events": [_public(e) for e in events[:MAX_EVENTS]], "truncated": len(events) > MAX_EVENTS}


def _public(event: dict) -> dict:
    return {k: event.get(k) for k in FIELDS}
