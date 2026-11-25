"""The web-claims job: the web-asserted facts pilot, measure-only (owner approval 2026-11-23; lei-commons/docs
04-designs/web-asserted-facts.md; config/web_claims.yaml).

Each run (the daily cycle's last step):
1. stops unless the system is running (the owner's control state), and takes the job's lease;
2. picks the candidates once, from the published snapshot, and keeps the list for the pilot;
3. goes on where the last run stopped, for at most run.minutes, a few records at a time (the gateway still asks
   each Common Crawl host one request at a time, at most one a second);
4. saves its progress, the claims and the report under trials/web-claims/ in the data bucket, rewriting each
   object every run (so the trials/ lifecycle net never takes a live pilot's files), and logs counts only.

LC_CLEANUP=1 deletes trials/web-claims/ in every version (the pilot ended: deploy-web-claims.yml runs it once,
then deletes the job). Nothing of the pilot reaches the snapshot, the database, the site or the releases.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from lc_core.control import SystemHalted, require_running
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.fetch import Denied, Gateway
from lc_core.lease import Busy, lease

from . import candidates as cand
from . import commoncrawl, report
from .models import ClaudeMaker, GeminiChecker, ModelError, Usage
from .process import Context, process
from .settings import load as load_settings

COMPONENT = "job:web-claims"
PILOT = "web-claims"  # its folder: trials/web-claims/
CANDIDATES, PROGRESS, CLAIMS, REPORT = "candidates.json.gz", "progress.json.gz", "claims.jsonl.gz", "report.json"
CHECKPOINT_SECONDS = 900
MAX_FETCH_ERRORS_IN_A_ROW = 25


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


class Store:
    """trials/web-claims/ in the data bucket."""

    def __init__(self, bucket):
        self.bucket = bucket

    def read(self, name):
        blob = self.bucket.blob(f"trials/{PILOT}/{name}")
        return blob.download_as_bytes() if blob.exists() else None

    def write(self, name, data: bytes, content_type: str):
        self.bucket.blob(f"trials/{PILOT}/{name}").upload_from_string(data, content_type=content_type)


class Snapshots:
    """The published snapshots bucket, read-only."""

    def __init__(self, bucket):
        self.bucket = bucket

    def read_json(self, name):
        blob = self.bucket.blob(name)
        return json.loads(blob.download_as_bytes()) if blob.exists() else None

    def download(self, name, local):
        self.bucket.blob(name).download_to_filename(local)


def _gz_json(store, name):
    data = store.read(name)
    return json.loads(gzip.decompress(data)) if data else None


def save(store, cands: dict, progress: dict, rep: dict) -> None:
    store.write(CANDIDATES, gzip.compress(json.dumps(cands, sort_keys=True).encode()), "application/gzip")
    store.write(PROGRESS, gzip.compress(json.dumps(progress, sort_keys=True).encode()), "application/gzip")
    claims = "".join(json.dumps(c, sort_keys=True, ensure_ascii=False) + "\n"
                     for r in progress["results"].values() for c in r.get("claims", []))
    store.write(CLAIMS, gzip.compress(claims.encode()), "application/gzip")
    store.write(REPORT, json.dumps(rep, indent=1, sort_keys=True, ensure_ascii=False).encode(), "application/json")


def _usage_total(prior: dict, usage: Usage) -> dict:
    total = {"calls": prior.get("calls", 0) + usage.calls, "tokensIn": prior.get("tokensIn", 0) + usage.tokens_in,
             "tokensOut": prior.get("tokensOut", 0) + usage.tokens_out,
             "usd": round(prior.get("usd", 0.0) + usage.usd, 4), "byModel": dict(prior.get("byModel") or {})}
    for model, m in usage.by_model.items():
        t = total["byModel"].setdefault(model, {"calls": 0, "tokensIn": 0, "tokensOut": 0})
        for k in t:
            t[k] += m[k]
    return total


def run(store, snapshots, gateway, s, *, maker=None, checker=None, gate=lambda: None, log=log,
        clock=time.monotonic, code_version="", today=None) -> str:
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "reason": str(exc)})
        return "halted"
    if not s.enabled:
        log({"event": "web-claims-disabled"})
        return "disabled"
    cands = _gz_json(store, CANDIDATES)
    if cands is None:
        try:
            cands = cand.select(snapshots, s)
        except cand.NotReady as exc:
            log({"event": "web-claims-waiting", "reason": str(exc)})
            return "waiting"
        cands["selectedOn"] = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log({"event": "web-claims-candidates", "snapshot": cands["date"],
             "withWebsite": sum(r["set"] == cand.WITH for r in cands["records"]),
             "withoutWebsite": sum(r["set"] == cand.WITHOUT for r in cands["records"]),
             "withLei": sum(bool(r.get("lei")) for r in cands["records"]),
             "withGleifParent": sum(bool(r.get("truthParents")) for r in cands["records"])})
    progress = _gz_json(store, PROGRESS) or {"results": {}, "usage": {}, "runs": 0}
    results = progress["results"]
    pending = [r for r in cands["records"] if r["id"] not in results]
    meta = {"pilot": PILOT, "snapshot": cands["date"], "selectedOn": cands.get("selectedOn"), "codeVersion": code_version,
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not pending:
        rep = report.build(cands["records"], results, s, progress.get("usage") or {}, meta)
        save(store, cands, progress, rep)  # rewritten, so the lifecycle net never takes a live pilot's files
        log({"event": "web-claims-already-done", "processed": len(results)})
        log(report.public(rep))
        return "already-done"

    crawls = commoncrawl.crawls(gateway, s.crawls)
    usage, prior = Usage(), progress.get("usage") or {}
    prior_usd = float(prior.get("usd", 0.0))

    def allow_models():
        return usage.usd < s.max_model_usd_per_run and prior_usd + usage.usd < s.max_model_usd_total

    ctx = Context(gateway=gateway, crawls=crawls, settings=s, maker=maker, checker=checker, usage=usage,
                  allow_models=allow_models)
    started = clock()
    deadline = started + s.run_minutes * 60
    log({"event": "web-claims-started", "pending": len(pending), "processed": len(results), "crawls": len(crawls),
         "models": bool(maker and checker)})
    counts = {"done": 0, "modelErrors": 0, "fetchErrors": 0, "denied": 0}
    last_errors, stop, model_row, fetch_row, denied_row = {}, None, 0, 0, 0
    next_checkpoint = started + CHECKPOINT_SECONDS
    todo = iter(pending)
    with ThreadPoolExecutor(max_workers=s.workers) as pool:
        inflight = {}
        while True:
            while stop is None and len(inflight) < 2 * s.workers and clock() < deadline:
                record = next(todo, None)
                if record is None:
                    break
                inflight[pool.submit(process, ctx, record)] = record
            if not inflight:
                break
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for future in finished:
                record = inflight.pop(future)
                try:
                    results[record["id"]] = future.result()
                    counts["done"] += 1
                    model_row = fetch_row = denied_row = 0
                except ModelError as exc:
                    counts["modelErrors"] += 1
                    model_row += 1
                    last_errors["model"] = exc.loggable
                    if model_row >= s.max_model_errors_in_a_row:
                        stop = stop or "model-errors"
                except Denied as exc:
                    counts["denied"] += 1
                    denied_row += 1
                    last_errors["denied"] = exc.rule
                    if denied_row >= 5:
                        stop = stop or "denied"
                except Exception as exc:  # the archive or the gateway: the record stays pending for the next run
                    counts["fetchErrors"] += 1
                    fetch_row += 1
                    last_errors["fetch"] = type(exc).__name__
                    if fetch_row >= MAX_FETCH_ERRORS_IN_A_ROW:
                        stop = stop or "fetch-errors"
            if stop is None and clock() >= deadline:
                stop = "time"
            if stop is None and maker is not None and not allow_models():
                stop = "model-budget"
            if clock() >= next_checkpoint:
                next_checkpoint = clock() + CHECKPOINT_SECONDS
                progress["usage"] = _usage_total(prior, usage)
                save(store, cands, progress, report.build(cands["records"], results, s, progress["usage"], meta))
                log({"event": "web-claims-progress", "processed": len(results), **counts})
                try:
                    gate()
                except SystemHalted:
                    stop = stop or "halted"
    progress["usage"] = _usage_total(prior, usage)
    progress["runs"] = progress.get("runs", 0) + 1
    rep = report.build(cands["records"], results, s, progress["usage"], meta)
    save(store, cands, progress, rep)
    remaining = len(cands["records"]) - len(results)
    outcome = "done" if remaining == 0 else (stop if stop in ("model-errors", "fetch-errors", "denied", "halted") else "progress")
    log({"event": "web-claims-run", "outcome": outcome, "stoppedBy": stop or "all-done", "seconds": round(clock() - started),
         "remaining": remaining, "modelCalls": usage.calls, "modelUsd": round(usage.usd, 2), **counts,
         **{f"lastError{k.capitalize()}": v for k, v in last_errors.items()}})
    log(report.public(rep))
    return outcome


def main() -> int:
    from google.cloud import storage

    client = storage.Client()
    data = client.bucket(os.environ["LC_DATA_BUCKET"])
    record = Recorder(COMPONENT, log)
    if os.environ.get("LC_CLEANUP") == "1":  # the pilot ended: its files go, every version
        from lc_ingest import trials

        removed = trials.remove(data, PILOT)
        record({"event": "trial-cleaned", "source": PILOT, "objects": removed, "why": "the pilot ended"})
        record.write(client.bucket(log_bucket_name(data.name)))
        return 0
    project = os.environ["LC_PROJECT_ID"]
    outcome = "failed"
    try:
        s = load_settings()
        gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
        maker = ClaudeMaker(s.maker, project)
        checker = GeminiChecker(s.checker, project)
        with lease(data, "web-claims"):
            outcome = run(Store(data), Snapshots(client.bucket(os.environ["LC_SNAPSHOTS_BUCKET"])), gateway, s,
                          maker=maker, checker=checker, gate=lambda: require_running(COMPONENT), log=record,
                          code_version=os.environ.get("LC_CODE_VERSION", ""))
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        record(failure(exc))
        raise
    finally:
        record.write(client.bucket(log_bucket_name(data.name)))
    # A handled stop (model errors, fetch errors, a refusal) is reported by its events and the next status, not by a
    # failed execution: the failed-job alert is kept for crashes.
    return 0 if outcome != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
