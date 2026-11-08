"""The decision log in Cloud Storage: what each job run decided, sealed into one chain per day.

The log bucket is create-only for every writer: versioned, with a retention
period, so nothing can be appended to, edited or deleted. So the log is built in
two steps:

1. Each job run collects its decisions from its own structured log records (see
   ``Recorder``) and writes them as one new object, ``runs/<day>/<time>-<actor>-<run>.jsonl``.
   The events in the object are chained among themselves.
2. Each day, the snapshot job seals the previous day (``seal_day``). It reads that day's
   run objects, orders every event by time, and chains them all from the last
   sealed day's head. It writes ``chains/<day>.jsonl`` once, and its head goes into
   the snapshot manifest, which the release workflow anchors in Sigstore Rekor.
   Anyone can then check a day with ``lc_core.events.verify``, starting from the previous day's head.

Events hold counts, keys and reasons, never record contents: the log becomes
public with the rest of the system.
"""

from __future__ import annotations

import json
import os
from datetime import date as Date, timedelta
from typing import Callable

from .events import GENESIS, DecisionLog, Event, utcnow, verify

SCALARS = (int, float, str, bool)


def _n(value) -> str:
    """A count with thousands separators: 167,618."""
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def _refs(record: dict, skip=("event", "reason")) -> dict:
    """Scalar fields only: counts, keys, hashes. Nested values are left out."""
    return {k: v for k, v in sorted(record.items()) if k not in skip and isinstance(v, SCALARS) and v is not None}


def _event(actor: str, record: dict) -> Event | None:
    """The decision a structured log record stands for, or None if it isn't one."""
    kind = record.get("event")
    subject = " ".join(str(record[k]) for k in ("source", "publish") if record.get(k)) or actor.split(":", 1)[-1]
    if kind == "done":
        reason = (f"{_n(record.get('changes', 0))} changes ({_n(record.get('new_records', 0))} new, "
                  f"{_n(record.get('existing_changed', 0))} changed); {_n(record.get('total', 0))} records stored")
        return Event(actor, "ingested", subject, "done", reason, refs=_refs(record))
    if kind == "held":
        return Event(actor, "ingested", subject, "held", record.get("reason") or "the volume guard held the update",
                     refs=_refs(record))
    if kind == "already-done":
        return Event(actor, "checked", subject, "skipped", "already ingested", refs=_refs(record))
    if kind == "built":
        reason = f"{_n(record.get('records', 0))} records, {_n(record.get('relationships', 0))} relationships"
        return Event(actor, "built", f"snapshot {record.get('date', '')}".strip(), "done", reason, refs=_refs(record))
    if kind == "loaded":
        reason = f"{_n(record.get('records', 0))} records loaded in {record.get('seconds', 0)} s"
        return Event(actor, "loaded", f"snapshot {record.get('date', '')}".strip(), "done", reason, refs=_refs(record))
    if kind == "already-loaded":
        return Event(actor, "checked", f"snapshot {record.get('date', '')}".strip(), "skipped", "already loaded",
                     refs=_refs(record))
    if kind in ("halted", "busy", "no-snapshot"):
        reasons = {"halted": "the system isn't running", "busy": "another run is doing this work",
                   "no-snapshot": "there is no snapshot yet"}
        detail = record.get("reason")
        return Event(actor, "checked", subject, "skipped", f"{reasons[kind]}: {detail}" if detail else reasons[kind],
                     refs=_refs(record))
    if kind == "reconciled":
        reason = (f"{_n(record.get('absent', 0))} of {_n(record.get('total', 0))} records were in no part of the month's file "
                  "and are labelled absent from the source")
        return Event(actor, "reconciled", subject, "done", reason, refs=_refs(record))
    if kind == "reconcile-skipped":
        return Event(actor, "reconciled", subject, "skipped", record.get("reason") or "skipped", refs=_refs(record))
    if kind == "exported":
        reason = (f"{_n(record.get('changes', 0))} new change files, {_n(record.get('baseline', 0))} baseline shards; "
                  f"{_n(record.get('total_changes', 0))} change files in the ledger")
        return Event(actor, "exported", f"ledger {subject}", "done", reason, refs=_refs(record))
    if kind == "rebuilt":
        ok = not record.get("mismatchedShards")
        reason = (f"all {_n(record.get('shards', 0))} shards rebuild from the ledger ({_n(record.get('records', 0))} records)" if ok
                  else f"{_n(record['mismatchedShards'])} shards don't rebuild from the ledger")
        return Event(actor, "verified", f"ledger {subject}", "done" if ok else "failed", reason, refs=_refs(record))
    if kind == "rebuild-skipped":
        return Event(actor, "verified", f"ledger {subject}", "skipped", record.get("reason") or "skipped", refs=_refs(record))
    if kind == "log-sealed":
        if not record.get("days"):
            return None
        reason = f"{_n(record['days'])} day(s) sealed; {record.get('day')} has {record.get('events', 0)} events"
        return Event(actor, "sealed", "decision log", "done", reason, refs=_refs(record))
    if kind == "log-unsealed":
        return Event(actor, "sealed", "decision log", "failed", f"stopped with {record.get('error', 'an error')}",
                     refs=_refs(record))
    if kind == "failed":
        return Event(actor, "ran", subject, "failed", f"stopped with {record.get('error', 'an error')}", refs=_refs(record))
    if kind == "denied":
        return Event(actor, "fetched", subject, "failed", record.get("reason") or "the gateway refused the request",
                     refs=_refs(record))
    return None


class Recorder:
    """Wraps a job's ``log``: passes every record on, and keeps the ones that are decisions.

    >>> r = Recorder("job:test", log=lambda record: None)
    >>> r({"event": "already-done", "source": "gleif", "publish": "2026-11-08T08:00 full"})
    >>> r({"event": "progress", "shard": 3})
    >>> [(e.action, e.subject, e.decision) for e in r.events]
    [('checked', 'gleif 2026-11-08T08:00 full', 'skipped')]
    """

    def __init__(self, actor: str, log: Callable[[dict], None], run_id: str = "", trigger: str = ""):
        self.actor, self._log, self.events = actor, log, []
        self.run_id = run_id or os.environ.get("CLOUD_RUN_EXECUTION", "")
        self.trigger = trigger or ("cloud-run-job" if self.run_id else "")

    def __call__(self, record: dict) -> None:
        self._log(record)
        event = _event(self.actor, record)
        if event is not None:
            event.trigger, event.run_id = self.trigger, self.run_id
            self.events.append(event)

    def write(self, bucket, workdir: str = "/tmp") -> str | None:
        """Write this run's events as one new object. Never raises: the log must not stop the work."""
        if not self.events:
            return None
        try:
            path = os.path.join(workdir, f"decisions-{os.getpid()}.jsonl")
            if os.path.exists(path):
                os.remove(path)
            chain = DecisionLog(path)
            for event in self.events:
                chain.append(event)
            first = self.events[0].ts
            safe_actor = self.actor.replace(":", "-").replace("/", "-")
            name = f"runs/{first[:10]}/{first.replace(':', '')}-{safe_actor}-{self.run_id or 'local'}.jsonl"
            bucket.blob(name).upload_from_filename(path, if_generation_match=0, content_type="application/x-ndjson")
            os.remove(path)
            self._log({"event": "decisions-written", "object": name, "count": len(self.events)})
            return name
        except Exception as exc:  # recorded in the job's own log instead
            self._log({"event": "decisions-unwritten", "error": type(exc).__name__})
            return None


def _previous_head(bucket, day: str) -> tuple[str, str]:
    """(head, day) of the latest sealed day before ``day``, or (GENESIS, "") if none."""
    sealed = sorted(b.name for b in bucket.list_blobs(prefix="chains/") if b.name.endswith(".json") and b.name < f"chains/{day}")
    if not sealed:
        return GENESIS, ""
    meta = json.loads(bucket.blob(sealed[-1]).download_as_bytes())
    return meta["head"], meta["day"]


def seal_day(bucket, day: str, workdir: str = "/tmp") -> dict:
    """Chain every event of ``day`` (YYYY-MM-DD) after the last sealed day, once.

    Returns the seal: {day, head, events, previousDay, previousHead}. If the day
    is already sealed, returns the existing seal unchanged.
    """
    meta_blob = bucket.blob(f"chains/{day}.json")
    if meta_blob.exists():
        return json.loads(meta_blob.download_as_bytes())
    previous, previous_day = _previous_head(bucket, day)
    events = []
    for blob in sorted(bucket.list_blobs(prefix=f"runs/{day}/"), key=lambda b: b.name):
        for line in blob.download_as_text().splitlines():
            if line.strip():
                d = json.loads(line)
                events.append((d["ts"], blob.name, d))
    events.sort(key=lambda e: (e[0], e[1]))
    path = os.path.join(workdir, f"chain-{day}.jsonl")
    if os.path.exists(path):
        os.remove(path)
    chain = DecisionLog(path)
    chain._head = previous  # the chain carries on from the last sealed day
    for ts, source, d in events:
        refs = dict(d.get("refs") or {}, runObject=source)
        chain.append(Event(d["actor"], d["action"], d["subject"], d["decision"], d["reason"], trigger=d.get("trigger", ""),
                           run_id=d.get("run_id", ""), refs=refs, ts=ts))
    ok, count, head = True, 0, previous  # a day without events carries the head over
    if events:
        with open(path, encoding="utf-8") as f:
            ok, count, head = verify(f, start=previous)
        if not ok:
            raise RuntimeError(f"the sealed chain does not verify: {head}")
        bucket.blob(f"chains/{day}.jsonl").upload_from_filename(path, if_generation_match=0,
                                                                content_type="application/x-ndjson")
        os.remove(path)
    seal = {"day": day, "head": head, "events": count, "previousDay": previous_day, "previousHead": previous,
            "sealedAt": utcnow()}
    meta_blob.upload_from_string(json.dumps(seal, sort_keys=True), if_generation_match=0, content_type="application/json")
    return seal


def seal_through(bucket, last_day: str, workdir: str = "/tmp") -> list:
    """Seal every unsealed day up to ``last_day``, oldest first, so no day is skipped
    even when the snapshot didn't run (for example while the system was paused)."""
    sealed = sorted(b.name[len("chains/"):-len(".json")] for b in bucket.list_blobs(prefix="chains/") if b.name.endswith(".json"))
    if sealed:
        day = Date.fromisoformat(sealed[-1]) + timedelta(days=1)
    else:
        logged = sorted(p.rstrip("/").split("/")[-1] for p in _prefixes(bucket, "runs/"))
        if not logged:
            return []
        day = Date.fromisoformat(logged[0])
    seals = []
    while day.isoformat() <= last_day:
        seals.append(seal_day(bucket, day.isoformat(), workdir))
        day += timedelta(days=1)
    return seals


def _prefixes(bucket, prefix: str) -> list:
    blobs = bucket.list_blobs(prefix=prefix, delimiter="/")
    list(blobs)  # the prefixes are filled in as the listing is read
    return list(blobs.prefixes)


def yesterday(today: str) -> str:
    return (Date.fromisoformat(today) - timedelta(days=1)).isoformat()


def log_bucket_name(data_bucket: str) -> str:
    """The log bucket from the data bucket's name: both are ``<project>-<purpose>``."""
    explicit = os.environ.get("LC_DECISION_LOG_BUCKET")
    if explicit:
        return explicit
    if not data_bucket.endswith("-data"):
        raise ValueError(f"can't derive the decision log bucket from {data_bucket!r}")
    return data_bucket[: -len("-data")] + "-decision-log"
