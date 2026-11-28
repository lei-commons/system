"""The daily check-in's judgement and report: pure functions over what the cloud recorded.

``judge`` turns the facts
(the day's cycle runs, their events, the trials, the GitHub workflows' outcomes, the network) into a verdict, the
issues, what recovered on its own, the recovery to start and what needs the owner. ``summary`` renders the short
lines of the owner's one email a day.

Everything here is counts, states, job and source names and error types: never a record, never an error's
message (it could quote a value), so the report is safe for the logs the session reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
OK, ATTENTION, FAILED = "ok", "attention", "failed"
LABEL_MAX = 900  # each emailed line stays under the log-based alert's label limit
OUTSIDE = ("refused", "unavailable")  # the source's doing, not ours (owner decision 2026-11-18)
SEVERITY = {OK: 0, ATTENTION: 1, FAILED: 2}


def ist(ts: str) -> str:
    """'2026-11-28T02:00:12Z' -> '07:30 IST' (times to the owner are in IST)."""
    if not ts:
        return "?"
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST).strftime("%H:%M IST")


def minutes(start: str, end: str):
    if not start or not end:
        return None
    a, b = (datetime.fromisoformat(t.replace("Z", "+00:00")) for t in (start, end))
    return round((b - a).total_seconds() / 60)


def _n(v) -> str:
    return f"{v:,}" if isinstance(v, int) else str(v)


def _span(m) -> str:
    return "?" if m is None else f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _last(events: list, name: str):
    found = [e for e in events if e.get("event") == name]
    return found[-1] if found else None


def _worse(a: str, b: str) -> str:
    return a if SEVERITY[a] >= SEVERITY[b] else b


# ---- the parts of the day ---------------------------------------------------------------------------------------

def source_outcomes(facts: dict) -> list:
    """One row per production source: ok / recovered / failed / unavailable / refused, with the error type."""
    failed_jobs = set(facts.get("failed") or [])
    retried = {}
    for e in facts.get("workflowEvents", []):
        if e.get("event") == "source-failed":
            retried[e.get("job")] = retried.get(e.get("job"), 0) + 1
    rows = []
    for src in facts.get("sources", []):
        job = src["job"]
        events = facts.get("jobEvents", {}).get(job, [])
        fail = _last(events, "failed")
        row = {"id": src["id"], "job": job, "label": src.get("label") or src["id"], "tries": retried.get(job, 0) + 1}
        done = _last(events, "done")
        if done:
            row.update(new=done.get("new_records", 0), changed=done.get("existing_changed", 0), total=done.get("total"))
        if _last(events, "source-unavailable"):
            row["state"] = "unavailable"  # the site's maintenance notice while this month's file is in (no retries)
        elif job in failed_jobs:
            row["state"] = "refused" if fail and fail.get("refused") else "failed"
            row["error"] = (fail or {}).get("error") or "unknown"
            if fail and fail.get("status"):
                row["status"] = fail["status"]
        elif row["tries"] > 1:
            row.update(state="recovered", error=(fail or {}).get("error") or "an earlier try failed")
        else:
            row["state"] = "ok"
        for flag in ("held", "gleif-rekeyed", "mapping-upgrade", "bad-download", "download-failed"):
            if _last(events, flag):
                row.setdefault("flags", []).append(flag)
        rows.append(row)
    return rows


def trial_outcomes(facts: dict) -> list:
    """Each trial: measured, failed or invalid, and whether the automatic part of the promotion checklist passes
    (docs/source-trials.md items 1, 4, 5 and 6; items 2 and 3, a plausible count and the sample, are a reader's)."""
    rows = []
    for t in facts.get("trials", []):
        row = {"id": t["id"], "job": t["job"]}
        validity = t.get("validity") or {}
        quality = (t.get("quality") or {}).get("quality") or t.get("report") or {}
        if validity.get("state") == "invalid":
            row["state"] = "invalid"
        elif t.get("failedToday"):
            row.update(state="failed", error=t.get("error") or "unknown", failedDays=validity.get("days", 0))
        elif quality.get("records") is not None:
            unmapped, anomalies = quality.get("statusUnmapped"), sum((quality.get("anomalies") or {}).values())
            shape = quality.get("mainShapeShare")
            checks = {"measured": True, "statusUnmapped": unmapped == 0, "anomalies": anomalies == 0,
                      "oneNumberShape": isinstance(shape, (int, float)) and shape >= 95}
            row.update(state="measured", records=quality.get("records"), unmapped=unmapped, anomalies=anomalies,
                       mainShapeShare=shape, checks=checks, ready=all(checks.values()), new=bool(t.get("quality")))
        else:
            row["state"] = "waiting"
        rows.append(row)
    return rows


def github_outcomes(facts: dict, settings: dict) -> list:
    """Each expected GitHub workflow as it stands when the final check-in runs (GitHub's timer runs them hours
    after the cycle, often late, so they aren't waited for):

    - snapshot (the day's release): released, failed so far (GitHub tries again later), or not yet run;
    - verify: the newest verify, normally the previous night's, of the day before;
    - sync (a ledger): synced since the cycle ended, failed so far, or not yet run;
    - anything else (the network check): its newest run.
    """
    runs, rows = facts.get("githubRuns", []), []
    day = facts.get("day")
    cycle_end = max((r.get("end") or "" for r in facts.get("runs") or []), default="")
    for expected in settings.get("github", []):
        repo, workflow = expected.split(" ", 1)
        mine = [r for r in runs if r.get("repo") == repo and r.get("workflow") == workflow]
        if workflow == "snapshot":
            mine = [r for r in mine if r.get("date") == day and r.get("detail") != "not-built-yet"]
        elif workflow == "sync":
            mine = [r for r in mine if (r.get("ts") or "") > (cycle_end or "9")]
        elif mine:
            mine = mine[-1:]
        ok = [r for r in mine if r.get("conclusion") == "success"]
        failures = len(mine) - len(ok)
        row = {"repo": repo, "workflow": workflow, "runs": len(mine), "failures": failures}
        if ok:
            row.update(state="recovered" if failures else "ok", at=ok[-1].get("ts"),
                       detail=ok[-1].get("detail") if workflow != "verify" else f"{ok[-1].get('date')} verified")
        elif mine:
            row.update(state="failed", conclusion=mine[-1].get("conclusion"), at=mine[-1].get("ts"),
                       detail=f"{mine[-1].get('date')}" if workflow == "verify" else None)
        else:
            row["state"] = "not yet run"
        rows.append(row)
    return rows


def plan_recovery(facts: dict, settings: dict, now: datetime) -> dict:
    """Whether the check-in starts a run of the cycle now, and why (or why not). Used at the cycle's end and by
    the watchdog; the final check-in only reports what happened."""
    rec = settings.get("recovery") or {}
    cycle = facts.get("cycle") or {}
    missing = not facts.get("runs")
    stopped = cycle.get("stopped") or ""
    if not (missing or stopped):
        return {"start": False}
    why = "no cycle ran today" if missing else f"{stopped} stopped the cycle"
    latest = rec.get("latest_start_utc") or "00:00"
    reasons = []
    if not rec.get("enabled"):
        reasons.append("recovery is off (config/cycle.yaml)")
    if (facts.get("control") or {}).get("state") not in (None, "running"):
        reasons.append(f"the system is {facts['control']['state']}")
    if cycle.get("recovery"):
        reasons.append("this was already a recovery run")
    if facts.get("recoveriesToday", 0) >= rec.get("max_runs_per_day", 0):
        reasons.append(f"{facts.get('recoveriesToday', 0)} recovery run(s) already today")
    if now.strftime("%H:%M") > latest:
        reasons.append(f"past {latest} UTC: the next cycle catches up")
    if reasons:
        return {"start": False, "why": why, "notStarted": "; ".join(reasons)}
    return {"start": True, "why": why, "recovery": not missing}


def judge(facts: dict, settings: dict) -> dict:
    """The day, whole: verdict, issues, what recovered on its own, the check-in's actions and what needs the owner."""
    runs = facts.get("runs") or []
    sources, trials = source_outcomes(facts), trial_outcomes(facts)
    github = github_outcomes(facts, settings)
    issues, recovered, needs, actions = [], [], [], []
    verdict = OK

    def flag(level, text, into=issues):
        nonlocal verdict
        verdict = _worse(level, verdict)
        into.append(text)

    first = runs[0] if runs else None
    final_run = runs[-1] if runs else None
    if not runs:
        flag(FAILED, "no daily cycle ran")
    for r in runs[1:]:
        actions.append(f"the check-in started a {'recovery' if r.get('recovery') else 'catch-up'} run at {ist(r.get('start'))} "
                       f"({r.get('why') or 'the day had not finished'})")
    if first and first.get("stopped"):
        if final_run is not first and not final_run.get("stopped") and final_run.get("state") != "ACTIVE":
            recovered.append(f"{first['stopped']} stopped the cycle; the recovery run built and loaded the day")
        else:
            flag(FAILED, f"{(final_run or first).get('stopped') or first['stopped']} failed: the day's snapshot and load didn't "
                         "finish (the day before's data is served)")
    for s in sources:
        if s["state"] == "failed":
            flag(FAILED, f"{s['label']} failed after {s['tries']} tries ({s.get('error')}); it keeps its last good data and the next cycle tries again")
        elif s["state"] in OUTSIDE:
            what = "was down for maintenance (nothing missed)" if s["state"] == "unavailable" else f"refused us (HTTP {s.get('status', '?')})"
            flag(ATTENTION, f"{s['label']} {what}")
        elif s["state"] == "recovered":
            recovered.append(f"{s['label']}: {s.get('error')} on the first try, fine on try {s['tries']}")
        for f in s.get("flags", []):
            if f in ("held", "mapping-upgrade"):
                flag(ATTENTION, f"{s['label']}: {f}")
    for t in trials:
        if t["state"] == "measured" and t.get("new") and t.get("ready"):
            needs.append(f"trial {t['id']} passes the automatic checks ({_n(t['records'])} records): read its sample and say if it should be promoted")
        elif t["state"] == "measured" and t.get("new"):
            needs.append(f"trial {t['id']} measured {_n(t['records'])} records; not ready: "
                         + ", ".join(k for k, v in t["checks"].items() if not v))
        elif t["state"] == "failed":
            flag(ATTENTION, f"trial {t['id']} failed ({t.get('error')}), failing day {t.get('failedDays') or '?'} of 3")
        elif t["state"] == "invalid":
            needs.append(f"trial {t['id']} is marked invalid: re-approve it on Data sources, or leave it")
    for g in github:
        name = f"{g['repo'].split('/', 1)[1]} {g['workflow']}"
        if g["state"] == "failed" and g["workflow"] == "verify":
            flag(FAILED, f"GitHub {name} of {g.get('detail')}: {g.get('conclusion')}")
        elif g["state"] == "failed" and g["workflow"] in ("snapshot", "sync"):
            flag(ATTENTION, f"GitHub {name}: {g['failures']} failed run(s) so far; GitHub tries again later today")
        elif g["state"] == "failed":
            flag(ATTENTION, f"GitHub {name}: last run {g.get('conclusion')}")
        elif g["state"] == "not yet run" and g["workflow"] == "verify":
            flag(ATTENTION, f"GitHub {name}: no run reported since yesterday")
        elif g["state"] == "recovered":
            recovered.append(f"GitHub {name}: {g['failures']} failed run(s), then succeeded")

    events = facts.get("jobEvents", {})
    dropped = (facts.get("nat") or {}).get("dropped")
    if dropped:
        flag(ATTENTION, f"Cloud NAT dropped {dropped} connections in 24 h")
    timings = _last(events.get("build-snapshot", []), "timings") or {}
    limit = (settings.get("timeouts") or {}).get("build-snapshot")
    if timings.get("total_s") and limit and timings["total_s"] > 0.8 * limit * 60:
        flag(ATTENTION, f"the build took {timings['total_s'] // 60} min, over 80% of its {limit} min limit")
    for job, name in (("build-snapshot", "dedi-failed"), ("dedi-check", "dedi-check-failed")):
        if _last(events.get(job, []), name):
            flag(ATTENTION, f"DeDi: {name}")
    for e in facts.get("workflowEvents", []):
        if e.get("event") == "checkin-failed":
            flag(ATTENTION, "the check-in at the end of the cycle couldn't run")
    if facts.get("late"):
        flag(ATTENTION, "the day's own final check-in didn't run; this summary comes from the next day's watchdog")
    if facts.get("unavailable"):
        flag(ATTENTION, "could not read: " + ", ".join(sorted(facts["unavailable"])))
    for r in facts.get("recoveryNotStarted") or []:
        needs.append(f"{r['why']}, and no recovery run was started ({r['notStarted']}): the owner can run the cycle from the admin page")
    if (facts.get("control") or {}).get("state") not in (None, "running"):
        needs.append(f"the system is {facts['control']['state']}")
    return {"verdict": verdict, "issues": issues, "recovered": recovered, "needs": needs, "actions": actions,
            "sources": sources, "trials": trials, "github": github, "timings": timings,
            "built": _last(events.get("build-snapshot", []), "built") or {}}


def summary(facts: dict, judged: dict) -> dict:
    """The email: a subject and a few lines, each short enough for one alert label."""
    runs, built, events = facts.get("runs") or [], judged["built"], facts.get("jobEvents", {})
    loaded = _last(events.get("load-db", []), "loaded") or {}
    sealed = _last(events.get("build-snapshot", []), "log-sealed") or {}
    records = built.get("records")
    subject = f"lei-commons {facts.get('day', '')}: {judged['verdict'].upper()}" + (f" · {_n(records)} records" if records else "")
    if runs:
        start, end = runs[0].get("start"), runs[-1].get("end")
        line = f"{ist(start)} to {ist(end)} ({_span(minutes(start, end))})" + (f", {len(runs)} runs" if len(runs) > 1 else "")
    else:
        line = "no cycle ran"
    if built:
        line += (f"; snapshot {built.get('kind')}: +{_n(built.get('added', 0))} added, {_n(built.get('changed', 0))} changed, "
                 f"{_n(built.get('deleted', 0))} deleted; relationships {_n(built.get('relationships', 0))}")
    if judged["timings"].get("total_s"):
        line += f"; build {judged['timings']['total_s'] // 60} min"
    if loaded:
        line += f"; load {loaded.get('mode')} {loaded.get('seconds', '?')} s"
    if sealed.get("day"):
        line += f"; decision log sealed through {sealed['day']} ({sealed.get('events', '?')} events)"
    counts = {}
    for s in judged["sources"]:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    sources = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none read"
    added = sum(s.get("new", 0) for s in judged["sources"])
    if added:
        sources += f"; {_n(added)} new records"
    trials = "; ".join(f"{t['id']} {t['state']}" + (f" {_n(t['records'])}" if t.get("records") is not None else "")
                       + (" (checks pass)" if t.get("ready") else "") for t in judged["trials"]) or "none"
    github = "; ".join(f"{g['repo'].split('/', 1)[1]} {g['workflow']} {g['state']}" + (f" ({g['detail']})" if g.get("detail") else "")
                       for g in judged["github"]) or "none expected"
    other = f"Cloud NAT dropped {(facts.get('nat') or {}).get('dropped', '?')} in 24 h"
    unmapped = facts.get("unmappedStatus")
    if unmapped is not None:
        other += f"; unmapped status words {len(unmapped)} ({_n(sum(unmapped.values()))} records)"
    dedi = _last(events.get("dedi-check", []), "dedi-check-ok")
    other += "; DeDi check ok" if dedi else ""
    lines = {"subject": subject, "cycle": line, "issues": "; ".join(judged["issues"]) or "none",
             "recovered": "; ".join(judged["recovered"]) or "nothing needed recovering",
             "actions": "; ".join(judged["actions"]) or "none", "needs": "; ".join(judged["needs"]) or "nothing",
             "sources": sources, "trials": trials, "github": github, "other": other}
    return {k: (v if len(v) <= LABEL_MAX else v[:LABEL_MAX - 1] + "…") for k, v in lines.items()}
