"""The entity's own website is published where its source gives one (web-asserted facts design, step 1;
owner decision 2026-11-23), and only as a plain web address."""

from lc_publish.snapshot import COLUMNS, entity_row, website_url


def test_a_plain_web_address_is_published():
    assert website_url("https://www.example.fi/") == "https://www.example.fi/"
    assert website_url("http://example.org") == "http://example.org"
    assert website_url("  www.example.fi ") == "https://www.example.fi"


def test_anything_else_is_dropped():
    for bad in (None, "", "example", "javascript:alert(1)", "mailto:info@example.fi", "ftp://example.fi",
                "https://user:pw@example.fi", "https://exa mple.fi", "https://localhost", "info@example.fi"):
        assert website_url(bad) is None, bad


def test_the_row_carries_the_website_column():
    assert "website" in COLUMNS
    record = {"directoryId": "X", "legalName": "Oy Example Ab", "sources": ["fi-prh"], "website": "www.example.fi",
              "legalJurisdiction": "FI"}
    row = entity_row(record)
    assert row["website"] == "https://www.example.fi" and set(row) == set(COLUMNS)
    assert entity_row({**record, "website": "javascript:x"})["website"] is None
