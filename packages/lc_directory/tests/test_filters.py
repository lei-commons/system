import pytest

pytest.importorskip("psycopg")
from lc_directory.pg import broad_values, text_match  # noqa: E402

STATS = {"byStatus": [{"value": "ACTIVE", "records": 70}, {"value": "Active", "records": 20}, {"value": None, "records": 10}],
         "bySources": [{"value": "gleif", "records": 60}, {"value": "gb-coh", "records": 30}, {"value": "gb-coh,gleif", "records": 10}]}


def test_broad_values_cover_at_least_half_the_records():
    assert broad_values(STATS, 100) == {("status", "ACTIVE"), ("source", "gleif")}
    assert broad_values(STATS, 0) == set() and broad_values({}, 100) == set()


def test_narrow_values_are_looked_up_and_broad_ones_only_checked():
    broad = broad_values(STATS, 100)
    match, params, rows, row_params = text_match("gmbh", country="de", status="Active", source="gb-coh", broad=broad)
    assert params == ["gmbh", "c=de", "s=active", "src=gb-coh"] and row_params == ["DE", "Active", "gb-coh"]
    match, params, rows, row_params = text_match("gmbh", status="ACTIVE", source="gleif", broad=broad)
    assert params == ["gmbh"] and row_params == ["ACTIVE", "gleif"]
    assert "legal_status = %s" in rows and "= ANY(sources)" in rows
    assert match.count("%s") == len(params) and rows.count("%s") == len(row_params)
