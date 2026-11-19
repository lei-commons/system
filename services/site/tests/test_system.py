import json

import pytest

from lc_site.system import BAD, BUSY, OK, SystemView

P, R = "lei-commons-prod", "us-central1"
BASE = f"https://run.googleapis.com/v2/projects/{P}/locations/{R}"


class Reader:
    project, region = P, R

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), 0

    def api(self, url):
        self.calls += 1
        if "api" in self.fail:
            raise PermissionError("no")
        if url == f"{BASE}/jobs":
            return {"jobs": [
                {"name": f"projects/{P}/locations/{R}/jobs/ingest-gleif",
                 "latestCreatedExecution": {"createTime": "t", "completionTime": "t2", "completionStatus": "EXECUTION_SUCCEEDED"}},
                {"name": f"projects/{P}/locations/{R}/jobs/ingest-gb",
                 "latestCreatedExecution": {"createTime": "t", "completionTime": "t2", "completionStatus": "EXECUTION_FAILED"}},
                {"name": f"projects/{P}/locations/{R}/jobs/egress-check", "latestCreatedExecution": {"createTime": "t"}}]}
        if url == f"{BASE}/services":
            return {"services": [{"name": f"projects/{P}/locations/{R}/services/conduct-gateway",
                                  "terminalCondition": {"state": "CONDITION_SUCCEEDED"}, "ingress": "INGRESS_TRAFFIC_INTERNAL_ONLY"}]}
        if "workflowexecutions" in url:
            return {"executions": [{"state": "ACTIVE", "startTime": "2026-11-10T02:00:00Z"},
                                   {"state": "SUCCEEDED", "startTime": "2026-11-09T02:00:00Z", "endTime": "e"},
                                   {"state": "SUCCEEDED", "startTime": "2026-11-08T02:00:00Z", "endTime": "e"},
                                   {"state": "FAILED", "startTime": "2026-11-07T02:00:00Z", "endTime": "e"}]}
        return {"jobs": [{"name": "x/ingest-gleif-daily", "schedule": "0 2 * * *", "state": "ENABLED", "lastAttemptTime": "t"}]}

    def bucket_json(self, bucket, name):
        if "storage" in self.fail:
            raise PermissionError("no")
        if name == "state.json":
            return {"state": "running", "paused_components": [], "updated_by": "owner:p", "updated_at": "t"}
        if name == "gleif/manifest.json":
            return {"records": 3355011, "lastKey": "2026-11-07 08:00:00 full"}
        if name.startswith("owner-control/"):
            return {"ts": "t", "actor": "owner:p", "action": "pause", "decision": "done", "reason": "<b>test</b>"}
        return None

    def bucket_text(self, bucket, name):
        if name.startswith("runs/"):
            return json.dumps({"ts": "2026-11-11T02:05:00Z", "actor": "job:ingest-gleif", "action": "ingested",
                               "subject": "gleif 2026-11-11", "decision": "done", "reason": "19,069 changes"}) + "\n"
        return None

    def bucket_names(self, bucket, prefix):
        if prefix.startswith("runs/"):
            return [f"{prefix}2026-11-11T020500-job-ingest-gleif-x.jsonl"]
        if "storage" in self.fail:
            raise PermissionError("no")
        return ["owner-control/1.json"] if prefix == "owner-control/" else []


def test_collects_everything_with_statuses():
    data = SystemView(Reader()).collect({"date": "2026-11-07"})
    assert {j["name"]: j["status"] for j in data["jobs"]} == {"ingest-gleif": OK, "ingest-gb": BAD, "egress-check": BUSY}
    assert data["control"]["state"] == "running" and data["stores"]["gleif"]["records"] == 3355011
    assert data["decisions"][0]["action"] == "pause" and data["schedules"][0]["status"] == OK
    assert {s["name"]: s["status"] for s in data["services"]}["conduct-gateway"] == OK


def test_missing_permissions_show_as_unavailable_never_as_healthy():
    data = SystemView(Reader(fail={"api", "storage"})).collect()
    assert data["jobs"] == [] and data["jobsError"] == "PermissionError" and data["control"]["error"] == "PermissionError"
    assert data["services"] == [] and all(not v.get("records") for v in data["stores"].values())


def test_results_are_cached_briefly():
    clock = [0.0]
    reader = Reader()
    view = SystemView(reader, clock=lambda: clock[0])
    view.collect()
    calls = reader.calls
    view.collect()
    assert reader.calls == calls
    clock[0] += 31
    view.collect()
    assert reader.calls > calls


def test_system_page_through_the_server(tmp_path):
    import http.client
    import threading

    from lc_site.server import make_server

    class Snap:
        meta, count, relationship_count = {"date": "2026-11-07", "snapshotRoot": "r"}, 0, 0

    srv = make_server(Snap(), ["o@x.test"], "aud", ("127.0.0.1", 0), iap=lambda t, a: "o@x.test" if t == "ok" else None,
                      system=SystemView(Reader()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path, token="ok"):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            c.request("GET", path, headers={"X-Goog-IAP-JWT-Assertion": token})
            r = c.getresponse()
            return r.status, r.read().decode()

        data = json.loads(get("/api/v1/system")[1])
        assert data["control"]["state"] == "running" and data["decisions"][0]["reason"] == "<b>test</b>"
        assert get("/api/v1/system", token="bad")[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_daily_cycle_history_and_its_clean_streak():
    from lc_site.system import clean_streak

    data = SystemView(Reader()).collect({})
    assert [c["status"] for c in data["cycles"]] == [BUSY, OK, OK, BAD] and data["cleanStreak"] == 2
    assert clean_streak([]) == 0
    assert clean_streak([{"state": "SUCCEEDED", "startTime": "2026-11-09T02:00:00Z"},
                         {"state": "FAILED", "startTime": "2026-11-09T05:00:00Z"}]) == 0  # a failed rerun that day counts


def test_sources_steps_and_the_cycle_timeline_come_from_the_registry():
    sources = [{"id": "gleif", "name": "GLEIF", "country": "*", "licence": "CC0-1.0", "cadence": "daily", "status": "active",
                "job": "ingest-gleif", "store": "gleif", "records_as": "gleif", "label": "GLEIF", "refresh": "daily",
                "relationships": "gleif-rr"},
               {"id": "xx-new", "name": "A new register", "country": "XX", "licence": "x", "cadence": "monthly",
                "status": "active", "job": "ingest-xx", "store": "xx", "records_as": "xx-new", "label": "New", "refresh": "monthly",
                "relationships": None}]
    data = SystemView(Reader(), sources=sources).collect({})
    assert [s["label"] for s in data["sources"]] == ["GLEIF", "New"]  # a new source appears without code changes
    assert data["sources"][0]["records"] == 3355011 and data["sources"][1]["records"] is None
    assert data["steps"] == ["ingest-gleif", "ingest-xx", "build-snapshot", "load-db"]
    assert data["feed"][0]["actor"] == "job:ingest-gleif"
    assert [s["job"] for s in data["lastCycle"]["steps"]] == data["steps"]


def test_the_data_sources_page_lists_every_registry_source_with_its_decisions():
    page = SystemView(Reader()).sources_page()
    ids = [s["id"] for s in page["sources"]]
    assert "gleif" in ids and "us-co-sos" in ids and page["total"] == len(ids)
    assert page["approved"] == 5 and page["pending"] == len(ids) - 5 and page["denied"] == 0
    gleif = next(s for s in page["sources"] if s["id"] == "gleif")
    assert gleif["built"] and gleif["records"] == 3355011 and gleif["terms_url"] and gleif["assessment"]["risk"] == "low"
    assert {a["id"] for a in gleif["actions"]} == {"source-deny:gleif", "source-pending:gleif"}


def test_unreadable_decisions_show_nothing_as_approved():
    page = SystemView(Reader(fail={"storage"})).sources_page()
    assert page["decisionsError"] and page["approved"] == 0 and all(not s["actions"] for s in page["sources"])


class EditedReader(Reader):
    edits = {"sources": {
        "us-co-sos": {"fields": {"name": "Colorado SOS", "url": "https://data.colorado.gov/business"},
                      "hint": "Try <the bulk CSV> first", "hint_by": "owner:p", "hint_at": "t", "edited_by": "owner:p"},
        "us-pa-dos": {"removed": True},
        "gleif": {"removed": True},  # a built source can't be removed this way
        "us-de-corporations": {"added": True, "fields": {"name": "Delaware", "country": "US"}, "edited_by": "owner:p"}},
        "history": [{"at": "t", "by": "owner:p", "source": "us-co-sos", "change": "hint", "after": {"hint": "secret text"}}]}

    def bucket_json(self, bucket, name):
        if name == "source-edits.json" and "storage" not in self.fail:
            return self.edits
        return super().bucket_json(bucket, name)


def test_the_data_sources_page_shows_the_owners_edits():
    page = SystemView(EditedReader()).sources_page()
    by = {s["id"]: s for s in page["sources"]}
    assert "us-pa-dos" not in by and "gleif" in by and by["us-de-corporations"]["approval"]["status"] == "pending"
    co = by["us-co-sos"]
    assert co["name"] == "Colorado SOS" and co["owner"]["hint"] == "Try <the bulk CSV> first"
    assert not co["removable"] and "deny it instead" in co["removeBlocked"]  # Colorado is built now
    assert by["in-mca"]["removable"]  # not built, not approved
    assert not by["gleif"]["removable"] and "deny it instead" in by["gleif"]["removeBlocked"]
    assert by["gleif"]["pipeline"] == {"job": "ingest-gleif", "store": "gleif", "records_as": "gleif"}
    assert {a["id"] for a in by["us-de-corporations"]["actions"]} == {"source-approve:us-de-corporations",
                                                                     "source-deny:us-de-corporations"}
    assert page["editable"] and page["changes"] == [{"at": "t", "by": "owner:p", "source": "us-co-sos", "change": "hint",
                                                     "fields": ["hint"]}]
    assert "secret text" not in json.dumps(page["changes"]) and page["limits"]["hint"] == 1000


def test_unreadable_edits_show_the_registry_and_offer_no_edits():
    class Broken(EditedReader):
        def bucket_json(self, bucket, name):
            if name == "source-edits.json":
                raise PermissionError("no")
            return super().bucket_json(bucket, name)

    page = SystemView(Broken()).sources_page()
    assert page["editsError"] == "PermissionError" and not page["editable"]
    assert "us-pa-dos" in {s["id"] for s in page["sources"]} and not any(s["removable"] for s in page["sources"])


# ---- each source's health, from its runs in the decision log (owner request 2026-11-18) ----

from lc_site.system import source_health  # noqa: E402


def _line(ts, action, decision, reason, run="ingest-gleif-ab12c"):
    return json.dumps({"ts": ts, "actor": "job:ingest-gleif", "action": action, "subject": "gleif 2026-11-18",
                       "decision": decision, "reason": reason, "run_id": run})


class RunsReader:
    def __init__(self, objects):
        self.objects = objects

    def bucket_text(self, bucket, name):
        return "\n".join(self.objects[name]) + "\n"


def test_a_failing_source_shows_its_error_and_attempts():
    a = "runs/2026-11-18/2026-11-18T020300Z-job-ingest-gleif-ingest-gleif-ab12c.jsonl"
    b = "runs/2026-11-18/2026-11-18T022900Z-job-ingest-gleif-ingest-gleif-cd34e.jsonl"
    other = "runs/2026-11-18/2026-11-18T023000Z-job-ingest-gb-ingest-gb-zz999.jsonl"
    reader = RunsReader({
        a: [_line("2026-11-18T02:03:00Z", "downloaded", "failed", "attempt 1 was bad, fetching again: FetchError: answered 403"),
            _line("2026-11-18T02:04:00Z", "ran", "failed", "stopped with BadFile: still bad after 3 downloads")],
        b: [_line("2026-11-18T02:29:00Z", "downloaded", "failed", "attempt 1 was bad", "ingest-gleif-cd34e"),
            _line("2026-11-18T02:30:00Z", "ran", "failed", "stopped with BadFile: 403 again", "ingest-gleif-cd34e")],
        other: [_line("t", "ingested", "done", "ok")]})
    h = source_health(reader, [a, b, other], "ingest-gleif", "2026-11-18")
    assert h["state"] == BAD and h["failedRunsToday"] == 2 and h["runsToday"] == 2
    p = h["problem"]
    assert p["error"] == "stopped with BadFile: 403 again" and p["run"] == "ingest-gleif-cd34e" and not p["recovered"]
    assert [e["decision"] for e in p["events"]] == ["failed", "failed"]


def test_a_source_that_recovered_after_retries_is_ok_but_says_so():
    a = "runs/2026-11-18/2026-11-18T020300Z-job-ingest-gleif-ingest-gleif-ab12c.jsonl"
    b = "runs/2026-11-18/2026-11-18T024000Z-job-ingest-gleif-ingest-gleif-cd34e.jsonl"
    reader = RunsReader({a: [_line("2026-11-18T02:04:00Z", "ran", "failed", "stopped with BadFile")],
                         b: [_line("2026-11-18T02:41:00Z", "ingested", "done", "17,000 changes", "ingest-gleif-cd34e")]})
    h = source_health(reader, [a, b], "ingest-gleif", "2026-11-18")
    assert h["state"] == OK and h["failedRunsToday"] == 1 and h["problem"]["recovered"]


def test_a_healthy_source_has_no_problem_and_a_source_without_runs_has_no_health():
    a = "runs/2026-11-18/2026-11-18T020300Z-job-ingest-gleif-ingest-gleif-ab12c.jsonl"
    h = source_health(RunsReader({a: [_line("t", "ingested", "done", "ok")]}), [a], "ingest-gleif", "2026-11-18")
    assert h["state"] == OK and "problem" not in h
    assert source_health(RunsReader({}), [], "ingest-gleif", "2026-11-18") is None


def test_one_jobs_runs_never_match_anothers_with_a_longer_name():
    a = "runs/2026-11-18/2026-11-18T020300Z-job-ingest-us-ia-ingest-us-ia-ab12c.jsonl"
    assert source_health(RunsReader({a: [_line("t", "ran", "failed", "x")]}), [a], "ingest-us", "2026-11-18") is None


def test_the_page_gets_the_gate_and_schedule_from_the_cycle_settings():
    settings = {"clean_days": 9, "schedule_utc": "03:30", "retries": 2, "retry_wait_minutes": 7, "cron": "30 3 * * *"}
    data = SystemView(Reader(), cycle_settings=settings).collect({})
    assert data["cycle"] == {"cleanDays": 9, "scheduleUtc": "03:30", "retries": 2, "retryWaitMinutes": 7}
    assert SystemView(Reader()).collect({})["cycle"]["cleanDays"] == 4  # config/cycle.yaml


# ---- a day whose only failures were sources refusing us counts as clean (owner decision 2026-11-18) ----

from lc_site.system import clean_streak, failed_sources, judge_cycles  # noqa: E402


def _cycle(day, state, failed=None):
    c = {"state": state, "startTime": f"{day}T02:00:00Z", "endTime": f"{day}T03:40:00Z"}
    if failed is not None:
        c["error"] = {"payload": json.dumps({"code": "sources-failed", "message": "m", "failed": failed})}
    return c


def test_refusal_only_days_count_as_clean_and_are_marked():
    cycles = [_cycle("2026-11-21", "FAILED", ["ingest-gleif"]), _cycle("2026-11-20", "SUCCEEDED"),
              _cycle("2026-11-19", "FAILED", ["ingest-gleif", "ingest-br"]), _cycle("2026-11-18", "SUCCEEDED")]
    refused = {("2026-11-21", ("ingest-gleif",)): True, ("2026-11-19", ("ingest-gleif", "ingest-br")): False}
    judged = judge_cycles(cycles, lambda day, jobs, until: refused[(day, tuple(jobs))])
    assert [(c["clean"], c["refusedOnly"]) for c in judged] == [(True, True), (True, False), (False, False), (True, False)]
    assert judged[0]["failedSources"] == ["ingest-gleif"] and judged[0]["status"] == BAD  # still shown as a failure
    assert clean_streak(judged) == 2


def test_other_failures_never_count_as_clean_and_unreadable_logs_fail_safe():
    snapshot_failed = {"state": "FAILED", "startTime": "2026-11-21T02:00:00Z", "error": {"payload": "{\"message\": \"build-snapshot failed\"}"}}
    assert failed_sources(snapshot_failed) is None
    judged = judge_cycles([snapshot_failed], lambda *a: True)
    assert judged[0]["clean"] is False

    def broken(*a):
        raise PermissionError("no")
    assert judge_cycles([_cycle("2026-11-21", "FAILED", ["ingest-gleif"])], broken)[0]["clean"] is False


def test_refused_only_reads_each_jobs_last_run_of_the_cycle():
    from lc_site.system import refused_only

    day = "2026-11-21"
    a = f"runs/{day}/{day}T020300.100Z-job-ingest-gleif-ingest-gleif-ab12c.jsonl"
    late = f"runs/{day}/{day}T090000.100Z-job-ingest-gleif-ingest-gleif-zz999.jsonl"  # after the cycle: ignored
    objects = {a: [_line(f"{day}T02:04:00Z", "refused", "failed", "the source refused us (HTTP 403)")],
               late: [_line(f"{day}T09:00:00Z", "ran", "failed", "stopped with ValueError")]}

    class R(RunsReader):
        def bucket_names(self, bucket, prefix):
            return [n for n in objects if n.startswith(prefix)]

    assert refused_only(R(objects), day, ["ingest-gleif"], f"{day}T03:40:00Z")
    objects[a] = [_line(f"{day}T02:04:00Z", "ran", "failed", "stopped with BadFile")]
    assert not refused_only(R(objects), day, ["ingest-gleif"], f"{day}T03:40:00Z")
    assert not refused_only(R(objects), day, ["ingest-br"], f"{day}T03:40:00Z")  # no run at all: not a refusal


def test_a_later_successful_run_with_nothing_to_record_clears_a_failure():
    """Brazil's quiet nights wrote no decision-log object, so the page kept showing an old failure."""
    a = "runs/2026-11-18/2026-11-18T124900Z-job-ingest-br-ingest-br-b5f5w.jsonl"
    reader = RunsReader({a: [_line("2026-11-18T12:49:30Z", "ran", "failed", "stopped with Denied: TimeoutError", "ingest-br-b5f5w")]})
    stale = source_health(reader, [a], "ingest-br", "2026-11-19")
    assert stale["state"] == BAD
    fresh = source_health(reader, [a], "ingest-br", "2026-11-19", succeeded_at="2026-11-19T02:19:42Z")
    assert fresh["state"] == OK and fresh["problem"]["recovered"] is True
    older = source_health(reader, [a], "ingest-br", "2026-11-19", succeeded_at="2026-11-18T06:14:18Z")
    assert older["state"] == BAD  # a success before the failure doesn't clear it
