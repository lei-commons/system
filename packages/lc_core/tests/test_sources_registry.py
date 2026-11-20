"""The sources registry drives every list of sources: its checks and its order."""

import pytest
import yaml

from lc_core.sources import DEFAULT_PATH, daily_diffs, jobs, merge_order, names, pipelines


def write(tmp_path, sources):
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "sources": sources}))
    return path


def entry(sid, job, store, records_as, role=None, **extra):
    pipeline = {"job": job, "store": store, "records_as": records_as, "label": sid.upper(), "refresh": "daily",
                "dataset": f"{sid} data", "attribution": ""}
    if role:
        pipeline["role"] = role
    return {"id": sid, "name": sid, "licence": "CC0", "pipeline": {**pipeline, **extra}}


def test_the_real_registry_is_valid_and_gleif_merges_last():
    built = merge_order()
    assert built[-1]["records_as"] == "gleif" and all(p["role"] == "register" for p in built[:-1])
    assert "ingest-gleif" in jobs() and len(jobs()) == len(set(jobs())) == len(pipelines())
    assert names()["us-co-sos"] == "Colorado Secretary of State"
    assert DEFAULT_PATH.exists()


def test_registers_merge_before_lei_sources_whatever_the_file_order(tmp_path):
    path = write(tmp_path, [entry("gleif", "ingest-gleif", "gleif", "gleif", role="lei"), entry("b", "ingest-b", "b", "b"),
                            {"id": "unbuilt", "name": "x"}, entry("a", "ingest-a", "a", "a")])
    assert [p["records_as"] for p in merge_order(path)] == ["b", "a", "gleif"]
    assert jobs(path) == ["ingest-gleif", "ingest-b", "ingest-a"]  # the cycle keeps the file order


@pytest.mark.parametrize("bad,why", [
    ([entry("a", "ingest-a", "a", "a"), entry("b", "ingest-a", "b", "b")], "already uses job"),
    ([entry("a", "ingest-a", "a", "a"), entry("b", "ingest-b", "a", "b")], "already uses store"),
    ([entry("a", "ingest-a", "a", "a"), entry("b", "ingest-b", "b", "a")], "already uses records_as"),
    ([entry("a", "ingest-a", "a", "a", role="boss")], "role must be"),
    ([{"id": "a", "pipeline": {"job": "ingest-a", "store": "a", "records_as": "a", "label": "A", "refresh": "x",
                               "dataset": "d"}}], "attribution"),
    ([{"id": "a", "pipeline": {"job": "ingest-a", "store": "a", "records_as": "a", "label": "A", "refresh": "x",
                               "attribution": ""}}], "dataset"),
])
def test_bad_registries_are_refused(tmp_path, bad, why):
    with pytest.raises(ValueError, match=why):
        pipelines(write(tmp_path, bad))


DIFFS = {"enabled": False, "page": "https://example.test/download/sabun/", "section": "CSV", "file_date_format": "%Y%m%d",
         "max_per_run": 5, "overlap_days": 7}


def test_daily_diffs_are_read_from_the_pipeline_and_absent_means_none(tmp_path):
    path = write(tmp_path, [entry("a", "ingest-a", "a", "a", daily_diffs=DIFFS), entry("b", "ingest-b", "b", "b")])
    assert daily_diffs("a", path) == DIFFS and daily_diffs("b", path) is None and daily_diffs("zz", path) is None


def test_the_real_registry_has_japans_daily_diffs_off_until_the_first_watched_run():
    config = daily_diffs("jp-nta")
    assert config["enabled"] is False
    assert config["page"].startswith("https://www.houjin-bangou.nta.go.jp/")  # the approved host, no other


@pytest.mark.parametrize("over,why", [
    ({"enabled": "yes"}, "enabled must be a bool"),
    ({"page": "http://example.test/"}, "https"),
    ({"max_per_run": 0}, "max_per_run"),
    ({"max_per_run": True}, "max_per_run must be a int"),
    ({"overlap_days": -1}, "overlap_days"),
    ({"section": None}, "section must be a str"),
])
def test_bad_daily_diffs_are_refused(tmp_path, over, why):
    path = write(tmp_path, [entry("a", "ingest-a", "a", "a", daily_diffs={**DIFFS, **over})])
    with pytest.raises(ValueError, match=why):
        daily_diffs("a", path)
