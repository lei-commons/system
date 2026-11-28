"""The daily check-in, in the cloud (owner, 2026-11-28: "Check-in, any self-recovery, etc all should run on the
cloud itself"; one email, "absolutely the FINAL summary rather than any intermediate status", sent "when entire
daily cycle is fully completed", covering the cloud flows and the GitHub flows, and run some time after the regular
jobs so it "will never run in the middle" of them).

Three ways in, one email a day:

- ``--stage cycle`` (the cycle's step after all its work, also when the snapshot or the load failed;
  ``--execution``, ``--ended``, ``--failed``, ``--stopped``, ``--recovery`` describe it): decides on recovery and
  may start one run of the cycle. No email.
- ``--stage final`` (the cycle's last step, config/cycle.yaml final_after_minutes, 2 hours, after its work ended;
  same arguments): the day's one ``checkin-report``: the cycle and any recovery run, the trials, and the GitHub
  workflows as they stand (the day's release and ledger syncs if they have run, the previous night's verify). A
  log-based alert emails it to the owner (infra/terraform/checkin.tf). Quiet if the day was already summarised,
  or if this run's check-in started a recovery run: that run's own final check-in reports the day.
- ``--stage watchdog`` (late morning): a day with no cycle, or one that stopped without its check-in, gets a run;
  the day before gets its summary if it never went out. No email of its own beyond that late summary.

Read-only, apart from starting a run of the cycle (a decision, so it is in the decision log too).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from lc_core import cycle as cycle_config
from lc_core.decisions import Recorder, log_bucket_name

from checkin import report

COMPONENT = "job:checkin"
GITHUB_LOG = "lc-github-runs"  # where the scheduled GitHub workflows report their outcomes
WORKFLOW = "daily-cycle"
JOB_EVENTS = ("done", "already-done", "failed", "held", "source-unavailable", "bad-download", "download-recovered",
              "download-failed", "gleif-rekeyed", "mapping-upgrade", "quality", "trial-invalid", "trial-invalidated",
              "built", "timings", "log-sealed", "log-unsealed", "dedi-published", "dedi-failed", "loaded",
              "incremental-failed", "dedi-check-ok", "dedi-check-failed", "fallback-used", "busy", "halted",
              "recovery-started", "recovery-not-started", "checkin-cycle")
WORKFLOW_EVENTS = ("source-failed", "trial-failed", "source-recovered-by-browser", "browser-fallback-failed",
                   "dedi-check-failed", "web-claims-failed", "checkin-failed")
# Only these fields of an event are kept: counts, states, names and error types, never a message.
KEEP = ("event", "source", "publish", "job", "round", "records", "total", "new_records", "existing_changed", "changes",
        "error", "refused", "status", "kind", "why", "date", "added", "changed", "deleted", "relationships", "mode",
        "seconds", "day", "events", "days", "quality", "total_s", "outcome", "repaired", "notStarted", "execution",
        "recovery")


def log(record: dict) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def _keep(payload: dict) -> dict:
    out = {k: payload[k] for k in KEEP if k in payload}
    if isinstance(out.get("error"), (dict, list)):  # a Workflows connector error: keep its kind only
        error = out["error"]
        out["error"] = (error.get("tags") or ["error"])[0] if isinstance(error, dict) else "error"
    return out


def _utc(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Cloud:
    """The reads (and the one write: starting a run of the cycle), as the check-in's own identity."""

    def __init__(self, project: str, region: str, buckets: dict):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import storage

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        self.http = AuthorizedSession(credentials)
        self.storage = storage.Client(project=project)
        self.project, self.region, self.buckets = project, region, buckets
        self.workflow = f"https://workflowexecutions.googleapis.com/v1/projects/{project}/locations/{region}/workflows/{WORKFLOW}"

    def executions(self) -> list:
        r = self.http.get(f"{self.workflow}/executions", params={"view": "FULL", "pageSize": 10}, timeout=30)
        r.raise_for_status()
        return r.json().get("executions", [])

    def entries(self, query: str, limit: int = 3000) -> list:
        out, token = [], None
        while len(out) < limit:
            body = {"resourceNames": [f"projects/{self.project}"], "filter": query, "orderBy": "timestamp asc",
                    "pageSize": min(1000, limit - len(out))}
            if token:
                body["pageToken"] = token
            r = self.http.post("https://logging.googleapis.com/v2/entries:list", json=body, timeout=60)
            r.raise_for_status()
            data = r.json()
            out += data.get("entries", [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def bucket_json(self, bucket: str, name: str):
        blob = self.storage.bucket(self.buckets[bucket]).blob(name)
        return json.loads(blob.download_as_bytes()) if blob.exists() else None

    def start_cycle(self, argument: dict) -> str:
        r = self.http.post(f"{self.workflow}/executions", json={"argument": json.dumps(argument)}, timeout=30)
        r.raise_for_status()
        return r.json().get("name", "").rsplit("/", 1)[-1]


def _argument(execution: dict) -> dict:
    try:
        return json.loads(execution.get("argument") or "null") or {}
    except ValueError:
        return {}


def _error(execution: dict) -> dict:
    payload = (execution.get("error") or {}).get("payload") or ""
    try:
        error = json.loads(payload) if payload else {}
    except ValueError:
        error = {}
    return error if isinstance(error, dict) else {}


def _stopped(execution: dict) -> str:
    """A run that ended FAILED or CANCELLED for a reason other than its sources: the step that stopped it."""
    if execution.get("state") not in ("FAILED", "CANCELLED") or _error(execution).get("code") == "sources-failed":
        return ""
    payload = (execution.get("error") or {}).get("payload") or ""
    return next((step for step in ("load-db", "build-snapshot") if step in payload), "the cycle")


def day_runs(executions: list, day: str) -> list:
    """The cycle's runs that started on ``day`` (UTC), oldest first."""
    out = []
    for x in sorted(executions, key=lambda x: x.get("startTime") or ""):
        if (x.get("startTime") or "")[:10] != day:
            continue
        arg = _argument(x)
        out.append({"name": x.get("name", "").rsplit("/", 1)[-1], "start": x.get("startTime"), "end": x.get("endTime"),
                    "state": x.get("state"), "stopped": _stopped(x), "recovery": bool(arg.get("recovery")),
                    "startedByCheckin": arg.get("started_by") == "checkin", "why": arg.get("why"),
                    "failed": _error(x).get("failed", []) if _error(x).get("code") == "sources-failed" else []})
    return out


class Facts:
    def __init__(self, cloud, now: datetime):
        self.cloud, self.now, self.unavailable = cloud, now, []

    def attempt(self, name, fn, default=None):
        try:
            return fn()
        except Exception as exc:  # named in the report, never a false "all good"
            self.unavailable.append(f"{name} ({type(exc).__name__})")
            return default


def _events_since(f: Facts, start: str, until: str) -> tuple:
    window = f'timestamp>="{start}" AND timestamp<"{until}"'
    jobs = f.attempt("the jobs' events", lambda: f.cloud.entries(
        f'resource.type="cloud_run_job" AND {window} AND ('
        + " OR ".join(f'jsonPayload.event="{e}"' for e in JOB_EVENTS) + ")"), []) or []
    grouped = {}
    for e in jobs:
        name = ((e.get("resource") or {}).get("labels") or {}).get("job_name", "")
        grouped.setdefault(name, []).append(_keep(e.get("jsonPayload") or {}) | {"ts": e.get("timestamp", "")})
    flow = f.attempt("the cycle's events", lambda: f.cloud.entries(
        f'resource.type="workflows.googleapis.com/Workflow" AND resource.labels.workflow_id="{WORKFLOW}" AND {window} AND ('
        + " OR ".join(f'jsonPayload.event="{e}"' for e in WORKFLOW_EVENTS) + ")"), []) or []
    return grouped, [_keep(e.get("jsonPayload") or {}) | {"ts": e.get("timestamp", "")} for e in flow]


def collect_day(cloud, day: str, now: datetime, sources: list, settings: dict, me: dict = None, executions=None) -> dict:
    """Everything the final summary needs about ``day``. ``me``: the run calling the final check-in (still running
    while it waits for it), with how it ended."""
    f = Facts(cloud, now)
    if executions is None:
        executions = f.attempt("the cycle's runs", cloud.executions, []) or []
    runs = day_runs(executions, day)
    for r in runs:
        if me and r["name"] == me["name"]:
            r.update(end=me["ended"] or r.get("end"), stopped=me["stopped"], failed=me["failed"],
                     state="FAILED" if me["stopped"] or me["failed"] else "SUCCEEDED")
    following = day_runs(executions, (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d"))
    start = runs[0]["start"] if runs else f"{day}T00:00:00Z"
    until = following[0]["start"] if following else _utc(now)  # the day's events end where the next cycle begins
    job_events, flow_events = _events_since(f, start, until)
    facts = {"day": day, "now": _utc(now), "runs": runs, "jobEvents": job_events, "workflowEvents": flow_events,
             "cycle": runs[0] if runs else None, "failed": runs[-1]["failed"] if runs else [],
             "sources": [s for s in sources if s.get("stage") == "production"]}
    facts["recoveryNotStarted"] = [e for e in job_events.get("checkin", []) if e.get("event") == "recovery-not-started"]
    trials = []
    for p in sources:
        if p.get("stage") != "trial":
            continue
        events = job_events.get(p["job"], [])
        rep = f.attempt(f"{p['id']}'s trial report", lambda b=p["id"]: cloud.bucket_json("data", f"trials/{b}/trial-report.json"))
        trials.append({"id": p["id"], "job": p["job"],
                       "quality": next((e for e in reversed(events) if e.get("event") == "quality"), None),
                       "failedToday": any(e.get("event") == "trial-failed" and e.get("job") == p["job"] for e in flow_events)
                       or any(e.get("event") == "failed" for e in events),
                       "error": next((e.get("error") for e in reversed(events) if e.get("event") == "failed"), None),
                       # the report's sample stays in the bucket: only its summary counts come here
                       "report": {k: rep.get(k) for k in ("records", "statusUnmapped", "anomalies", "mainShapeShare")} if rep else None,
                       "validity": f.attempt(f"{p['id']}'s validity", lambda b=p["id"]: cloud.bucket_json("data", f"trials/{b}/validity.json"))})
    facts["trials"] = trials
    built = next((e for e in reversed(job_events.get("build-snapshot", [])) if e.get("event") == "built"), None)
    if built and built.get("date"):
        manifest = f.attempt("the snapshot's manifest", lambda: cloud.bucket_json("snapshots", f"{built['date']}/manifest.json"))
        if manifest:
            facts["unmappedStatus"] = {str(e["value"]): e["records"] for e in (manifest.get("stats") or {}).get("unmappedStatus", [])}
    runs_gh = f.attempt("the GitHub workflows' reports", lambda: cloud.entries(
        # from midday the day before: the previous night's verify (GitHub often starts it hours late)
        f'logName="projects/{cloud.project}/logs/{GITHUB_LOG}" AND timestamp>="{_utc(datetime.fromisoformat(day) - timedelta(hours=12))}"'), []) or []
    facts["githubRuns"] = [{k: (e.get("jsonPayload") or {}).get(k) for k in ("repo", "workflow", "conclusion", "run", "date", "detail")}
                           | {"ts": e.get("timestamp")} for e in runs_gh]
    nat = f.attempt("Cloud NAT's log", lambda: cloud.entries(
        f'resource.type="nat_gateway" AND jsonPayload.allocation_status="DROPPED" AND timestamp>="{_utc(now - timedelta(hours=24))}"', 1000))
    facts["nat"] = {"dropped": len(nat)} if nat is not None else {}
    facts["control"] = f.attempt("the control state", lambda: cloud.bucket_json("control", "state.json")) or {}
    facts["unavailable"] = f.unavailable
    return facts


def recover(cloud, facts: dict, settings: dict, now: datetime, record) -> dict:
    """Start one run of the cycle if the day needs it and the limits allow; record the decision either way."""
    plan = report.plan_recovery(facts, settings, now)
    if plan.get("start"):
        argument = {"started_by": "checkin", "recovery": bool(plan.get("recovery")), "why": plan["why"]}
        try:
            plan["execution"] = cloud.start_cycle(argument)
            record({"event": "recovery-started", "reason": plan["why"], "why": plan["why"], "execution": plan["execution"],
                    "recovery": argument["recovery"]})
        except Exception as exc:
            plan.update(start=False, notStarted=f"starting it failed ({type(exc).__name__})")
    if plan.get("why") and not plan.get("start"):
        record({"event": "recovery-not-started", "why": plan["why"], "notStarted": plan["notStarted"]})
    return plan


def stage_cycle(cloud, args, now, settings, record) -> dict:
    """The cycle's last step: recovery only (the final check-in reports the day)."""
    f = Facts(cloud, now)
    day = now.strftime("%Y-%m-%d")
    runs = day_runs(f.attempt("the cycle's runs", cloud.executions, []) or [], day)
    me = next((r for r in runs if r["name"] == args.execution), None) or {"name": args.execution}
    me.update(stopped=args.stopped or "", recovery=args.recovery == "true", failed=json.loads(args.failed or "[]"))
    facts = {"runs": runs or [me], "cycle": me, "recoveriesToday": sum(1 for r in runs if r["startedByCheckin"]),
             "control": f.attempt("the control state", lambda: cloud.bucket_json("control", "state.json")) or {}}
    plan = recover(cloud, facts, settings, now, record)
    entry = {"event": "checkin-cycle", "day": day, "execution": args.execution, "stopped": me["stopped"],
             "failed": me["failed"], "recovery": plan, "unavailable": f.unavailable}
    record(entry)
    return entry


def stage_watchdog(cloud, now, sources, settings, record) -> dict:
    """Late morning: a day with no cycle, or one that stopped without its own check-in, gets a run; and the day
    before, if its summary never went out (its cycle broke before its final check-in), gets it now."""
    f = Facts(cloud, now)
    day = now.strftime("%Y-%m-%d")
    executions = f.attempt("the cycle's runs", cloud.executions, []) or []
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if not f.unavailable and not any(r["state"] == "ACTIVE" for r in day_runs(executions, yesterday)):
        stage_final(cloud, now, sources, settings, record, day=yesterday, late=True, executions=executions)
    runs = day_runs(executions, day)
    checked = f.attempt("today's check-ins", lambda: cloud.entries(
        f'resource.type="cloud_run_job" AND resource.labels.job_name="checkin" AND jsonPayload.event="checkin-cycle" AND timestamp>="{day}T00:00:00Z"', 5), [])
    if f.unavailable or (runs and (checked or any(r["state"] == "ACTIVE" for r in runs) or not runs[-1]["stopped"])):
        entry = {"event": "checkin-watchdog", "day": day, "action": "none", "unavailable": f.unavailable}
        record(entry)
        return entry
    facts = {"runs": runs, "cycle": runs[-1] if runs else None, "recoveriesToday": sum(1 for r in runs if r["startedByCheckin"]),
             "control": f.attempt("the control state", lambda: cloud.bucket_json("control", "state.json")) or {}}
    plan = recover(cloud, facts, settings, now, record)
    entry = {"event": "checkin-watchdog", "day": day, "action": "recovery" if plan.get("start") else "none", "recovery": plan}
    record(entry)
    return entry


def stage_final(cloud, now, sources, settings, record, args=None, day=None, late=False, executions=None) -> dict:
    """The day's one summary: run by the cycle (``args``) final_after_minutes after all its work finished, or by the
    next day's watchdog (``late``) if it never ran. Quiet if the day was already summarised, or if the check-in
    started a recovery run after this one: that run's own final check-in reports the day."""
    f = Facts(cloud, now)
    if executions is None:
        executions = f.attempt("the cycle's runs", cloud.executions, []) or []
    me = None
    if args is not None and args.execution:
        me = {"name": args.execution, "ended": args.ended, "stopped": args.stopped or "",
              "failed": json.loads(args.failed or "[]")}
        mine = next((x for x in executions if x.get("name", "").endswith("/" + args.execution)), None)
        day = ((mine or {}).get("startTime") or args.ended or _utc(now))[:10]
    day = day or now.strftime("%Y-%m-%d")
    sent = f.attempt("the day's reports", lambda: cloud.entries(
        f'resource.type="cloud_run_job" AND resource.labels.job_name="checkin" AND jsonPayload.event="checkin-report" '
        f'AND jsonPayload.day="{day}" AND timestamp>="{day}T00:00:00Z"', 5), [])
    runs = day_runs(executions, day)
    later = [r for r in runs if me and r["startedByCheckin"] and r["name"] != me["name"]
             and (r["start"] or "") > next((x["start"] or "" for x in runs if x["name"] == me["name"]), "")]
    if sent or later or (late and f.unavailable):
        reason = ("the day's summary was sent" if sent else "a recovery run reports the day" if later
                  else "couldn't tell whether the day's summary was sent")
        entry = {"event": "checkin-quiet", "day": day, "reason": reason}
        record(entry)
        return entry
    facts = collect_day(cloud, day, now, sources, settings, me=me, executions=executions)
    facts["late"] = late
    judged = report.judge(facts, settings)
    lines = report.summary(facts, judged)
    entry = {"event": "checkin-report", "day": day, "verdict": judged["verdict"], **lines,
             "details": {k: judged[k] for k in ("sources", "trials", "github", "timings")}
             | {"runs": facts["runs"], "unmappedStatus": facts.get("unmappedStatus"), "nat": facts.get("nat"),
                "late": late, "unavailable": facts.get("unavailable")}}
    record(entry)
    return entry


def parse(argv=None):
    ap = argparse.ArgumentParser(description="The daily check-in")
    ap.add_argument("--stage", choices=("cycle", "watchdog", "final"), default="cycle")
    ap.add_argument("--execution", default="")
    ap.add_argument("--ended", default="", help="when the calling run's work ended (ISO time)")
    ap.add_argument("--failed", default="[]")
    ap.add_argument("--stopped", default="")
    ap.add_argument("--recovery", default="false")
    return ap.parse_args(argv)


def run(cloud, args, now: datetime, sources: list, settings: dict, record=log) -> dict:
    if args.stage == "final":
        return stage_final(cloud, now, sources, settings, record, args=args)
    if args.stage == "watchdog":
        return stage_watchdog(cloud, now, sources, settings, record)
    return stage_cycle(cloud, args, now, settings, record)


def main(argv=None) -> int:
    from google.cloud import storage
    from lc_core.sources import pipelines

    args = parse(argv)
    s = cycle_config.load()
    settings = {**s["checkin"], "timeouts": s["timeout_minutes"], "dedi_check_utc": s["dedi_check_utc"]}
    if not settings["enabled"]:
        log({"event": "checkin-disabled"})
        return 0
    bucket = os.environ["LC_DATA_BUCKET"]
    cloud = Cloud(os.environ["LC_PROJECT"], os.environ["LC_REGION"],
                  {"data": bucket, "snapshots": os.environ["LC_SNAPSHOT_BUCKET"],
                   "control": os.environ["LC_CONTROL_STATE_URI"].split("/")[2]})
    record = Recorder(COMPONENT, log)
    try:
        run(cloud, args, datetime.now(timezone.utc), pipelines(), settings, record=record)
    finally:
        record.write(storage.Client().bucket(log_bucket_name(bucket)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
