"""The DeDi steps as the jobs run them: publishing after the build never fails the snapshot, and the check
repairs what it can and reports what it can't. Uses the real config/sources.yaml, so every source in the
registry must make a valid data-source record."""

import json
from datetime import timedelta

import pytest

from lc_dedi import jws
from lc_publish import dedi, dedi_job
from lc_publish.testing import FakeBucket
from test_dedi import T0, Clock, FlakySigner, seed_bucket


@pytest.fixture(autouse=True)
def no_owner_decisions(monkeypatch):
    monkeypatch.delenv("LC_SOURCE_APPROVALS_URI", raising=False)
    monkeypatch.delenv("LC_CONTROL_STATE_URI", raising=False)


@pytest.fixture
def cfg():
    return dedi.load_config()


@pytest.fixture
def signer():
    return jws.SeedSigner("lc-dedi-1", bytes(range(32)))


def test_publish_after_build_never_raises(cfg, signer):
    events = []
    assert dedi_job.publish_after_build(FakeBucket(), events.append, cfg=cfg, signer=signer, now=Clock()) is None
    assert events[-1]["event"] == "dedi-failed" and "latest.json" in events[-1]["error"]

    def broken_config():
        raise ValueError("dedi.yaml: bad")
    events.clear()
    original = dedi.load_config
    dedi.load_config = broken_config
    try:
        assert dedi_job.publish_after_build(FakeBucket(), events.append, signer=signer, now=Clock()) is None
    finally:
        dedi.load_config = original
    assert events == [{"event": "dedi-failed", "error": "ValueError: dedi.yaml: bad"}]


def test_every_registry_source_makes_a_valid_record(cfg, signer):
    bucket, events = FakeBucket(), []
    seed_bucket(bucket)
    status = dedi_job.publish_after_build(bucket, events.append, cfg=cfg, signer=signer, now=Clock())
    assert status and status["ok"], events
    records = json.loads(bucket.objects["dedi/dedi.data-sources.json"]["data"])["records"]
    from lc_core.approvals import registry
    assert [r["record_name"] for r in records] == [s["id"] for s in registry()]


def test_check_writes_its_report_and_repairs_a_stale_set(cfg, signer):
    bucket, events = FakeBucket(), []
    seed_bucket(bucket)
    dedi_job.publish_after_build(bucket, events.append, cfg=cfg, signer=signer, now=Clock())
    events.clear()
    ok = dedi_job.run_check(bucket, events.append, repair=False, cfg=cfg, signer=signer, now=Clock(T0 + timedelta(hours=2)))
    assert ok["ok"] and events[-1] == {"event": "dedi-check-ok", "repaired": False, "problems": []}
    assert json.loads(bucket.objects["dedi/check.json"]["data"])["ok"]
    # the next day's cycle failed, so nothing re-issued the set: by the afternoon check it is close to stale
    later = Clock(T0 + timedelta(hours=26))
    events.clear()
    report = dedi_job.run_check(bucket, events.append, repair=True, cfg=cfg, signer=signer, now=later)
    assert report["ok"] and report["repaired"]
    assert [e["event"] for e in events] == ["dedi-check-failed", "dedi-published", "dedi-check-ok"]


def test_a_check_that_cannot_repair_reports_failure(cfg, signer):
    bucket, events = FakeBucket(), []
    seed_bucket(bucket)
    dedi_job.publish_after_build(bucket, events.append, cfg=cfg, signer=signer, now=Clock())
    events.clear()
    report = dedi_job.run_check(bucket, events.append, repair=True, cfg=cfg, signer=FlakySigner(signer, 99),
                                now=Clock(T0 + timedelta(hours=40)), wait=lambda s: None)
    assert not report["ok"] and not report["repaired"]
    assert events[0]["event"] == "dedi-check-failed" and events[-1]["event"] == "dedi-check-failed"
    assert any(e["event"] == "dedi-failed" for e in events)
    saved = json.loads(bucket.objects["dedi/check.json"]["data"])
    assert not saved["ok"] and any("freshness" in p for p in saved["problems"])
