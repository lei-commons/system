import copy

import pytest
import yaml

from lc_core import guard
from lc_core.decisions import _event
from lc_core.guard import ConfigError, GuardSettings, MappingUpgrade, parse

UPGRADE = {"source": "us-ny-dos", "publish": "2026-W48", "reason": "street address added",
           "approved_by": "owner", "approved_on": "2026-11-20"}
BASE = {"version": 1, "sources": [{"id": "us-ny-dos"}, {"id": "us-co-sos"}],
        "volume_guard": {"max_fraction": 0.1, "minimum": 100, "mapping_upgrades": [UPGRADE]}}


def with_guard(**changes):
    data = copy.deepcopy(BASE)
    for key, value in changes.items():
        if value is ...:
            data["volume_guard"].pop(key)
        else:
            data["volume_guard"][key] = value
    return data


def test_the_repo_config_keeps_the_guard_at_a_tenth_of_100_or_more_records():
    settings = guard.load()
    assert settings.max_fraction == 0.1 and settings.minimum == 100
    ny = settings.upgrade_for("us-ny-dos", "2026-W48")
    assert ny is not None and ny.approved_by == "owner" and ny.approved_on == "2026-11-20"


def test_an_upgrade_names_exactly_one_source_and_publication():
    settings = parse(BASE)
    assert settings.upgrade_for("us-ny-dos", "2026-W48") == MappingUpgrade(**UPGRADE)
    assert settings.upgrade_for("us-ny-dos", "2026-W49") is None
    assert settings.upgrade_for("us-ny-dos", "2026-W47") is None
    assert settings.upgrade_for("us-co-sos", "2026-W48") is None
    assert GuardSettings(0.1, 100).upgrade_for("us-ny-dos", "2026-W48") is None


@pytest.mark.parametrize("data,message", [
    ({"version": 1, "sources": []}, "volume_guard: missing"),
    (with_guard(max_fraction=...), "max_fraction"),
    (with_guard(max_fraction=0), "max_fraction"),
    (with_guard(max_fraction=1.5), "max_fraction"),
    (with_guard(max_fraction="0.1"), "max_fraction"),
    (with_guard(max_fraction=True), "max_fraction"),
    (with_guard(minimum=...), "minimum"),
    (with_guard(minimum=-1), "minimum"),
    (with_guard(minimum=10.5), "minimum"),
    (with_guard(minimum=True), "minimum"),
    (with_guard(mapping_upgrades=...), "mapping_upgrades: missing"),
    (with_guard(mapping_upgrades={"source": "us-ny-dos"}), "must be a list"),
    (with_guard(extra=1), "unknown settings extra"),
    (with_guard(mapping_upgrades=[{**UPGRADE, "reason": " "}]), "reason: missing"),
    (with_guard(mapping_upgrades=[{k: v for k, v in UPGRADE.items() if k != "publish"}]), "publish: missing"),
    (with_guard(mapping_upgrades=[{**UPGRADE, "source": "us-xx"}]), "not a source"),
    (with_guard(mapping_upgrades=[{**UPGRADE, "approved_by": "claude"}]), "must be owner"),
    (with_guard(mapping_upgrades=[{**UPGRADE, "approved_on": "20 Nov"}]), "must be a date"),
    (with_guard(mapping_upgrades=[{**UPGRADE, "until": "never"}]), "unknown settings until"),
    (with_guard(mapping_upgrades=[UPGRADE, UPGRADE]), "listed twice"),
    (with_guard(mapping_upgrades=["us-ny-dos"]), "must be a mapping"),
])
def test_a_bad_setting_stops_the_run(data, message):
    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_no_upgrades_is_an_empty_list():
    assert parse(with_guard(mapping_upgrades=[])).mapping_upgrades == ()


def test_load_reads_the_file_named_by_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(with_guard(max_fraction=0.25, mapping_upgrades=[])))
    monkeypatch.setenv("LC_SOURCES_CONFIG", str(path))
    assert guard.load() == GuardSettings(0.25, 100, ())


def test_a_mapping_upgrade_is_a_decision_in_the_log():
    event = _event("job:ingest-ny", {"event": "mapping-upgrade", "source": "us-ny-dos", "publish": "2026-W48",
                                     "changed": 3_900_000, "total": 4_300_000, "max_fraction": 0.1,
                                     "reason": "street address added", "approved_by": "owner",
                                     "approved_on": "2026-11-20"})
    assert event.decision == "approved" and event.subject == "us-ny-dos 2026-W48"
    assert "3,900,000 of 4,300,000" in event.reason and "approved by the owner on 2026-11-20" in event.reason
    assert event.refs["approved_by"] == "owner" and event.refs["changed"] == 3_900_000
