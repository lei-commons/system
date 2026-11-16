"""Every record credits its sources: a URL where it can be seen, the licence, the attribution, a citation."""

from lc_core.attribution import credits, record_url

GB = {"id": "gb-coh", "name": "Companies House basic company data", "publisher": "UK Companies House", "licence": "OGL-UK-3.0",
      "licenceUrl": "https://example.gov.uk/licence", "datasetUrl": "https://download.companieshouse.gov.uk/en_output.html",
      "recordUrl": "https://find-and-update.company-information.service.gov.uk/company/{registrationNumber}",
      "attribution": "Contains public sector information licensed under the Open Government Licence v3.0.",
      "citation": "UK Companies House: Companies House basic company data. ..."}
GLEIF = {"id": "gleif", "name": "GLEIF LEI golden copy", "publisher": "GLEIF", "licence": "CC0-1.0",
         "datasetUrl": "https://www.gleif.org/en/lei-data/gleif-golden-copy", "recordUrl": "https://search.gleif.org/#/record/{lei}",
         "attribution": "", "citation": "GLEIF: ..."}
NY = {"id": "us-ny-dos", "name": "NY", "publisher": "New York DOS", "datasetUrl": "https://data.ny.gov/x", "recordUrl": None}
BY_ID = {s["id"]: s for s in (GB, GLEIF, NY)}


def test_record_urls_use_the_sources_page_or_fall_back_to_the_dataset():
    assert record_url({"registrationNumber": "01234567"}, GB) == GB["recordUrl"].format(registrationNumber="01234567")
    assert record_url({"lei": "5493001KJTIIGC8Y1R12"}, GLEIF) == "https://search.gleif.org/#/record/5493001KJTIIGC8Y1R12"
    assert record_url({}, GLEIF) == GLEIF["datasetUrl"]  # no LEI, no record page
    assert record_url({"registrationNumber": "1"}, NY) == NY["datasetUrl"]
    assert record_url({"registrationNumber": "a b/../c?d"}, GB).endswith("/company/a%20b%2F..%2Fc%3Fd")  # values are quoted
    assert record_url({}, None) is None


def test_a_merged_record_credits_every_source_in_order():
    record = {"sources": ["gb-coh", "gleif"], "registrationNumber": "01234567", "lei": "5493001KJTIIGC8Y1R12",
              "registryUrl": "https://find-and-update.company-information.service.gov.uk/company/01234567"}
    got = credits(record, BY_ID)
    assert [c["id"] for c in got] == ["gb-coh", "gleif"]
    assert got[0]["url"] == record["registryUrl"] and got[0]["attribution"].startswith("Contains public sector")
    assert got[1]["url"] == "https://search.gleif.org/#/record/5493001KJTIIGC8Y1R12" and got[1]["licence"] == "CC0-1.0"
    assert credits({"sources": "gb-coh,gleif", "registrationNumber": "1"}, BY_ID)[0]["id"] == "gb-coh"  # a string list too


def test_an_unknown_source_is_still_listed_never_dropped():
    assert credits({"sources": ["xx-new"]}, BY_ID) == [{"id": "xx-new", "name": "xx-new", "url": None, "attribution": "",
                                                        "citation": ""}]
