"""The system view: what is running, what ran, and what the owner and the guards decided.

Read-only. Every source of information is optional: a missing permission or an
API error shows as "unavailable" for that part, never as a broken page, and
never as a false "all good".
"""

from __future__ import annotations

import json
import threading
import time

CACHE_SECONDS = 30
DECISIONS_SHOWN = 20
SOURCE_STORES = {"gleif": "GLEIF entities", "gleif-rr": "GLEIF relationships", "gb": "Companies House"}
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
    def __init__(self, reader, clock=time.time):
        self._reader, self._clock = reader, clock
        self._cached, self._at = None, -1e9
        self._lock = threading.Lock()

    def collect(self, snapshot_meta: dict = None) -> dict:
        with self._lock:
            if self._cached is None or self._clock() - self._at > CACHE_SECONDS:
                self._cached, self._at = self._collect(), self._clock()
            data = dict(self._cached)
        data["snapshot"] = snapshot_meta or {}
        return data

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
        stores = {}
        for prefix, label in SOURCE_STORES.items():
            manifest, error = _try(lambda p=prefix: r.bucket_json("data", f"{p}/manifest.json"))
            stores[prefix] = {"label": label, "records": (manifest or {}).get("records"),
                              "last": (manifest or {}).get("lastKey") or ((manifest or {}).get("processed") or [None])[-1],
                              "error": error}
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
                          "iap": bool(s.get("iapEnabled"))} for s in services],
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
            "decisions": decisions,
            "decisionsError": decisions_error,
            "agents": [{"role": role, "status": IDLE, "note": "not running: agents are off until the owner switches them on"}
                       for role in AGENT_ROLES],
        }


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
        "gleif": store("gleif"), "gb": store("gb"),
        "gateway": services.get("conduct-gateway", UNKNOWN), "egress": jobs.get("egress-check", UNKNOWN),
        "ingest-gleif": jobs.get("ingest-gleif", UNKNOWN), "ingest-gb": jobs.get("ingest-gb", UNKNOWN),
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
