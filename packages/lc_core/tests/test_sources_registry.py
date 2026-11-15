"""The sources registry drives every list of sources: its checks and its order."""

import pytest
import yaml

from lc_core.sources import DEFAULT_PATH, jobs, merge_order, names, pipelines


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
