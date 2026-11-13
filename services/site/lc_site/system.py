"""The system view: what is running, what ran, and what the owner and the guards decided.

Read-only. Every source of information is optional: a missing permission or an
API error shows as "unavailable" for that part, never as a broken page, and
never as a false "all good".
"""

from __future__ import annotations

import json
import os
import threading
import time

from lc_core.actions import catalogue, source_catalogue
from lc_core.approvals import effective as effective_approvals

CACHE_SECONDS = 30
DECISIONS_SHOWN = 20
FEED_SHOWN = 14
AFTER_INGEST = ["build-snapshot", "load-db"]  # the cycle's last steps, after every source's ingestion
AGENT_ROLES = ["engineering"]  # agents/roles; none runs until the owner switches agents on

OK, BAD, BUSY, IDLE, UNKNOWN = "ok", "failed", "running", "idle", "unknown"


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

    def __init__(self, reader, clock=time.time, sources=None):
        from lc_core.sources import pipelines

        self._reader, self._clock = reader, clock
        self._sources = sources if sources is not None else pipelines(os.environ.get("LC_SOURCES_CONFIG")
                                                                         or _default_sources_path())
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
        services, _ = _try(lambda: r.api(f"https://run.googleapis.com/v2/projects/{r.project}/locations/{r.region}/services")
                           .get("services", []), [])
        console = next((s.get("uri") for s in services if s["name"].rsplit("/", 1)[1] == "owner-console"), None)
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
        return {"total": len(rows), **counts, "review": review, "sources": rows, "console": console,
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
            "/executions?pageSize=14").get("executions", []), [])
        control, control_error = _try(lambda: r.bucket_json("control", "state.json"))

        def store(prefix, label):
            manifest, error = _try(lambda: r.bucket_json("data", f"{prefix}/manifest.json"))
            return {"label": label, "records": (manifest or {}).get("records"),
                    "last": (manifest or {}).get("lastKey") or ((manifest or {}).get("processed") or [None])[-1],
                    "error": error}

        job_rows = {j["name"].rsplit("/", 1)[1]: j for j in jobs}
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
            sources.append({k: src.get(k) for k in ("id", "name", "country", "licence", "cadence", "label", "refresh",
                                                     "job", "store", "records_as")}
                           | {"records": entities["records"], "last": entities["last"], "error": entities["error"],
                              "relationships": (relationships or {}).get("records"),
                              "jobStatus": _execution_status(row) if row else UNKNOWN,
                              "lastRun": latest.get("createTime"), "finished": latest.get("completionTime")})
        steps = [src["job"] for src in self._sources] + AFTER_INGEST
        feed, feed_error = _try(lambda: self._feed(r), [])
        names, decisions_error = _try(lambda: sorted(r.bucket_names("decision_log", "owner-control/")
                                                     + r.bucket_names("decision_log", "budget-guard/"))[-DECISIONS_SHOWN:], [])
        decisions = []
        for name in reversed(names):
            event, _ = _try(lambda n=name: r.bucket_json("decision_log", n))
            if event:
                decisions.append({k: event.get(k) for k in ("ts", "actor", "action", "decision", "reason", "amount", "component")})

        return {
            "collectedAt": self._clock(),
            "control": {"state": (control or {}).get("state"), "pausedComponents": (control or {}).get("paused_components", []),
                        "updatedBy": (control or {}).get("updated_by"), "updatedAt": (control or {}).get("updated_at"),
                        "error": control_error},
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
            "cycles": [{"started": c.get("startTime"), "ended": c.get("endTime"), "state": c.get("state"),
                        "status": {"SUCCEEDED": OK, "FAILED": BAD, "CANCELLED": BAD, "ACTIVE": BUSY}.get(c.get("state"), UNKNOWN)}
                       for c in cycles],
            "cleanStreak": clean_streak(cycles),
            "cyclesError": cycles_error,
            "stores": stores,
            "sources": sources,
            # Owner actions are taken inline on the site's pages (lc_site.actions); the console offers them too.
            "console": next((s.get("uri") for s in services if s["name"].rsplit("/", 1)[1] == "owner-console"), None),
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


def clean_streak(executions: list) -> int:
    """Days in a row, newest first, whose daily cycle succeeded. A run still going doesn't break it."""
    days = {}
    for e in executions:
        day = (e.get("startTime") or "")[:10]
        if day and e.get("state") != "ACTIVE":
            days.setdefault(day, []).append(e.get("state"))
    streak = 0
    for day in sorted(days, reverse=True):
        if all(state == "SUCCEEDED" for state in days[day]):
            streak += 1
        else:
            break
    return streak


COLOURS = {OK: "#d7f5dd", BAD: "#fbd5d5", BUSY: "#d6e4ff", IDLE: "#eeeeee", UNKNOWN: "#f5f0d0"}


def node_statuses(data: dict) -> dict:
    jobs = {j["name"]: j["status"] for j in data["jobs"]}
    services = {s["name"]: s["status"] for s in data["services"]}
    state = data["control"]["state"]
    control = OK if state == "running" else (IDLE if state in ("paused", "manual") else (BAD if state == "stopped" else UNKNOWN))

    def store(prefix):
        return OK if data["stores"][prefix]["records"] else UNKNOWN

    return {
        "gleif": store("gleif"), "gb": store("gb"), "br": store("br"), "us-ny": store("us-ny"), "sg": store("sg"),
        "gateway": services.get("conduct-gateway", UNKNOWN), "egress": jobs.get("egress-check", UNKNOWN),
        "ingest-gleif": jobs.get("ingest-gleif", UNKNOWN), "ingest-gb": jobs.get("ingest-gb", UNKNOWN),
        "ingest-br": jobs.get("ingest-br", UNKNOWN), "ingest-ny": jobs.get("ingest-ny", UNKNOWN),
        "ingest-sg": jobs.get("ingest-sg", UNKNOWN),
        "data": OK if any(s["records"] for s in data["stores"].values()) else UNKNOWN,
        "snapshot": OK if data["snapshot"].get("date") else UNKNOWN,
        "site": services.get("lei-commons-site", UNKNOWN), "console": services.get("owner-console", UNKNOWN),
        "guard": services.get("budget-guard", UNKNOWN), "control": control, "agents": IDLE,
    }


# x, y, label, sublabel key
LAYOUT = {
    "gleif": (20, 40, "GLEIF", "source"), "gb": (20, 120, "Companies House", "source"),
    "gateway": (230, 80, "Conduct gateway", "only way out"), "egress": (230, 170, "Egress check", "proves the seal"),
    "ingest-gleif": (440, 40, "GLEIF ingestion", "daily 02:00 UTC"), "ingest-gb": (440, 120, "UK ingestion", "daily 03:00 UTC"),
    "data": (650, 80, "Data bucket", "records and ledger"), "snapshot": (860, 40, "Daily snapshot", "06:00 UTC, signed"),
    "site": (860, 120, "Site and API", "private"),
    "control": (230, 270, "Control state", "owner only"), "console": (20, 270, "Owner console", "two factors"),
    "guard": (440, 270, "Budget guard", "pauses at the cap"), "agents": (650, 270, "Agents", "off"),
}
EDGES = [("gleif", "gateway"), ("gb", "gateway"), ("gateway", "ingest-gleif"), ("gateway", "ingest-gb"),
         ("ingest-gleif", "data"), ("ingest-gb", "data"), ("data", "snapshot"), ("snapshot", "site"),
         ("egress", "gateway"), ("console", "control"), ("guard", "control"), ("control", "agents")]
W, H = 180, 46


def diagram(data: dict, esc) -> str:
    status = node_statuses(data)
    parts = []
    for a, b in EDGES:
        (ax, ay, *_), (bx, by, *_) = LAYOUT[a], LAYOUT[b]
        parts.append(f"<line x1={ax + W / 2:.0f} y1={ay + H / 2:.0f} x2={bx + W / 2:.0f} y2={by + H / 2:.0f} stroke=\"#bbb\"/>")
    for key, (x, y, label, sub) in LAYOUT.items():
        s = status.get(key, UNKNOWN)
        parts.append(f"<rect x={x} y={y} width={W} height={H} rx=6 fill=\"{COLOURS[s]}\" stroke=\"#888\"/>"
                     f"<text x={x + 10} y={y + 19}>{esc(label)}</text>"
                     f"<text x={x + 10} y={y + 36} fill=\"#555\">{esc(sub)} · {esc(s)}</text>")
    return (f"<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"100%\" viewBox=\"0 0 1060 340\" role=img "
            f"aria-label=\"System diagram\">{''.join(parts)}</svg>")
