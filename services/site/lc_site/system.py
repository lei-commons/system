"""The system view: what is running, what ran, and what the owner and the guards decided.

Read-only. Every source of information is optional: a missing permission or an
API error shows as "unavailable" for that part, never as a broken page, and
never as a false "all good".
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from lc_core.actions import catalogue, source_catalogue
from lc_core.approvals import effective as effective_approvals
from lc_core.decisions import public_actor

CACHE_SECONDS = 30
DECISIONS_SHOWN = 20
FEED_SHOWN = 14
AFTER_INGEST = ["build-snapshot", "load-db"]  # the cycle's last steps, after every source's ingestion
AGENT_ROLES = ["engineering"]  # agents/roles; none runs until the owner switches agents on

OK, BAD, BUSY, IDLE, UNKNOWN = "ok", "failed", "running", "idle", "unknown"
DETAILS_SHOWN = 30  # events shown under a source's error details


class GoogleReader:
    """The real reads, as the site's own identity."""

    def __init__(self, project: str, region: str, buckets: dict):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import storage

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self._http = AuthorizedSession(credentials)
        self._storage = storage.Client(project=project)
        self.project, self.region, self.buckets = project, region, buckets

    def api(self, url: str) -> dict:
        response = self._http.get(url, timeout=15)
        response.raise_for_status()
        return response.json()

    def bucket_json(self, bucket: str, name: str):
        blob = self._storage.bucket(self.buckets[bucket]).blob(name)
        return json.loads(blob.download_as_bytes()) if blob.exists() else None

    def bucket_names(self, bucket: str, prefix: str) -> list:
        return [b.name for b in self._storage.list_blobs(self.buckets[bucket], prefix=prefix)]

    def bucket_text(self, bucket: str, name: str):
        blob = self._storage.bucket(self.buckets[bucket]).blob(name)
        return blob.download_as_text() if blob.exists() else None


def _try(fn, default=None):
    try:
        return fn(), None
    except Exception as exc:  # shown as "unavailable", with the reason's type only
        return default, type(exc).__name__


def _execution_status(job: dict) -> str:
    latest = job.get("latestCreatedExecution") or {}
    status = latest.get("completionStatus", "")
    if "RUNNING" in status or (latest and not latest.get("completionTime")):
        return BUSY
    if "SUCCEEDED" in status:
        return OK
    if status:
        return BAD
    return UNKNOWN


class SystemView:
    """Everything the System page shows. Sources come from the registry (config/sources.yaml)."""

    def __init__(self, reader, clock=time.time, sources=None, cycle_settings=None):
        from lc_core.sources import pipelines

        from lc_core import cycle

        self._reader, self._clock = reader, clock
        self._sources = sources if sources is not None else pipelines(os.environ.get("LC_SOURCES_CONFIG")
                                                                         or _default_sources_path())
        # The cycle's settings (config/cycle.yaml): the clean-days gate, the schedule, the retries.
        self._cycle = cycle_settings if cycle_settings is not None else cycle.load()
        self._cached, self._at = None, -1e9
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """Forget the cached view, so the page shows an owner action at once."""
        with self._lock:
            self._cached, self._at = None, -1e9

    def collect(self, snapshot_meta: dict = None) -> dict:
        with self._lock:
            if self._cached is None or self._clock() - self._at > CACHE_SECONDS:
                self._cached, self._at = self._collect(), self._clock()
            data = dict(self._cached)
        data["snapshot"] = snapshot_meta or {}
        return data

    def trials(self) -> dict:
        """The Trials page: every source on trial (config/sources.yaml stage) with its latest quality report
        (trials/<source>/trial-report.json, lc_ingest.quality) and its last failure. A trial keeps no records,
        so nothing of it can reach the snapshot; this is where the owner reads it before promoting it."""
        r = self._reader
        out = []
        for p in self._sources:
            if p.get("stage") != "trial":
                continue
            base = f"trials/{p['id']}"
            report, report_error = _try(lambda b=base: r.bucket_json("data", f"{b}/trial-report.json"))
            manifest, _ = _try(lambda b=base: r.bucket_json("data", f"{b}/manifest.json"))
            health, _ = _try(lambda b=base: r.bucket_json("data", f"{b}/health.json"))
            out.append({"id": p["id"], "label": p["label"], "name": p.get("name"), "country": p.get("country"),
                        "job": p["job"], "store": p["store"], "licence": p.get("licence"),
                        "measured": len((manifest or {}).get("processed") or []), "lastKey": (manifest or {}).get("lastKey"),
                        "health": health or None, "report": report or None, "reportError": report_error})
        return {"trials": out, "promotion": "docs/source-trials.md"}

    def sources_page(self) -> dict:
        """The Data sources page: every source in the registry with the owner's edits, its terms, decisions and actions."""
        from lc_core import source_edits
        from lc_core.approvals import registry

        r = self._reader
        base = registry(os.environ.get("LC_SOURCES_CONFIG") or _default_sources_path())
        edits, edits_error = _try(lambda: r.bucket_json("control", source_edits.EDITS_NAME) or {}, {})
        if not edits_error and not isinstance(edits, dict):
            edits, edits_error = {}, "malformed"
        entries = source_edits.merged(base, edits if not edits_error else {})
        decisions, decisions_error = _try(lambda: r.bucket_json("control", "source-approvals.json") or {}, {})
        statuses = effective_approvals(entries, decisions if not decisions_error else {})
        if decisions_error:  # can't read the owner's decisions: show nothing as approved, as the jobs do
            statuses = {k: {**v, "status": "unknown"} for k, v in statuses.items()}
        actions = {}
        for a in source_catalogue(entries, statuses) if not decisions_error else []:
            actions.setdefault(a["id"].split(":", 1)[1], []).append(a)
        editable = not edits_error
        rows = []
        for s in entries:
            pipeline = s.get("pipeline") or {}
            records = None
            if pipeline.get("store"):
                manifest, _ = _try(lambda p=pipeline["store"]: r.bucket_json("data", f"{p}/manifest.json"))
                records = (manifest or {}).get("records")
            history = ((decisions or {}).get(s["id"]) or {}).get("history") or []
            status = statuses[s["id"]]["status"]
            remove_blocked = ("In use: deny it instead, so its records leave the snapshot." if pipeline
                              else "Approved: deny it or keep it pending first." if status == "approved"
                              else "Your decisions can't be read right now." if decisions_error else None)
            rows.append({k: s.get(k) for k in ("id", "name", "country", "access", "method", "licence", "cadence", "url",
                                                 "terms_url", "assessment")}
                        | {"built": bool(pipeline), "label": pipeline.get("label"), "job": pipeline.get("job"), "records": records,
                           "pipeline": {k: pipeline.get(k) for k in ("job", "store", "records_as")} if pipeline else None,
                           "approval": statuses[s["id"]], "history": list(reversed(history))[:10],
                           "actions": actions.get(s["id"], []), "owner": s.get("owner") or {},
                           "editable": editable, "removable": editable and not remove_blocked, "removeBlocked": remove_blocked})
        counts = {k: sum(1 for x in rows if x["approval"]["status"] == k) for k in ("approved", "denied", "pending")}
        review = sum(1 for x in rows if x["approval"]["status"] == "pending"
                     or (x["approval"]["status"] == "approved" and not (x["assessment"] or {}).get("verified")))
        changes = [{k: e.get(k) for k in ("at", "by", "source", "change")} | {"fields": sorted((e.get("after") or {}))}
                   for e in reversed((edits or {}).get("history") or [])][:20] if not edits_error else []
        return {"total": len(rows), **counts, "review": review, "sources": rows,
                "decisionsError": decisions_error, "editsError": edits_error, "editable": editable, "changes": changes,
                "limits": {"hint": source_edits.HINT_MAX, "summary": source_edits.SUMMARY_MAX, "name": source_edits.NAME_MAX,
                           "text": source_edits.TEXT_MAX},
                "choices": {"access": source_edits.ACCESS, "method": source_edits.METHOD, "cadence": source_edits.CADENCE,
                            "risk": source_edits.RISK}}

    def _feed(self, r) -> list:
        """The newest decisions from the jobs' run objects, today's and yesterday's, newest first."""
        from datetime import datetime, timedelta, timezone

        today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
        names = []
        for day in (today, today - timedelta(days=1)):
            names += r.bucket_names("decision_log", f"runs/{day.isoformat()}/")
        events = []
        for name in sorted(names, reverse=True)[:FEED_SHOWN]:
            text = r.bucket_text("decision_log", name) or ""
            for line in text.splitlines():
                if line.strip():
                    e = json.loads(line)
                    events.append({k: e.get(k) for k in ("ts", "actor", "action", "subject", "decision", "reason")})
        return sorted(events, key=lambda e: e["ts"] or "", reverse=True)[:FEED_SHOWN]

    def _collect(self) -> dict:
        r = self._reader
        base = f"https://run.googleapis.com/v2/projects/{r.project}/locations/{r.region}"
        jobs, jobs_error = _try(lambda: r.api(f"{base}/jobs").get("jobs", []), [])
        services, services_error = _try(lambda: r.api(f"{base}/services").get("services", []), [])
        schedules, schedules_error = _try(
            lambda: r.api(f"https://cloudscheduler.googleapis.com/v1/projects/{r.project}/locations/{r.region}/jobs").get("jobs", []), [])
        cycles, cycles_error = _try(lambda: r.api(
            f"https://workflowexecutions.googleapis.com/v1/projects/{r.project}/locations/{r.region}/workflows/daily-cycle"
            f"/executions?view=FULL&pageSize={min(100, max(14, 2 * self._cycle['clean_days']))}").get("executions", []), [])
        control, control_error = _try(lambda: r.bucket_json("control", "state.json"))

        def store(prefix, label):
            manifest, error = _try(lambda: r.bucket_json("data", f"{prefix}/manifest.json"))
            return {"label": label, "records": (manifest or {}).get("records"),
                    "last": (manifest or {}).get("lastKey") or ((manifest or {}).get("processed") or [None])[-1],
                    "error": error}

        job_rows = {j["name"].rsplit("/", 1)[1]: j for j in jobs}
        today = datetime.fromtimestamp(self._clock(), timezone.utc).date()
        run_names, runs_error = _try(lambda: [n for day in (today, today - timedelta(days=1))
                                              for n in r.bucket_names("decision_log", f"runs/{day.isoformat()}/")], [])
        stores, sources = {}, []
        for src in self._sources:
            entities = store(src["store"], src["label"])
            stores[src["store"]] = entities
            relationships = None
            if src.get("relationships"):
                relationships = store(src["relationships"], f"{src['label']} relationships")
                stores[src["relationships"]] = relationships
            row = job_rows.get(src["job"]) or {}
            latest = row.get("latestCreatedExecution") or {}
            sources.append({k: src.get(k) for k in ("id", "name", "country", "licence", "cadence", "label", "refresh", "stage",
                                                     "job", "store", "records_as")}
                           | {"records": entities["records"], "last": entities["last"], "error": entities["error"],
                              "relationships": (relationships or {}).get("records"),
                              "jobStatus": _execution_status(row) if row else UNKNOWN,
                              "lastRun": latest.get("createTime"), "finished": latest.get("completionTime"),
                              "health": None if runs_error else source_health(
                                  r, run_names, src["job"], today.isoformat(),
                                  latest.get("completionTime") if row and _execution_status(row) == OK else None)})
        steps = [src["job"] for src in self._sources] + AFTER_INGEST
        judged = judge_cycles(cycles, lambda day, jobs, until: refused_only(r, day, jobs, until))
        feed, feed_error = _try(lambda: self._feed(r), [])
        names, decisions_error = _try(lambda: sorted(r.bucket_names("decision_log", "owner-control/")
                                                     + r.bucket_names("decision_log", "budget-guard/"))[-DECISIONS_SHOWN:], [])
        decisions = []
        for name in reversed(names):
            event, _ = _try(lambda n=name: r.bucket_json("decision_log", n))
            if event:
                decisions.append({k: event.get(k) for k in ("ts", "actor", "action", "decision", "reason", "amount", "component")}
                                 | {"actor": public_actor(event.get("actor"))})

        return {
            "collectedAt": self._clock(),
            "control": {"state": (control or {}).get("state"), "pausedComponents": (control or {}).get("paused_components", []),
                        "updatedBy": (control or {}).get("updated_by"), "updatedAt": (control or {}).get("updated_at"),
                        "monthlyBudgetUsd": (control or {}).get("monthly_budget_usd"), "error": control_error},
            "jobs": [{"name": j["name"].rsplit("/", 1)[1], "status": _execution_status(j),
                      "lastRun": (j.get("latestCreatedExecution") or {}).get("createTime"),
                      "finished": (j.get("latestCreatedExecution") or {}).get("completionTime")} for j in jobs],
            "jobsError": jobs_error,
            "services": [{"name": s["name"].rsplit("/", 1)[1],
                          "status": OK if (s.get("terminalCondition") or {}).get("state") == "CONDITION_SUCCEEDED" else UNKNOWN,
                          "ingress": (s.get("ingress") or "").replace("INGRESS_TRAFFIC_", "").lower(),
                          "iap": bool(s.get("iapEnabled")), "uri": s.get("uri")} for s in services],
            "servicesError": services_error,
            "schedules": [{"name": s["name"].rsplit("/", 1)[1], "schedule": s.get("schedule"), "state": s.get("state"),
                           "lastAttempt": s.get("lastAttemptTime"),
                           "status": BAD if (s.get("status") or {}).get("code") else (OK if s.get("lastAttemptTime") else IDLE)}
                          for s in schedules],
            "schedulesError": schedules_error,
            "cycles": judged,
            "cleanStreak": clean_streak(judged),
            "cycle": {"cleanDays": self._cycle["clean_days"], "scheduleUtc": self._cycle["schedule_utc"],
                      "retries": self._cycle["retries"], "retryWaitMinutes": self._cycle["retry_wait_minutes"]},
            "healthError": runs_error,
            "cyclesError": cycles_error,
            "stores": stores,
            "sources": sources,
            "actions": catalogue(self._sources, control or {}) if control is not None else [],
            "steps": steps,
            "lastCycle": last_cycle(cycles, job_rows, steps),
            "feed": feed,
            "feedError": feed_error,
            "decisions": decisions,
            "decisionsError": decisions_error,
            "agents": [{"role": role, "status": IDLE, "note": "not running: agents are off until the owner switches them on"}
                       for role in AGENT_ROLES],
        }


def _default_sources_path():
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:  # the repository in development; the image sets LC_SOURCES_CONFIG
        candidate = parent / "config" / "sources.yaml"
        if candidate.exists():
            return candidate
    return here.parent / "sources.yaml"


def _job_runs(names: list, job: str) -> list:
    """This job's run objects (runs/<day>/<time>-job-<job>-<execution>.jsonl), oldest first."""
    mine = re.compile(rf"-job-{re.escape(job)}-(?:{re.escape(job)}-[a-z0-9]+|local)\.jsonl$")
    return sorted(n for n in names if mine.search(n))


def _events(reader, name: str) -> list:
    out = []
    for line in (reader.bucket_text("decision_log", name) or "").splitlines():
        if line.strip():
            e = json.loads(line)
            out.append({k: e.get(k) for k in ("ts", "action", "subject", "decision", "reason", "run_id")})
    return out


def source_health(reader, names: list, job: str, today: str, succeeded_at: str = None):
    """How a source's job is doing, from its run objects in the decision log (today's and yesterday's).

    state is "failed" if its newest run failed, else "ok". With a failure: the error, when, and the newest
    failed run's events (attempts, reasons) for the details view. ``failedRunsToday`` counts today's runs
    that failed, so a source that recovered after retries still shows it struggled."""
    runs = _job_runs(names, job)
    if not runs:
        return None
    newest_events = _events(reader, runs[-1])
    todays = [n for n in runs if n.startswith(f"runs/{today}/")]
    failed_today, last_failed = 0, None
    for name in todays:
        events = newest_events if name == runs[-1] else _events(reader, name)
        if any(e["decision"] == "failed" for e in events):
            failed_today += 1
            last_failed = (name, events)
    if last_failed is None and any(e["decision"] == "failed" for e in newest_events):
        last_failed = (runs[-1], newest_events)  # yesterday's newest run failed and nothing ran today
    newest_failed = any(e["decision"] == "failed" for e in newest_events)
    # A later run that succeeded but had nothing to record (no decision-log object) still counts: the job's
    # own latest execution (Cloud Run) is newer than the failure and succeeded.
    failed_at = next((e["ts"] for e in reversed(newest_events) if e["decision"] == "failed"), None)
    if newest_failed and succeeded_at and failed_at and succeeded_at > failed_at:
        newest_failed = False
    health = {"state": BAD if newest_failed else OK, "lastRunAt": (newest_events[-1]["ts"] if newest_events else None),
              "failedRunsToday": failed_today, "runsToday": len(todays)}
    if last_failed:
        name, events = last_failed
        final = next((e for e in reversed(events) if e["action"] in ("ran", "refused") and e["decision"] == "failed"), None) \
            or next(e for e in reversed(events) if e["decision"] == "failed")
        health["problem"] = {"at": final["ts"], "error": final["reason"], "run": final.get("run_id") or name.rsplit("/", 1)[-1],
                             "recovered": not newest_failed, "refused": final["action"] == "refused",
                             "events": events[-DETAILS_SHOWN:]}
    return health


def last_cycle(executions: list, job_rows: dict, steps: list):
    """The newest daily cycle, with each step's run if that run started inside the cycle."""
    if not executions:
        return None
    newest = executions[0]
    start, end = newest.get("startTime") or "", newest.get("endTime") or "9999"
    out = []
    for name in steps:
        latest = (job_rows.get(name) or {}).get("latestCreatedExecution") or {}
        created = latest.get("createTime") or ""
        inside = start <= created <= end
        out.append({"job": name, "started": created if inside else None,
                    "finished": latest.get("completionTime") if inside else None,
                    "status": _execution_status(job_rows[name]) if inside and name in job_rows else UNKNOWN})
    return {"started": newest.get("startTime"), "ended": newest.get("endTime"), "state": newest.get("state"), "steps": out}


def failed_sources(execution: dict):
    """The jobs a cycle names as still failing (it raised code sources-failed), or None for any other end."""
    payload = ((execution.get("error") or {}).get("payload")) or ""
    try:
        error = json.loads(payload) if isinstance(payload, str) else payload
    except ValueError:
        return None
    if isinstance(error, dict) and error.get("code") == "sources-failed" and isinstance(error.get("failed"), list):
        return [str(j) for j in error["failed"]]
    return None


def _stamp(ts: str) -> str:
    """An ISO time as run object names spell it (no colons), for comparing."""
    return (ts or "").replace(":", "")


def refused_only(reader, day: str, jobs: list, until: str) -> bool:
    """Whether every job in ``jobs`` stopped on ``day`` (by ``until``) because its source refused us."""
    names = reader.bucket_names("decision_log", f"runs/{day}/")
    for job in jobs:
        runs = [n for n in _job_runs(names, job) if _stamp(n.rsplit("/", 1)[-1][:len("2026-01-01T000000")]) <= _stamp(until)[:17]]
        if not runs:
            return False
        events = _events(reader, runs[-1])
        final = next((e for e in reversed(events) if e["decision"] == "failed"), None)
        if final is None or final["action"] != "refused":
            return False
    return True


def judge_cycles(executions: list, refused) -> list:
    """Each cycle with its status and whether it counts as clean. Clean: it succeeded, or every source it
    names as failing was refused by that source (owner decision 2026-11-18: an outside refusal isn't a
    fault in our system); such a day is still marked, with the sources."""
    out = []
    for c in executions:
        state = c.get("state")
        row = {"started": c.get("startTime"), "ended": c.get("endTime"), "state": state,
               "status": {"SUCCEEDED": OK, "FAILED": BAD, "CANCELLED": BAD, "ACTIVE": BUSY}.get(state, UNKNOWN),
               "clean": state == "SUCCEEDED", "failedSources": [], "refusedOnly": False}
        jobs = failed_sources(c) if state == "FAILED" else None
        if jobs:
            row["failedSources"] = jobs
            day = (c.get("startTime") or "")[:10]
            try:
                row["refusedOnly"] = bool(day) and refused(day, jobs, c.get("endTime") or "")
            except Exception:
                row["refusedOnly"] = False  # can't tell: not clean, never a false "all good"
            row["clean"] = row["refusedOnly"]
        out.append(row)
    return out


def clean_streak(cycles: list) -> int:
    """Days in a row, newest first, whose daily cycle was clean (``judge_cycles``). A run still going
    doesn't break it."""
    days = {}
    for c in cycles:
        day = (c.get("started") or "")[:10]
        if day and c.get("state") != "ACTIVE":
            days.setdefault(day, []).append(c.get("clean"))
    streak = 0
    for day in sorted(days, reverse=True):
        if all(days[day]):
            streak += 1
        else:
            break
    return streak
