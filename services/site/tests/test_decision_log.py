import json

from lc_core.decisions import Recorder, seal_day
from lc_site.decision_log import DecisionLogReader


class Listing(list):
    def __init__(self, items, prefixes):
        super().__init__(items)
        self.prefixes = prefixes


class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def upload_from_filename(self, path, **kw):
        with open(path, "rb") as f:
            self.bucket.objects[self.name] = f.read()

    def upload_from_string(self, data, **kw):
        self.bucket.objects[self.name] = data.encode() if isinstance(data, str) else data

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def download_as_text(self):
        return self.bucket.objects[self.name].decode()


class Bucket:
    def __init__(self):
        self.objects, self.lists = {}, 0

    def blob(self, name):
        return Blob(self, name)

    def list_blobs(self, prefix="", delimiter=None):
        self.lists += 1
        names = sorted(n for n in self.objects if n.startswith(prefix))
        if delimiter:
            subs = sorted({prefix + n[len(prefix):].split(delimiter)[0] + delimiter for n in names if delimiter in n[len(prefix):]})
            return Listing([self.blob(n) for n in names if delimiter not in n[len(prefix):]], subs)
        return Listing([self.blob(n) for n in names], [])


def logged(bucket, tmp_path, day, run_id, record):
    r = Recorder("job:ingest-gb", log=lambda _: None, run_id=run_id)
    r(record)
    r.events[0].ts = f"{day}T02:00:0{run_id[-1]}.000Z"
    return r.write(bucket, str(tmp_path))


def test_sealed_days_are_verified_and_unsealed_days_come_from_their_runs(tmp_path):
    bucket = Bucket()
    logged(bucket, tmp_path, "2026-11-08", "run1", {"event": "already-done", "source": "gb", "publish": "2026-11-01"})
    logged(bucket, tmp_path, "2026-11-09", "run2", {"event": "busy", "reason": "lease held"})
    seal_day(bucket, "2026-11-08", str(tmp_path))
    reader = DecisionLogReader(bucket, ttl=0)

    days = reader.days()["days"]
    assert [(d["day"], d["sealed"]) for d in days] == [("2026-11-09", False), ("2026-11-08", True)]
    sealed = reader.day("2026-11-08")
    assert sealed["sealed"] and sealed["verified"] and sealed["events"][0]["decision"] == "skipped"
    today = reader.day("2026-11-09")
    assert not today["sealed"] and today["verified"] and today["events"][0]["reason"].startswith("another run")
    assert reader.day("../../etc") is None and reader.day("2026-11-10")["events"] == []


def test_a_tampered_sealed_day_shows_as_unverified(tmp_path):
    bucket = Bucket()
    logged(bucket, tmp_path, "2026-11-08", "run1", {"event": "already-done", "source": "gb", "publish": "x"})
    seal_day(bucket, "2026-11-08", str(tmp_path))
    line = json.loads(bucket.objects["chains/2026-11-08.jsonl"])
    line["reason"] = "rewritten"
    bucket.objects["chains/2026-11-08.jsonl"] = (json.dumps(line) + "\n").encode()
    assert DecisionLogReader(bucket, ttl=0).day("2026-11-08")["verified"] is False


def test_results_are_cached(tmp_path):
    bucket = Bucket()
    now = [0.0]
    reader = DecisionLogReader(bucket, ttl=60, clock=lambda: now[0])
    reader.days()
    reader.days()
    first = bucket.lists
    now[0] = 61
    reader.days()
    assert bucket.lists > first


def test_admin_actions_and_guard_pauses_are_in_the_chain_and_the_owner_shows_without_an_email(tmp_path):
    from lc_core.decisions import write_events

    bucket = Bucket()
    logged(bucket, tmp_path, "2026-11-22", "run1", {"event": "already-done", "source": "gb", "publish": "x"})
    write_events(bucket, [{"ts": "2026-11-22T03:04:05Z", "actor": "owner:o@x.test", "action": "set-budget:1000",
                           "subject": "monthly budget", "decision": "done", "reason": "lower the cap", "trigger": "site",
                           "component": "budget"}], label="admin")
    write_events(bucket, [{"ts": 1795395600.5, "actor": "service:budget-guard", "action": "pause", "subject": "control state",
                           "decision": "paused", "reason": "spend reached the cap"}], label="budget-guard")
    reader = DecisionLogReader(bucket, ttl=0)
    day = reader.day("2026-11-22")
    assert day["verified"] and [e["actor"] for e in day["events"]] == ["job:ingest-gb", "owner"]
    budget = day["events"][1]
    assert budget["action"] == "set-budget:1000" and budget["refs"] == {"component": "budget"} and budget["chained"]
    assert "o@x.test" not in json.dumps(day)
    seal = seal_day(bucket, "2026-11-22", str(tmp_path))
    assert seal["events"] == 2 and reader.day("2026-11-22")["verified"]  # sealed with the jobs' decisions
    assert reader.day("2026-11-23")["events"][0]["actor"] == "service:budget-guard"  # its own day (UTC)


def test_actions_kept_beside_the_chain_before_the_change_still_show_on_their_day(tmp_path):
    bucket = Bucket()
    logged(bucket, tmp_path, "2026-11-21", "run1", {"event": "already-done", "source": "gb", "publish": "x"})
    seal_day(bucket, "2026-11-21", str(tmp_path))
    bucket.objects["owner-control/2026-11-21T16:45:10Z-a1b2c3.json"] = json.dumps({
        "ts": "2026-11-21T16:45:10Z", "actor": "owner:o@x.test", "action": "set-budget:1000", "subject": "monthly budget",
        "trigger": "site", "reason": "test", "decision": "done", "component": "budget"}).encode()
    bucket.objects["owner-control/2026-11-20T07:39:11Z-d4e5f6.json"] = json.dumps({
        "ts": "2026-11-20T07:39:11Z", "actor": "owner:o@x.test", "action": "source-approve:ee-rik", "subject": "source ee-rik",
        "trigger": "site", "reason": "ok", "decision": "done", "component": "ee-rik"}).encode()
    reader = DecisionLogReader(bucket, ttl=0)
    assert [d["day"] for d in reader.days()["days"]] == ["2026-11-21", "2026-11-20"]
    day = reader.day("2026-11-21")
    assert day["sealed"] and day["verified"] and len(day["events"]) == 2
    legacy = day["events"][1]
    assert legacy["chained"] is False and legacy["actor"] == "owner" and legacy["ts"] == "2026-11-21T16:45:10.000Z"
    assert legacy["refs"] == {"component": "budget"} and "o@x.test" not in json.dumps(day)
    assert reader.day("2026-11-20")["events"][0]["action"] == "source-approve:ee-rik"
