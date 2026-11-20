"""Format v2 end to end: a simulated directory changing over many days, published through the real
job steps into in-memory buckets, then every kept day rebuilt and checked against the truth.
Plus the ways it must refuse: damaged files, broken chains, bad changes, unreadable manifests."""

import json
import random
from dataclasses import replace
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from lc_publish.testing import FakeBucket, World, did
from lc_core.publishing import Publishing, SnapshotSettings, load
from lc_core.snapshot_format import FormatError, parts_for
from lc_publish.job import publish
from lc_publish.prune import PruneAborted, plan as prune_plan, prune
from lc_publish.rebuild import BucketStore, DirStore, VerifyError, rebuild
from lc_publish.snapshot import CHANGE_SCHEMA, sha256_file

START = Date(2026, 11, 18)


def settings(**over) -> Publishing:
    base = load()
    snaps = replace(base.snapshots, **{"full_every_days": 3, "retain_days": 30, "part_target_records": 400,
                                       "change_file_max_rows": 50, "full_when_changes_exceed": 0.25, "state_kept": 3,
                                       "prune_grace_days": 0, "prune_dry_run": False, **over})
    return replace(base, snapshots=snaps)


class Env:
    def __init__(self, tmp_path, s=None):
        self.tmp, self.settings = tmp_path, s or settings()
        self.now = [datetime(2026, 11, 18, 3, tzinfo=timezone.utc)]
        self.snapshots, self.data = FakeBucket(lambda: self.now[0]), FakeBucket(lambda: self.now[0])
        self.events, self.truths, self.days = [], {}, 0

    def record(self, event):
        self.events.append(event)

    def publish(self, world, day=None):
        date = (START + timedelta(days=self.days if day is None else day)).isoformat()
        self.now[0] = datetime.fromisoformat(date + "T03:00:00+00:00")
        work = self.tmp / f"work-{date}-{len(self.events)}"
        work.mkdir()
        m = publish(self.snapshots, self.data, date, world.readers(), world.relationship_reader(), self.settings, work,
                    self.record, now=self.now[0])
        self.truths[date] = world.truth()
        if day is None:
            self.days += 1
        return m

    def rebuilt_rows(self, date):
        out = self.tmp / f"rebuilt-{date}-{len(self.events)}"
        report = rebuild(BucketStore(self.snapshots, self.tmp / f"rw-{date}-{len(self.events)}"), date, out, log=lambda r: None)
        rows = []
        for path in sorted(out.glob("*.parquet")):
            if not path.name.startswith("relationships"):
                rows += [json.dumps(r, sort_keys=True) for r in pq.read_table(path).to_pylist()]
        return report, sorted(rows)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_full_every_x_days_and_changes_in_between(env):
    world = World()
    kinds = []
    for _ in range(8):
        kinds.append(env.publish(world)["kind"])
        world.mutate()
    assert kinds == ["full", "changes", "changes", "full", "changes", "changes", "full", "changes"]
    built = [e for e in env.events if e["event"] == "built"]
    assert built[0]["base"] is None and built[0]["why"] == "no snapshot yet"
    assert all(e["base"] for e in built[1:]) and all(e["added"] and e["changed"] and e["deleted"] for e in built[1:])


def test_every_day_rebuilds_to_its_signed_root_and_the_true_records(env):
    world = World()
    for _ in range(8):
        env.publish(world)
        world.mutate()
    for date, truth in env.truths.items():
        report, rows = env.rebuilt_rows(date)
        assert report["ok"] and rows == truth, date


def test_a_changes_day_publishes_only_its_changes(env):
    world = World()
    env.publish(world)
    world.mutate()
    m = env.publish(world)
    day_objects = [n for n in env.snapshots.objects if n.startswith(m["date"] + "/")]
    assert m["kind"] == "changes" and all(f["kind"] in ("changes", "relationship-changes") for f in m["files"])
    assert sorted(n.split("/")[1] for n in day_objects) == sorted([f["name"] for f in m["files"]] + ["NOTICE.txt", "manifest.json"])
    assert m["changes"]["added"] + m["changes"]["changed"] + m["changes"]["deleted"] < m["records"] * 0.05
    assert all(r <= 50 for r in [f["records"] for f in m["files"]])  # change_file_max_rows
    assert json.loads(env.snapshots.objects["latest.json"]["data"])["kind"] == "changes"


def test_unchanged_parts_are_stored_once_and_shared(env):
    world = World()
    for _ in range(3):  # the first full snapshot has no counts to split groups by; the second one does
        env.publish(world)
    first = env.publish(world)
    for _ in range(2):
        world.sources["gleif"][did(2400)]["legalName"] += " Y"  # only a GLEIF-only German company changes
        env.publish(world)
    third = env.publish(world)  # the next full snapshot
    assert first["kind"] == third["kind"] == "full" and first["layout"]["groups"] == third["layout"]["groups"]
    first_objects = {p["name"]: p["object"] for p in first["parts"]}
    shared = [p for p in third["parts"] if first_objects.get(p["name"]) == p["object"]]
    assert any(p["name"].startswith("BR.br-rfb.") for p in shared)  # the Brazilian register didn't change
    assert any(p["name"].startswith("GB.gb-coh_gleif.") for p in shared)  # the register's name wins: unchanged
    assert not any(p["name"].startswith("DE.gleif.") for p in shared)
    built = [e for e in env.events if e["event"] == "built"]
    assert built[-1]["reused"] >= len(shared)


def test_large_groups_split_into_parts_by_shard(env):
    world = World()
    env.publish(world)
    for _ in range(3):
        env.publish(world)
    m = json.loads(env.snapshots.objects[(START + timedelta(days=3)).isoformat() + "/manifest.json"]["data"])
    br = [p for p in m["parts"] if p["group"] == "BR.br-rfb"]
    assert m["kind"] == "full" and len(br) == parts_for(1200, 400) == 4 and sum(p["records"] for p in br) == 1200
    assert {p["name"] for p in br} == {f"BR.br-rfb.p{i}of4.parquet" for i in range(4)}


def test_a_day_with_large_changes_becomes_full(env):
    world = World()
    env.publish(world)
    world.mutate(size=0.4)
    m = env.publish(world)
    assert m["kind"] == "full" and "exceed" in m["why"] and m["base"] and m["changes"]["changed"]


def test_missing_or_damaged_state_means_a_full_snapshot_without_a_base(env):
    world = World()
    first = env.publish(world)
    del env.data.objects[f"publish/state/{first['date']}/state.json"]
    m = env.publish(world)
    assert m["kind"] == "full" and m["base"] is None and "no build state" in m["why"]
    env.data.objects[f"publish/state/{m['date']}/entities.parquet"]["data"] += b"x"
    m2 = env.publish(world)
    assert m2["kind"] == "full" and m2["base"] is None and "damaged" in m2["why"]


def test_building_the_same_day_again_starts_from_its_base(env):
    world = World()
    env.publish(world)
    world.mutate()
    first = env.publish(world)
    world.mutate()
    again = env.publish(world, day=1)
    assert again["date"] == first["date"] and again["base"] == first["base"] and again["kind"] == "changes"
    report, rows = env.rebuilt_rows(again["date"])
    assert report["ok"] and rows == world.truth()


def test_nothing_outside_the_columns_is_published_in_parts_or_changes(env):
    world = World()
    env.publish(world)
    world.sources["gb-coh"][did(5)] = {**world.sources["gb-coh"][did(5)], "careOf": "MR PERSON", "legalName": "CHANGED"}
    env.publish(world)
    assert not any(b"MR PERSON" in o["data"] for o in env.snapshots.objects.values())


def test_status_words_are_kept_and_categorised(env):
    world = World()
    world.sources["gb-coh"][did(1)] = {**world.sources["gb-coh"][did(1)], "legalStatus": "Liquidation"}
    world.sources["gb-coh"][did(2)] = {**world.sources["gb-coh"][did(2)], "legalStatus": "Something New"}
    m = env.publish(world)
    words = {s["value"]: s["records"] for s in m["stats"]["byStatus"]}
    assert {"Active", "ACTIVE", "ATIVA", "Liquidation"} <= set(words)  # each register's own word (Article 4.3)
    categories = {s["value"]: s["records"] for s in m["stats"]["byStatusCategory"]}
    assert categories == {"active": m["records"] - 2, "in-liquidation": 1, "unknown": 1}
    assert m["stats"]["unmappedStatus"] == [{"value": "gb-coh: Something New", "records": 1}]
    assert m["columns"]["entities"].index("statusCategory") == m["columns"]["entities"].index("legalStatus") + 1


def test_new_columns_start_a_new_chain(env):
    """A day after one published with other columns is a full snapshot with no base: every record's leaf
    changed, so publishing them all as changes would only double the day."""
    world = World()
    first = env.publish(world)
    key = f"{first['date']}/manifest.json"
    old = json.loads(env.snapshots.objects[key]["data"])
    old.pop("columns")  # as every manifest before statusCategory
    env.snapshots.objects[key]["data"] = json.dumps(old).encode()
    world.mutate()
    second = env.publish(world)
    assert second["kind"] == "full" and second["base"] is None and second["why"] == "the record columns changed"
    assert not second["changes"]["files"] and second["anchor"] == second["date"]
    assert {"event": "no-previous-state", "reason": "the record columns changed"} in env.events
    report, rows = env.rebuilt_rows(second["date"])
    assert report["ok"] and rows == world.truth()
    world.mutate()
    assert env.publish(world)["kind"] == "changes"  # and the next day builds on it as usual


def test_same_input_same_bytes_and_roots(tmp_path):
    a, b = Env(tmp_path / "a"), Env(tmp_path / "b")
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    ma, mb = a.publish(World()), b.publish(World())
    assert ma["snapshotRoot"] == mb["snapshotRoot"] and [p["object"] for p in ma["parts"]] == [p["object"] for p in mb["parts"]]


# ---- rebuilding refuses what doesn't add up ----

def _two_days(env):
    world = World()
    env.publish(world)
    world.mutate()
    return env.publish(world)


def _store(env):
    return BucketStore(env.snapshots, env.tmp / "store")


def test_a_damaged_change_file_is_caught(env):
    m = _two_days(env)
    env.snapshots.objects[f"{m['date']}/changes.000.parquet"]["data"] += b"\0"
    with pytest.raises(VerifyError, match="SHA-256"):
        rebuild(_store(env), m["date"], log=lambda r: None)


def test_a_tampered_root_is_caught(env):
    m = _two_days(env)
    m["parts"][0]["root"] = "sha256:" + "0" * 64
    env.snapshots.objects[f"{m['date']}/manifest.json"]["data"] = json.dumps(m).encode()
    with pytest.raises(VerifyError, match="doesn't match its manifest"):
        rebuild(_store(env), m["date"], log=lambda r: None)


def test_a_missing_day_or_part_is_caught(env):
    m = _two_days(env)
    anchor = json.loads(env.snapshots.objects[f"{m['anchor']}/manifest.json"]["data"])
    del env.snapshots.objects[anchor["parts"][0]["object"]]
    with pytest.raises(VerifyError, match="missing"):
        rebuild(_store(env), m["date"], log=lambda r: None)
    del env.snapshots.objects[f"{m['anchor']}/manifest.json"]
    with pytest.raises(FormatError, match="missing"):
        rebuild(_store(env), m["date"], log=lambda r: None)


def _rewrite_changes(env, m, edit):
    name = f"{m['date']}/changes.000.parquet"
    path = env.tmp / "c.parquet"
    path.write_bytes(env.snapshots.objects[name]["data"])
    rows = edit(pq.read_table(path).to_pylist())
    pq.write_table(pa.table({f.name: [r.get(f.name) for r in rows] for f in CHANGE_SCHEMA}, schema=CHANGE_SCHEMA), path)
    env.snapshots.objects[name]["data"] = path.read_bytes()
    entry = next(f for f in m["files"] if f["name"] == "changes.000.parquet")
    entry["sha256"] = sha256_file(path)  # as if the manifest had been made to match
    env.snapshots.objects[f"{m['date']}/manifest.json"]["data"] = json.dumps(m).encode()


@pytest.mark.parametrize("edit,why", [
    (lambda rows: [{**rows[0], "op": "add", "directoryId": next(r for r in rows if r["op"] == "change")["directoryId"]}] + rows[1:],
     "already exists|out of order"),
    (lambda rows: [{**r, "op": "change"} if r["op"] == "add" else r for r in rows], "doesn't exist"),
    (lambda rows: [{**r, "op": "delete", "directoryId": "LECOZZ999999999999XX"} if i == 0 else r for i, r in enumerate(rows)],
     "doesn't exist|out of order"),
    (lambda rows: list(reversed(rows)), "out of order"),
    (lambda rows: [{**rows[0], "op": "upsert"}] + rows[1:], "unknown change"),
])
def test_impossible_changes_are_refused(env, edit, why):
    m = _two_days(env)
    _rewrite_changes(env, m, edit)
    with pytest.raises(VerifyError, match=why):
        rebuild(_store(env), m["date"], log=lambda r: None)


def test_a_signature_can_be_required(env):
    m = _two_days(env)
    with pytest.raises(VerifyError, match="no signature"):
        rebuild(_store(env), m["date"], require_signature=True, log=lambda r: None)


# ---- retention ----

def test_pruning_keeps_what_kept_days_need_and_they_all_still_rebuild(tmp_path):
    env = Env(tmp_path, settings(retain_days=4, full_every_days=3))
    world = World(n=300)
    for _ in range(12):
        env.publish(world)
        world.mutate()
    days = sorted(k.split("/")[0] for k in env.snapshots.objects if k[:2] == "20" and k.endswith("/manifest.json"))
    today = (START + timedelta(days=11)).isoformat()
    cutoff = (START + timedelta(days=11 - 4)).isoformat()
    assert days[0] < cutoff  # an older day is kept because a kept day needs it
    oldest_kept = json.loads(env.snapshots.objects[f"{days[0]}/manifest.json"]["data"])
    assert oldest_kept["kind"] == "full" and len(days) < 12
    for date in days:
        report, rows = env.rebuilt_rows(date)
        assert report["ok"] and rows == env.truths[date]
    referenced = set()
    for date in days:
        m = json.loads(env.snapshots.objects[f"{date}/manifest.json"]["data"])
        if m["kind"] == "full":
            referenced |= {p["object"] for p in m["parts"]}
    assert {k for k in env.snapshots.objects if k.startswith("parts/")} == referenced
    states = {k.split("/")[2] for k in env.data.objects if k.startswith("publish/state/")}
    assert len(states) == 3 and today in states
    assert [e for e in env.events if e["event"] == "pruned"][-1]["days_deleted"] >= 0


def test_parts_are_kept_during_the_grace_period(tmp_path):
    env = Env(tmp_path, settings(retain_days=1, full_every_days=1, prune_grace_days=3, keep_monthly_full=False))
    world = World(n=200)
    for _ in range(3):
        env.publish(world)
        world.mutate()
    pruned = [e for e in env.events if e["event"] == "pruned"]
    assert pruned[-1]["parts_deleted"] == 0 and pruned[-1]["days_deleted"] >= 1
    env.now[0] += timedelta(days=3)
    m = json.loads(env.snapshots.objects["latest.json"]["data"])
    result = prune(env.snapshots, env.data, m["date"], m, env.settings.snapshots, now=env.now[0], log=lambda r: None)
    assert result["parts_deleted"] > 0


def test_dry_run_deletes_nothing(tmp_path):
    env = Env(tmp_path, settings(retain_days=1, full_every_days=1, prune_dry_run=True, keep_monthly_full=False))
    world = World(n=200)
    for _ in range(4):
        env.publish(world)
        world.mutate()
    assert env.snapshots.deletes == [] and env.data.deletes == []
    assert [e for e in env.events if e["event"] == "pruned"][-1]["days_deleted"] >= 2


def test_an_unreadable_or_broken_chain_stops_pruning(tmp_path):
    env = Env(tmp_path, settings(retain_days=2))
    world = World(n=200)
    for _ in range(5):
        env.publish(world)
        world.mutate()
    latest = json.loads(env.snapshots.objects["latest.json"]["data"])
    env.snapshots.fail_reads.add(f"{latest['date']}/manifest.json")
    with pytest.raises(PruneAborted, match="can't be read"):
        prune_plan(env.snapshots, latest["date"], latest["date"], env.settings.snapshots)
    env.snapshots.fail_reads.clear()
    del env.snapshots.objects[f"{latest['anchor']}/manifest.json"]
    before = set(env.snapshots.objects)
    env.publish(world)
    assert any(e["event"] == "prune-skipped" for e in env.events)
    assert before - set(env.snapshots.objects) == set()  # nothing was deleted


def test_old_format_days_age_out_and_are_never_needed(tmp_path):
    env = Env(tmp_path, settings(retain_days=2))
    env.snapshots.objects["2026-11-01/manifest.json"] = {"data": json.dumps({"date": "2026-11-01", "files": []}).encode(),
                                                         "created": env.now[0]}
    env.snapshots.objects["2026-11-01/GB.parquet"] = {"data": b"x" * 10, "created": env.now[0]}
    env.snapshots.objects["latest.json"] = {"data": json.dumps({"date": "2026-11-01", "prefix": "2026-11-01"}).encode(),
                                            "created": env.now[0]}
    m = env.publish(World(n=100))
    assert m["kind"] == "full" and m["base"] is None and "old format" in m["why"]
    assert "2026-11-01/GB.parquet" not in env.snapshots.objects


def test_the_streaming_root_is_the_merkle_root():
    import hashlib

    from lc_publish.merkle import StreamingRoot, root

    rng = random.Random(1)
    for n in list(range(0, 70)) + [255, 256, 257, 1000]:
        leaves = [hashlib.sha256(str(rng.random()).encode()).digest() for _ in range(n)]
        s = StreamingRoot()
        for leaf_hash in leaves:
            s.add(leaf_hash)
        assert s.hexdigest() == root(leaves)


def test_settings_in_the_repository_are_valid():
    s = load()
    assert s.snapshots.full_every_days == 7 and s.snapshots.retain_days >= 1 and s.load.incremental is True
    assert isinstance(s.snapshots, SnapshotSettings)


def _unused():  # keep imports honest for linters
    return Path


def test_the_rebuild_command_on_a_copy_of_the_bucket(env, capsys):
    from lc_publish.rebuild import main

    m = _two_days(env)
    mirror = env.tmp / "mirror"
    for name, obj in env.snapshots.objects.items():
        (mirror / name).parent.mkdir(parents=True, exist_ok=True)
        (mirror / name).write_bytes(obj["data"])
    out = env.tmp / "rebuilt"
    assert main(["--date", m["date"], "--dir", str(mirror), "--out", str(out)]) == 0
    assert '"ok": true' in capsys.readouterr().out and len(list(out.glob("*.parquet"))) == len(m["parts"])
    (mirror / m["date"] / "changes.000.parquet").write_bytes(b"damaged")
    assert main(["--date", m["date"], "--dir", str(mirror)]) == 1
    assert "FAILED" in capsys.readouterr().err
    assert main(["--date", "2030-01-01", "--dir", str(mirror)]) == 1


def test_any_day_rebuilds_from_release_style_folders(env):
    """What the GitHub releases (or the site's recipe API) give a consumer: one folder per day, with its
    manifest and its files by name. A changes day needs its full snapshot's folder and every day since."""
    world = World(n=300)
    for _ in range(5):
        env.publish(world)
        world.mutate()
    folders = env.tmp / "releases"
    for name, obj in env.snapshots.objects.items():
        day = name.split("/")[0]
        if day[:2] == "20":
            manifest = json.loads(env.snapshots.objects[f"{day}/manifest.json"]["data"])
            (folders / day).mkdir(parents=True, exist_ok=True)
            (folders / name).write_bytes(obj["data"])
            for f in manifest["files"]:  # parts are named, not content-addressed, in a release
                (folders / day / f["name"]).write_bytes(env.snapshots.objects[f["object"]]["data"])
    for date, truth in env.truths.items():
        out = env.tmp / f"from-releases-{date}"
        assert rebuild(DirStore(folders), date, out, log=lambda r: None)["ok"]
        rows = sorted(json.dumps(r, sort_keys=True) for p in sorted(out.glob("*.parquet")) if not p.name.startswith("relationships")
                      for r in pq.read_table(p).to_pylist())
        assert rows == truth, date
    last = max(env.truths)
    anchor = json.loads((folders / last / "manifest.json").read_text())["anchor"]
    if anchor != last:
        part = next(p for p in (folders / anchor).glob("*.parquet") if not p.name.startswith(("changes", "relationship-")))
        part.unlink()
        with pytest.raises(VerifyError, match="missing"):
            rebuild(DirStore(folders), last, None, log=lambda r: None)


# ---- build speed: reading ahead, the state in one pass, the stage timings ----

def test_reading_ahead_builds_exactly_the_same_days(tmp_path):
    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    a, b = Env(tmp_path / "a", settings(read_ahead_shards=0)), Env(tmp_path / "b", settings(read_ahead_shards=3))
    wa, wb = World(), World()
    for _ in range(3):
        ma, mb = a.publish(wa), b.publish(wb)
        assert ma["snapshotRoot"] == mb["snapshotRoot"] and ma["changes"] == mb["changes"] and ma["kind"] == mb["kind"]
        wa.mutate(), wb.mutate()


def test_the_timings_event_splits_the_build_into_stages(env):
    env.publish(World())
    timings = [e for e in env.events if e.get("event") == "timings"][-1]
    for stage in ("read_s", "rows_s", "leaf_s", "parts_s", "diff_s", "relationships_s", "finish_s"):
        assert isinstance(timings[stage], int)


def _state_file(path, shards):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from lc_publish.snapshot import STATE_SCHEMA

    rows = [(s, f"id-{s}-{k}-{i}", bytes([s % 256, i]) * 8) for k, s in enumerate(shards) for i in range(3)]
    pq.write_table(pa.table({"shard": [r[0] for r in rows], "id": [r[1] for r in rows], "h": [r[2] for r in rows]},
                            schema=STATE_SCHEMA), path, row_group_size=4)
    return path


def test_the_state_is_read_in_one_pass_and_matches_a_filtered_read(tmp_path):
    from lc_publish.snapshot import StateReader

    path = _state_file(tmp_path / "s.parquet", [0, 1, 1, 3, 7, 7, 9])
    one_pass, filtered = StateReader(path), StateReader(path)
    for n in range(12):
        assert one_pass.shard(n) == filtered._filtered(n)
    assert one_pass.shard(3) == filtered._filtered(3)  # asked again, out of order: still right


def test_a_state_not_in_shard_order_is_still_read_right(tmp_path):
    from lc_publish.snapshot import StateReader

    path = _state_file(tmp_path / "s.parquet", [3, 0, 3, 1])
    reader = StateReader(path)
    assert not reader._in_order
    assert [len(reader.shard(n)) for n in range(4)] == [3, 3, 0, 6]


def test_the_first_full_snapshot_of_each_month_is_kept_for_good(tmp_path):
    # 2026-11-18 .. 2026-12-03: a full snapshot every 3 days, only 2 days retained.
    (tmp_path / "on").mkdir(), (tmp_path / "off").mkdir()
    kept = {}
    for name, flag in (("on", True), ("off", False)):
        env = Env(tmp_path / name, settings(retain_days=2, full_every_days=3, keep_monthly_full=flag))
        world = World(n=200)
        for _ in range(16):
            env.publish(world)
            world.mutate()
        kept[name] = sorted(k.split("/")[0] for k in env.snapshots.objects if k[:2] == "20" and k.endswith("/manifest.json"))
        if flag:
            for date in ("2026-11-18", "2026-12-02"):  # November's and December's first full days
                report, rows = env.rebuilt_rows(date)
                assert report["ok"] and rows == env.truths[date]
    assert "2026-11-18" in kept["on"] and "2026-11-18" not in kept["off"]
    assert "2026-12-02" in kept["on"]
