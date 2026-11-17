"""The publishing settings, the snapshot format's checks, and how load-db plans a load."""

import copy

import pytest
import yaml

from lc_core import publishing
from lc_core.publishing import ConfigError
from lc_core.snapshot_format import (FormatError, TooFar, back_to, check_manifest, from_anchor, group_of, part_name,
                                     parts_for, plan_load, shard_of)


def repo_settings():
    return yaml.safe_load(publishing.default_path().read_text())


def test_the_repository_settings_are_valid_and_x_defaults_to_seven():
    s = publishing.load()
    assert s.snapshots.full_every_days == 7 and s.snapshots.retain_days == 30 and s.snapshots.noncurrent_retain_days == 7
    assert s.load.incremental and s.load.fallback_to_full


@pytest.mark.parametrize("edit,why", [
    (lambda d: d["snapshots"].pop("full_every_days"), "full_every_days: missing"),
    (lambda d: d["snapshots"].update(full_every_days=0), "between 1 and"),
    (lambda d: d["snapshots"].update(full_every_days="7"), "whole number"),
    (lambda d: d["snapshots"].update(full_every_days=7.5), "whole number"),
    (lambda d: d["snapshots"].update(retain_days=True), "whole number"),
    (lambda d: d["snapshots"].update(full_when_changes_exceed=1.5), "between"),
    (lambda d: d["snapshots"].update(prune_dry_run="no"), "true or false"),
    (lambda d: d["snapshots"].update(retain_dayz=3), "unknown settings retain_dayz"),
    (lambda d: d["load"].update(incremental_max_days=-1), "between"),
    (lambda d: d.pop("load"), "load: missing"),
    (lambda d: d.update(version=2), "version"),
    (lambda d: d.update(extra={}), "unknown sections"),
])
def test_bad_settings_are_refused(edit, why):
    data = repo_settings()
    edit(data)
    with pytest.raises(ConfigError, match=why):
        publishing.parse(data)


def test_an_unreadable_file_is_a_config_error(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text("snapshots: [unclosed")
    with pytest.raises(ConfigError):
        publishing.load(bad)
    with pytest.raises(ConfigError):
        publishing.load(tmp_path / "missing.yaml")


def test_whole_numbers_are_accepted_for_fractions():
    data = repo_settings()
    data["snapshots"]["full_when_changes_exceed"] = 1
    assert publishing.parse(data).snapshots.full_when_changes_exceed == 1.0


def test_names_and_parts():
    assert group_of("GB", "gb-coh,gleif") == "GB.gb-coh_gleif" and part_name("GB.gb-coh_gleif", 3, 8) == "GB.gb-coh_gleif.p3of8.parquet"
    assert part_name("relationships", 0, 1) == "relationships.p0of1.parquet"
    for bad in [("gb", "gleif"), ("GB", ""), ("GB", "GB-COH"), ("GB", "../x")]:
        with pytest.raises(FormatError):
            group_of(*bad)
    with pytest.raises(FormatError):
        part_name("GB.gleif", 4, 4)
    assert [parts_for(n, 100) for n in (0, 100, 101, 400, 401, 10 ** 9)] == [1, 1, 2, 4, 8, 256]
    assert 0 <= shard_of("LECOZZ000000000001XX") < 256


# ---- chains of manifests ----

def day(date, kind, base=None, anchor=None, root=None, changes=10, records=1000):
    return {"schemaVersion": 2, "date": date, "kind": kind, "anchor": anchor or date,
            "base": {"date": base[0], "snapshotRoot": base[1]} if base else None,
            "snapshotRoot": root or f"sha256:{date}", "records": records, "relationships": 0,
            "parts": [{"name": "GB.gleif.p0of1.parquet", **({"object": "parts/" + "a" * 64 + ".parquet"} if kind == "full" else {})}],
            "changes": {"added": changes}, "files": []}


def chain(n=6, full_every=3):
    """Days 01..n; a full snapshot every ``full_every`` days."""
    days, anchor = {}, None
    for i in range(1, n + 1):
        date = f"2026-12-{i:02d}"
        kind = "full" if (i - 1) % full_every == 0 else "changes"
        anchor = date if kind == "full" else anchor
        prev = days.get(f"2026-12-{i - 1:02d}")
        days[date] = day(date, kind, (prev["date"], prev["snapshotRoot"]) if prev else None, anchor)
    return days


def test_from_anchor_walks_back_to_the_full_snapshot():
    days = chain()
    walk = from_anchor(days.get, days["2026-12-06"])
    assert [m["date"] for m in walk] == ["2026-12-04", "2026-12-05", "2026-12-06"]
    assert [m["date"] for m in from_anchor(days.get, days["2026-12-04"])] == ["2026-12-04"]


def test_broken_chains_are_reported():
    days = chain()
    gone = dict(days)
    del gone["2026-12-05"]
    with pytest.raises(FormatError, match="missing"):
        from_anchor(gone.get, days["2026-12-06"])
    swapped = copy.deepcopy(days)
    swapped["2026-12-05"]["snapshotRoot"] = "sha256:other"
    with pytest.raises(FormatError, match="doesn't match"):
        from_anchor(swapped.get, swapped["2026-12-06"])
    with pytest.raises(TooFar):
        back_to(days.get, days["2026-12-06"], lambda m: m["date"] <= "2026-12-01", limit=2)


@pytest.mark.parametrize("edit,why", [
    (lambda m: m.update(kind="weekly"), "kind"),
    (lambda m: m.update(kind="changes", base=None), "needs a base"),
    (lambda m: m.update(anchor="2026-12-02"), "own anchor"),
    (lambda m: m.update(base={"date": "2026-12-09", "snapshotRoot": "x"}), "base malformed"),
    (lambda m: m["parts"][0].update(object="../../etc"), "without an object"),
    (lambda m: m["parts"][0].update(name="x.parquet"), "bad part name"),
    (lambda m: m.update(files=[{"name": "a.parquet", "object": "elsewhere/a.parquet"}]), "bad object"),
    (lambda m: m.pop("snapshotRoot"), "lacks snapshotRoot"),
])
def test_malformed_manifests_are_refused(edit, why):
    m = day("2026-12-01", "full")
    edit(m)
    with pytest.raises(FormatError, match=why):
        check_manifest(m)


# ---- planning a load ----

def plan(days, latest, live, **kw):
    opts = {"incremental": True, "max_days": 14, "max_fraction": 0.2, "load_version": 5, **kw}
    return plan_load(days.get, days[latest], live, **opts)


def live_at(days, date, version=5):
    return {"date": date, "snapshotRoot": days[date]["snapshotRoot"], "loadVersion": version}


def test_already_loaded():
    days = chain()
    assert plan(days, "2026-12-06", live_at(days, "2026-12-06"))["mode"] == "already-loaded"


def test_incremental_from_the_day_before_and_across_a_full_snapshot():
    days = chain()
    p = plan(days, "2026-12-06", live_at(days, "2026-12-05"))
    assert p["mode"] == "incremental" and [m["date"] for m in p["steps"]] == ["2026-12-06"]
    p = plan(days, "2026-12-06", live_at(days, "2026-12-02"))  # 12-04 is a full snapshot with a base: still incremental
    assert p["mode"] == "incremental" and [m["date"] for m in p["steps"]] == ["2026-12-03", "2026-12-04", "2026-12-05", "2026-12-06"]


@pytest.mark.parametrize("live,kw,why", [
    (None, {}, "holds no snapshot"),
    ("old-version", {}, "another load version"),
    ("2026-12-02", {"incremental": False}, "off"),
    ("2026-12-01", {"max_days": 3}, "more than 3 days"),
    ("2026-12-05", {"max_fraction": 0.001}, "too large"),
    ("wrong-root", {}, "isn't in the chain"),
    ("future", {}, "isn't before"),
])
def test_full_loads_and_why(live, kw, why):
    days = chain()
    if live == "old-version":
        live = live_at(days, "2026-12-05", version=4)
    elif live == "wrong-root":
        live = {"date": "2026-12-05", "snapshotRoot": "sha256:restored-elsewhere", "loadVersion": 5}
    elif live == "future":
        live = {"date": "2026-12-09", "snapshotRoot": "x", "loadVersion": 5}
    elif live:
        live = live_at(days, live)
    p = plan(days, "2026-12-06", live, **kw)
    assert p["mode"] == "full" and why in p["why"]
    assert p["anchor"]["date"] == "2026-12-04" and [m["date"] for m in p["steps"]] == ["2026-12-05", "2026-12-06"]


def test_a_break_in_the_chain_means_a_full_load_or_an_error():
    days = chain()
    del days["2026-12-03"]
    p = plan(days, "2026-12-06", live_at(chain(), "2026-12-02"))
    assert p["mode"] == "full" and "no chain" in p["why"]
    del days["2026-12-05"]
    with pytest.raises(FormatError):  # not even the full snapshot can be reached: load-db fails loudly
        plan(days, "2026-12-06", None)
