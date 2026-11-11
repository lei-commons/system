import json

import pytest

from lc_site.system import BAD, BUSY, OK, UNKNOWN, SystemView, diagram, node_statuses

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
    s = node_statuses(data)
    assert (s["ingest-gleif"], s["ingest-gb"], s["gateway"], s["control"], s["agents"]) == (OK, BAD, OK, OK, "idle")


def test_missing_permissions_show_as_unavailable_never_as_healthy():
    data = SystemView(Reader(fail={"api", "storage"})).collect()
    assert data["jobs"] == [] and data["jobsError"] == "PermissionError" and data["control"]["error"] == "PermissionError"
    s = node_statuses(data)
    assert OK not in (s["ingest-gleif"], s["gateway"], s["control"], s["data"])


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


def test_diagram_escapes_and_draws_every_node():
    import html

    svg = diagram(SystemView(Reader()).collect(), html.escape)
    assert svg.count("<rect") == 13 and "Conduct gateway" in svg and "<b>" not in svg


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
