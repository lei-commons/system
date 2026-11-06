import json

import pytest

from lc_core.events import GENESIS, DecisionLog, Event, verify


def _ev(i=0, **kw):
    base = dict(
        actor="agent:checker-gb", action="reviewed", subject=f"PR ledger-gb#{i}",
        decision="approved", reason="all Tier 1 checks passed", trigger="pr:opened",
    )
    base.update(kw)
    return Event(**base)


def test_chain_appends_and_verifies(tmp_path):
    log = DecisionLog(tmp_path / "2026-11-06.jsonl")
    for i in range(5):
        log.append(_ev(i))
    lines = (tmp_path / "2026-11-06.jsonl").read_text().splitlines()
    ok, n, head = verify(lines)
    assert ok and n == 5 and head == log.head


def test_reopen_continues_chain(tmp_path):
    p = tmp_path / "d.jsonl"
    DecisionLog(p).append(_ev(1))
    log2 = DecisionLog(p)
    log2.append(_ev(2))
    ok, n, _ = verify(p.read_text().splitlines())
    assert ok and n == 2


def test_summary_comes_from_template(tmp_path):
    e = DecisionLog(tmp_path / "d.jsonl").append(_ev(7))
    assert e.summary == "agent:checker-gb reviewed PR ledger-gb#7: approved — all Tier 1 checks passed"


def test_tampering_detected(tmp_path):
    p = tmp_path / "d.jsonl"
    log = DecisionLog(p)
    for i in range(3):
        log.append(_ev(i))
    lines = p.read_text().splitlines()
    d = json.loads(lines[1])
    d["decision"] = "rejected"
    lines[1] = json.dumps(d)
    ok, n, msg = verify(lines)
    assert not ok and n == 1 and "hash" in msg


def test_deletion_detected(tmp_path):
    p = tmp_path / "d.jsonl"
    log = DecisionLog(p)
    for i in range(3):
        log.append(_ev(i))
    lines = p.read_text().splitlines()
    del lines[1]
    ok, _, msg = verify(lines)
    assert not ok and "prev" in msg


def test_edited_summary_detected(tmp_path):
    p = tmp_path / "d.jsonl"
    DecisionLog(p).append(_ev(1))
    d = json.loads(p.read_text())
    d["summary"] = "something a model wrote"
    ok, _, msg = verify([json.dumps(d)])
    assert not ok and "summary" in msg


def test_genesis_for_new_log(tmp_path):
    assert DecisionLog(tmp_path / "new.jsonl").head == GENESIS


@pytest.mark.parametrize("kw", [{"decision": "maybe"}, {"reason": ""}, {"reason": "two\nlines"}])
def test_invalid_events(kw):
    with pytest.raises(ValueError):
        _ev(**kw)
