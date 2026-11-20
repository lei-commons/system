import pytest

pytest.importorskip("psycopg")
from lc_directory.pg import broad_values, text_match  # noqa: E402

STATS = {"byStatusCategory": [{"value": "active", "records": 70}, {"value": "dissolved", "records": 20},
                             {"value": None, "records": 10}],
         "byStatus": [{"value": "ACTIVE", "records": 90}],  # the registers' own words never decide a filter
         "bySources": [{"value": "gleif", "records": 60}, {"value": "gb-coh", "records": 30}, {"value": "gb-coh,gleif", "records": 10}]}


def test_broad_values_cover_at_least_half_the_records():
    assert broad_values(STATS, 100) == {("status", "active"), ("source", "gleif")}
    assert broad_values(STATS, 0) == set() and broad_values({}, 100) == set()


def test_narrow_values_are_looked_up_and_broad_ones_only_checked():
    broad = broad_values(STATS, 100)
    match, params, rows, row_params = text_match("gmbh", country="de", status="dissolved", source="gb-coh", broad=broad)
    assert params == ["gmbh", "c=de", "s=dissolved", "src=gb-coh"] and row_params == ["DE", "dissolved", "gb-coh"]
    match, params, rows, row_params = text_match("gmbh", status="active", source="gleif", broad=broad)
    assert params == ["gmbh"] and row_params == ["active", "gleif"]
    assert "status_category = %s" in rows and "legal_status" not in rows and "= ANY(sources)" in rows
    assert match.count("%s") == len(params) and rows.count("%s") == len(row_params)
