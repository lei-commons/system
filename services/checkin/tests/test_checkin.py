import json
from datetime import datetime, timezone

from checkin import job, report

DAY = "2026-11-29"
ENDED = "2026-11-29T05:41:00Z"                                    # the cycle's work ended (11:11 IST)
FINAL = datetime(2026, 11, 29, 7, 41, tzinfo=timezone.utc)       # its final check-in, 2 hours later
CYCLE_END = datetime(2026, 11, 29, 5, 41, tzinfo=timezone.utc)
WATCHDOG = datetime(2026, 11, 30, 8, 17, tzinfo=timezone.utc)
SETTINGS = {"enabled": True, "watchdog_utc": "08:17", "final_after_minutes": 120, "timeouts": {"build-snapshot": 180},
            "dedi_check_utc": "14:17",
            "recovery": {"enabled": True, "max_runs_per_day": 1, "wait_minutes": 15, "latest_start_utc": "16:00"},
            "github": ["lei-commons/releases snapshot", "lei-commons/releases verify", "lei-commons/ledger-gb sync",
                       "lei-commons/system egress-check"]}
SOURCES = [{"id": "gleif", "job": "ingest-gleif", "label": "GLEIF", "stage": "production"},
           {"id": "gb-coh", "job": "ingest-gb", "label": "UK", "stage": "production"},
           {"id": "in-mca", "job": "ingest-in-mca", "label": "India", "stage": "trial"}]
# The cycle calling its final check-in is still running (it waited for it).
RUN = {"name": "projects/p/locations/r/workflows/daily-cycle/executions/abc", "startTime": "2026-11-29T02:00:05Z",
       "state": "ACTIVE"}


def entry(job_name, payload, ts="2026-11-29T03:00:00Z"):
    return {"resource": {"labels": {"job_name": job_name}}, "jsonPayload": payload, "timestamp": ts}


GOOD_JOBS = [
    entry("ingest-gleif", {"event": "done", "source": "gleif", "new_records": 1676, "existing_changed": 2328, "total": 3388349}),
    entry("ingest-gb", {"event": "already-done", "source": "gb"}),
    entry("build-snapshot", {"event": "built", "date": DAY, "kind": "changes", "records": 54597057, "added": 1589,
                             "changed": 2387, "deleted": 0, "relationships": 492784}),
    entry("build-snapshot", {"event": "timings", "total_s": 5434}),
    entry("build-snapshot", {"event": "log-sealed", "days": 1, "day": "2026-11-28", "events": 111}),
    entry("load-db", {"event": "loaded", "mode": "incremental", "seconds": 62}),
    entry("dedi-check", {"event": "dedi-check-ok"}, ts="2026-11-29T05:30:00Z"),
]
GITHUB = [
    {"repo": "lei-commons/releases", "workflow": "verify", "conclusion": "success", "date": "2026-11-28", "ts": "2026-11-29T00:42:00Z"},
    {"repo": "lei-commons/ledger-gb", "workflow": "sync", "conclusion": "success", "ts": "2026-11-29T06:40:00Z"},
    {"repo": "lei-commons/releases", "workflow": "snapshot", "conclusion": "success", "date": DAY, "detail": "released",
     "ts": "2026-11-29T06:30:00Z"},
    {"repo": "lei-commons/system", "workflow": "egress-check", "conclusion": "success", "ts": "2026-11-29T06:20:00Z"},
]


class FakeCloud:
    project = "p"

    def __init__(self, executions=None, jobs=GOOD_JOBS, flow=(), github=GITHUB, nat=0, sent=False, state="running",
                 checked_in=False, start_fails=False):
        self._executions = [RUN] if executions is None else executions
        self.jobs, self.flow, self.github, self.nat, self.sent = list(jobs), list(flow), list(github), nat, sent
        self.state, self.checked_in, self.start_fails, self.started = state, checked_in, start_fails, []

    def executions(self):
        return self._executions

    def entries(self, query, limit=3000):
        if "checkin-report" in query:
            return [{}] if self.sent else []
        if '"checkin-cycle"' in query and "OR" not in query:
            return [{}] if self.checked_in else []
        if "nat_gateway" in query:
            return [{}] * self.nat
        if job.GITHUB_LOG in query:
            return [{"jsonPayload": g, "timestamp": g["ts"]} for g in self.github]
        if "workflows.googleapis.com" in query:
            return [{"jsonPayload": p, "timestamp": "2026-11-29T02:30:00Z"} for p in self.flow]
        return self.jobs

    def bucket_json(self, bucket, name):
        if bucket == "control":
            return {"state": self.state}
        if name.endswith("manifest.json"):
            return {"stats": {"unmappedStatus": [{"value": "Dormant", "records": 7}]}}
        return None

    def start_cycle(self, argument):
        if self.start_fails:
            raise RuntimeError("no")
        self.started.append(argument)
        return "rec1"


def go(cloud, stage="final", now=FINAL, execution="abc", **kw):
    argv = ["--stage", stage]
    if stage in ("cycle", "final"):
        argv += ["--execution", execution, "--ended", kw.get("ended", ENDED), "--failed", json.dumps(kw.get("failed", [])),
                 "--stopped", kw.get("stopped", ""), "--recovery", kw.get("recovery", "false")]
    seen = []
    out = job.run(cloud, job.parse(argv), now, SOURCES, SETTINGS, record=seen.append)
    return out, seen


# ---- the final check-in: 2 hours after the cycle's work, one summary ------------------------------------------

def test_a_clean_day_gets_its_one_summary():
    out, seen = go(FakeCloud())
    assert out["event"] == "checkin-report" and out["verdict"] == "ok" and out["day"] == DAY, out["issues"]
    assert out["subject"] == "lei-commons 2026-11-29: OK · 54,597,057 records"
    assert out["cycle"].startswith("07:30 IST to 11:11 IST (3 h 41 min)")  # the work, not the 2-hour wait
    assert "+1,589 added" in out["cycle"] and "load incremental 62 s" in out["cycle"] and "sealed through 2026-11-28" in out["cycle"]
    assert "1,676 new records" in out["sources"]
    assert "releases snapshot ok (released)" in out["github"] and "releases verify ok (2026-11-28 verified)" in out["github"]
    assert out["needs"] == "nothing" and "unmapped status words 1 (7 records)" in out["other"] and "DeDi check ok" in out["other"]
    assert [e["event"] for e in seen] == ["checkin-report"]


def test_github_flows_not_yet_run_are_shown_not_counted_as_issues():
    out, _ = go(FakeCloud(github=GITHUB[:1]))  # only last night's verify so far
    assert out["verdict"] == "ok" and "releases snapshot not yet run" in out["github"] and "ledger-gb sync not yet run" in out["github"]


def test_a_release_not_built_yet_or_a_sync_before_the_cycle_ended_is_not_yet_run():
    github = [GITHUB[0], dict(GITHUB[1], ts="2026-11-29T04:40:00Z"), dict(GITHUB[2], detail="not-built-yet")]
    out, _ = go(FakeCloud(github=github))
    assert "releases snapshot not yet run" in out["github"] and "ledger-gb sync not yet run" in out["github"]


def test_github_failures_so_far_and_a_failed_verify():
    github = GITHUB + [{"repo": "lei-commons/ledger-gb", "workflow": "sync", "conclusion": "failure", "ts": "2026-11-29T06:10:00Z"}]
    out, _ = go(FakeCloud(github=github))
    assert "GitHub ledger-gb sync: 1 failed run(s), then succeeded" in out["recovered"]
    github = [GITHUB[0], {"repo": "lei-commons/releases", "workflow": "snapshot", "conclusion": "failure", "date": DAY,
                          "ts": "2026-11-29T06:30:00Z"}]
    out, _ = go(FakeCloud(github=github))
    assert out["verdict"] == "attention" and "GitHub releases snapshot: 1 failed run(s) so far; GitHub tries again later today" in out["issues"]
    github = [dict(GITHUB[0], conclusion="failure")]
    out, _ = go(FakeCloud(github=github))
    assert out["verdict"] == "failed" and "GitHub releases verify of 2026-11-28: failure" in out["issues"]
    out, _ = go(FakeCloud(github=[]))
    assert "GitHub releases verify: no run reported since yesterday" in out["issues"]


def test_a_day_already_summarised_stays_quiet():
    out, seen = go(FakeCloud(sent=True))
    assert out["event"] == "checkin-quiet" and [e["event"] for e in seen] == ["checkin-quiet"]


def test_a_run_that_started_a_recovery_leaves_the_summary_to_it_and_the_recovery_reports_both():
    stopped = dict(RUN)  # still running: it is waiting for its own final check-in
    rec = {"name": ".../executions/rec1", "startTime": "2026-11-29T05:42:00Z", "state": "ACTIVE",
           "argument": json.dumps({"started_by": "checkin", "recovery": True, "why": "load-db stopped the cycle"})}
    out, _ = go(FakeCloud(executions=[rec, stopped]), stopped="load-db")
    assert out["event"] == "checkin-quiet" and out["reason"] == "a recovery run reports the day"
    first = dict(RUN, state="FAILED", endTime="2026-11-29T07:41:30Z", error={"payload": "jobs/load-db failed"})
    out, _ = go(FakeCloud(executions=[rec, first]), execution="rec1", ended="2026-11-29T08:30:00Z",
                now=datetime(2026, 11, 29, 10, 30, tzinfo=timezone.utc))
    assert out["event"] == "checkin-report" and out["verdict"] == "ok", out["issues"]
    assert "load-db stopped the cycle; the recovery run built and loaded the day" in out["recovered"]
    assert "the check-in started a recovery run at 11:12 IST (load-db stopped the cycle)" in out["actions"]
    assert ", 2 runs" in out["cycle"] and "to 14:00 IST" in out["cycle"]


def test_retried_sources_are_recovered_and_failing_ones_are_issues():
    flow = [{"event": "source-failed", "job": "ingest-gleif", "error": {"tags": ["TimeoutError"]}}]
    jobs = GOOD_JOBS + [entry("ingest-gleif", {"event": "failed", "error": "TimeoutError", "message": "secret value"}),
                        entry("ingest-gb", {"event": "source-unavailable", "source": "gb-coh"})]
    out, _ = go(FakeCloud(jobs=jobs, flow=flow))
    assert out["verdict"] == "attention" and "GLEIF: TimeoutError on the first try, fine on try 2" in out["recovered"]
    assert "UK was down for maintenance (nothing missed)" in out["issues"]
    assert "secret value" not in json.dumps(out)  # error messages never reach the report
    out, _ = go(FakeCloud(jobs=jobs[:-1]), failed=["ingest-gleif"])
    assert out["verdict"] == "failed" and "GLEIF failed after 1 tries (TimeoutError)" in out["issues"]


def test_a_stopped_cycle_that_could_not_be_recovered_says_so():
    jobs = GOOD_JOBS[:2] + [entry("checkin", {"event": "recovery-not-started", "why": "load-db stopped the cycle",
                                               "notStarted": "the system is paused"})]
    out, _ = go(FakeCloud(jobs=jobs), stopped="load-db")
    assert out["verdict"] == "failed" and "load-db failed: the day's snapshot and load didn't finish" in out["issues"]
    assert "load-db stopped the cycle, and no recovery run was started (the system is paused)" in out["needs"]


def test_trials_ready_for_promotion_ask_the_owner():
    q = {"records": 3829633, "statusUnmapped": 0, "anomalies": {}, "mainShapeShare": 100.0}
    out, _ = go(FakeCloud(jobs=GOOD_JOBS + [entry("ingest-in-mca", {"event": "quality", "quality": q})]))
    assert "trial in-mca passes the automatic checks (3,829,633 records)" in out["needs"]
    out, _ = go(FakeCloud(jobs=GOOD_JOBS + [entry("ingest-in-mca", {"event": "quality", "quality": {**q, "statusUnmapped": 4}})]))
    assert "not ready: statusUnmapped" in out["needs"]


def test_unreadable_parts_are_named_never_taken_as_fine():
    class Broken(FakeCloud):
        def entries(self, query, limit=3000):
            if "checkin-report" in query:
                return []
            raise PermissionError("no")

    out, _ = go(Broken())
    assert out["verdict"] != "ok" and "could not read" in out["issues"] and "PermissionError" in out["issues"]


def test_every_emailed_line_fits_one_alert_label():
    flow = [{"event": "source-failed", "job": "ingest-gleif"}] * 40
    many = SETTINGS | {"github": [f"lei-commons/ledger-x{i} sync" for i in range(60)]}
    args = job.parse(["--stage", "final", "--execution", "abc", "--ended", ENDED, "--failed", json.dumps(["ingest-gleif"] * 60)])
    out = job.run(FakeCloud(flow=flow), args, FINAL, SOURCES, many, record=lambda e: None)
    assert all(len(out[k]) <= report.LABEL_MAX for k in ("subject", "cycle", "issues", "needs", "sources", "github"))


# ---- the cycle stage and the watchdog: recovery, and a late summary only if one never went out ---------------

def test_the_cycle_stage_starts_one_recovery_run_and_sends_nothing():
    cloud = FakeCloud()
    out, seen = go(cloud, "cycle", CYCLE_END, stopped="build-snapshot")
    assert cloud.started == [{"started_by": "checkin", "recovery": True, "why": "build-snapshot stopped the cycle"}]
    assert [e["event"] for e in seen] == ["recovery-started", "checkin-cycle"]  # no checkin-report: no email
    out, seen = go(FakeCloud(), "cycle", CYCLE_END)
    assert [e["event"] for e in seen] == ["checkin-cycle"] and out["recovery"] == {"start": False}


def test_recovery_is_bounded_and_says_why_not():
    earlier = {"name": ".../executions/old", "startTime": "2026-11-29T01:00:00Z", "state": "SUCCEEDED",
               "argument": json.dumps({"started_by": "checkin"})}
    for cloud, kw, now, why in [
        (FakeCloud(executions=[RUN, earlier]), {}, CYCLE_END, "1 recovery run(s) already today"),
        (FakeCloud(), {"recovery": "true"}, CYCLE_END, "already a recovery run"),
        (FakeCloud(state="paused"), {}, CYCLE_END, "the system is paused"),
        (FakeCloud(), {}, datetime(2026, 11, 29, 17, 0, tzinfo=timezone.utc), "past 16:00 UTC"),
        (FakeCloud(start_fails=True), {}, CYCLE_END, "starting it failed (RuntimeError)"),
    ]:
        out, seen = go(cloud, "cycle", now, stopped="load-db", **kw)
        assert cloud.started == [] and why in seen[0]["notStarted"], seen
        assert seen[0]["event"] == "recovery-not-started"


def test_the_watchdog_starts_a_missing_cycle_and_otherwise_does_nothing():
    at = datetime(2026, 11, 29, 8, 17, tzinfo=timezone.utc)
    cloud = FakeCloud(executions=[], sent=True)
    out, _ = go(cloud, "watchdog", at)
    assert out["action"] == "recovery" and cloud.started[0]["recovery"] is False
    cloud = FakeCloud(sent=True)
    out, _ = go(cloud, "watchdog", at)
    assert out["action"] == "none" and cloud.started == []
    stopped = dict(RUN, state="FAILED", error={"payload": "jobs/build-snapshot"})
    cloud = FakeCloud(executions=[stopped], sent=True)  # stopped, and its own check-in never ran
    out, _ = go(cloud, "watchdog", at)
    assert out["action"] == "recovery" and cloud.started[0]["why"] == "build-snapshot stopped the cycle"
    cloud = FakeCloud(executions=[stopped], checked_in=True, sent=True)  # its check-in already decided
    out, _ = go(cloud, "watchdog", at)
    assert out["action"] == "none" and cloud.started == []


def test_the_watchdog_sends_a_summary_that_never_went_out():
    yesterday = dict(RUN, state="FAILED", endTime="2026-11-29T05:41:00Z")
    today = {"name": ".../executions/t", "startTime": "2026-11-30T02:00:00Z", "endTime": "2026-11-30T05:00:00Z", "state": "SUCCEEDED"}
    seen = []
    job.run(FakeCloud(executions=[today, yesterday]), job.parse(["--stage", "watchdog"]), WATCHDOG, SOURCES, SETTINGS, record=seen.append)
    late = next(e for e in seen if e["event"] == "checkin-report")
    assert late["day"] == DAY and "this summary comes from the next day's watchdog" in late["issues"]
    seen.clear()
    job.run(FakeCloud(executions=[today, yesterday], sent=True), job.parse(["--stage", "watchdog"]), WATCHDOG, SOURCES, SETTINGS,
            record=seen.append)
    assert not any(e["event"] == "checkin-report" for e in seen)


def test_times_for_the_owner_are_in_ist():
    assert report.ist("2026-11-28T05:37:00Z") == "11:07 IST"
    assert report.ist("2026-11-29T05:41:00.123456Z") == "11:11 IST"  # Workflows' time.format
