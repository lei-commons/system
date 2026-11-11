import json

import pytest

from lc_core.approvals import (SourceNotApproved, approved_record_sources, decide, effective, load_decisions, registry,
                               require_approved, status_of)
from lc_core.control import SystemHalted

SOURCES = [
    {"id": "gleif", "approval": "approved", "approved_by": "owner", "pipeline": {"records_as": "gleif"}},
    {"id": "br-rfb-cnpj", "approval": "approved", "pipeline": {"records_as": "br-rfb"}},
    {"id": "us-co-sos"},  # listed, not built, no starting status
]


def test_starting_statuses_come_from_the_registry_and_new_sources_are_pending():
    eff = effective(SOURCES, {})
    assert eff["gleif"]["status"] == "approved" and eff["us-co-sos"]["status"] == "pending"
    assert not eff["gleif"]["decided"] and status_of("never-heard-of", SOURCES, {}) == "pending"


def test_the_newest_owner_decision_wins_and_can_be_toggled():
    d = decide({}, "br-rfb-cnpj", "denied", "owner:o@x.test", "2026-11-11T10:00:00Z", "checking the licence")
    assert status_of("br-rfb-cnpj", SOURCES, d) == "denied" and approved_record_sources(SOURCES, d) == {"gleif"}
    d = decide(d, "br-rfb-cnpj", "pending", "owner:o@x.test", "2026-11-11T11:00:00Z", "legal review")
    d = decide(d, "br-rfb-cnpj", "approved", "owner:o@x.test", "2026-11-12T09:00:00Z", "licence confirmed")
    assert status_of("br-rfb-cnpj", SOURCES, d) == "approved"
    assert [h["status"] for h in d["br-rfb-cnpj"]["history"]] == ["denied", "pending"]
    with pytest.raises(ValueError):
        decide(d, "gleif", "maybe", "owner", "t", "x")


def test_jobs_stop_unless_their_source_is_approved():
    require_approved("gleif", SOURCES, {})
    for sid, decisions in (("us-co-sos", {}), ("gleif", {"gleif": {"status": "denied"}}), ("unknown", {})):
        with pytest.raises(SourceNotApproved):
            require_approved(sid, SOURCES, decisions)
    assert issubclass(SourceNotApproved, SystemHalted)  # a job treats it like a paused system: it stops cleanly


def test_a_missing_decisions_file_means_none_yet_but_an_unreadable_one_fails_closed(tmp_path):
    assert load_decisions(str(tmp_path / "source-approvals.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(SystemHalted):
        load_decisions(str(bad))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"gleif": {"status": "denied"}}))
    assert load_decisions(str(good))["gleif"]["status"] == "denied"


def test_the_real_registry_approves_only_the_sources_in_use():
    eff = effective(registry(), {})
    approved = sorted(k for k, v in eff.items() if v["status"] == "approved")
    assert approved == ["br-rfb-cnpj", "gb-coh", "gleif", "sg-acra", "us-ny-dos"]
    for source in registry():  # every source carries what the Data sources page shows
        assert source.get("url") and source.get("terms_url") and source.get("assessment", {}).get("risk"), source["id"]
