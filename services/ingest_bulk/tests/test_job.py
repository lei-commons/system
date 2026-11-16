"""Ireland (and the bulk-file registers to come): the reader, the mapping, IDs that match GLEIF, a weekly run."""

import csv
import gzip
import io
import json
import zipfile
from datetime import date

import pytest
from lc_core.allowlist import AllowList
from lc_core.directory_id import mint
from lc_ingest.bulk import IRELAND, PROFILES, ie_record
from lc_ingest.gleif import Skip
from lc_ingest.store import decode_records

from ingest_bulk.job import profile_for, run

HEADER = ["company_num", "company_name", "company_status_code", "company_status", "company_type_code", "company_type",
          "company_reg_date", "last_ar_date", "company_address_1", "company_address_2", "company_address_3", "company_address_4",
          "comp_dissolved_date", "nard", "last_accounts_date", "company_status_date", "nace_v2_code", "eircode",
          "company_name_eff_date", "company_type_eff_date", "princ_object_code"]
ROW = {"company_num": "753469", "company_name": "LYONSKELLY LIMITED", "company_status": "Normal ",
       "company_type": "LTD - Private Company Limited by Shares", "company_reg_date": "2023-12-06",
       "company_address_1": "12 Pembroke Road, ", "company_address_2": "Ballsbridge", "company_address_3": "",
       "company_address_4": "Dublin", "comp_dissolved_date": "", "nace_v2_code": "7111.0", "eircode": "D04 TV21"}


def zipped(rows, name="companies.csv"):
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=HEADER)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in HEADER})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, "﻿" + text.getvalue())
    return out.getvalue()


class FakeGateway:
    def __init__(self, body):
        self.body, self.fetched = body, []

    def download(self, target, file):
        self.fetched.append(target)
        file.write(self.body)
        return len(self.body)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes = {"records": 0, "processed": []}, {}, {}

    def read_manifest(self):
        return {**self.manifest, "processed": list(self.manifest["processed"])}

    def write_manifest(self, m):
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard_file(self, n, path):
        with open(path, "rb") as f:
            self.shards[n] = decode_records(f.read())

    def write_changes_file(self, name, path):
        with gzip.open(path, "rt") as f:
            self.changes[name] = [json.loads(line) for line in f]

    def keep_source(self, name, path):
        pass


def test_the_mapping_and_an_id_gleif_will_match():
    r = IRELAND.record(ROW)
    assert r["directoryId"] == mint("IE", "RA000402", "753469")  # GLEIF's registeredAs for RA000402 is the CRO number
    assert r == {**r, "legalName": "LYONSKELLY LIMITED", "legalStatus": "Normal", "legalJurisdiction": "IE",
                 "registrationAuthority": "IE-CRO", "industryCodes": ["7111"], "incorporationDate": "2023-12-06",
                 "legalAddress": {"addressLines": ["12 Pembroke Road", "Ballsbridge", "Dublin"], "postalCode": "D04 TV21",
                                  "country": "IE"}}
    assert "legalForm" not in IRELAND.record({**ROW, "company_type": "Unknown"})


@pytest.mark.parametrize("change", [{"company_num": ""}, {"company_name": "  "}, {"company_num": "12A"}])
def test_rows_without_a_proper_number_or_name_are_dropped(change):
    with pytest.raises(Skip):
        ie_record({**ROW, **change})


def test_the_reader_skips_repeated_numbers_and_needs_exactly_one_csv():
    rows = list(IRELAND.read(io.BytesIO(zipped([ROW, {**ROW, "company_name": "SECOND COPY"}, {**ROW, "company_num": "1"}]))))
    assert [r["company_num"] for r in rows] == ["753469", "1"] and rows[0]["company_name"] == "LYONSKELLY LIMITED"
    with pytest.raises(ValueError, match="one .csv"):
        list(IRELAND.read(io.BytesIO(zipped([ROW], name="readme.txt"))))


def test_registry_and_profiles_agree():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    for source, profile in PROFILES.items():
        assert built[source]["job"] == profile.job and built[source]["store"] == profile.store
    with pytest.raises(SystemExit):
        profile_for("xx-nowhere")


def test_a_weekly_run():
    store, gw = MemStore(), FakeGateway(zipped([ROW, {**ROW, "company_num": "456570", "company_name": "OTHER DAC"}]))
    assert run(IRELAND, gw, store, today=date(2026, 11, 18), gate=lambda: None, allowlist=AllowList.load(),
               log=lambda r: None) == "done"
    recs = {k: v for s in store.shards.values() for k, v in s.items()}
    assert set(recs) == {mint("IE", "RA000402", "753469"), mint("IE", "RA000402", "456570")}
    assert store.manifest["processed"] == ["2026-W47"]
    assert run(IRELAND, FakeGateway(b""), store, today=date(2026, 11, 19), gate=lambda: None, allowlist=AllowList.load(),
               log=lambda r: None) == "already-done"


# ---- Finland ----

from lc_ingest.bulk import FINLAND, fi_record, zipped_json_array  # noqa: E402

FI = {"businessId": {"value": "0184194-4", "registrationDate": "1978-03-15"},
      "names": [{"name": "Asunto Oy Vaasan Suvikartano", "type": "1", "endDate": None},
                {"name": "Old Name Oy", "type": "1", "endDate": "2001-01-01"},
                {"name": "Suvikartano Isännöinti", "type": "3", "endDate": None}],
      "mainBusinessLine": {"type": "68202"}, "website": {"url": "www.example.fi"},
      "companyForms": [{"type": "2", "descriptions": [{"languageCode": "3", "description": "Housing corporation"}], "endDate": None}],
      "companySituations": [],
      "addresses": [{"type": 1, "street": "Rantakatu", "buildingNumber": "13", "entrance": "V", "apartmentNumber": "7",
                     "co": "c/o Markku Mäenpää", "postCode": "65100",
                     "postOffices": [{"city": "VAASA", "languageCode": "1"}, {"city": "VASA", "languageCode": "2"}]}]}


def zipped_json(objects, text=None):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("data_20261116.json", text if text is not None else json.dumps(objects, ensure_ascii=False))
    return out.getvalue()


def test_finland_mapping_never_keeps_the_care_of_line_or_apartment():
    r = FINLAND.record(FI)
    assert r["directoryId"] == mint("FI", "RA000188", "0184194-4") and r["legalForm"] == "Housing corporation"
    assert r["legalAddress"] == {"addressLines": ["Rantakatu 13 V"], "city": "VAASA", "postalCode": "65100", "country": "FI"}
    assert r["otherNames"] == ["Suvikartano Isännöinti"] and r["legalStatus"] == "Active" and r["industryCodes"] == ["68202"]
    assert "Mäenpää" not in json.dumps(r) and "7" not in r["legalAddress"]["addressLines"][0].split()


def test_finland_status_codes_are_translated():
    r = FINLAND.record({**FI, "companySituations": [{"type": "KONK", "endDate": None}]})
    assert r["legalStatus"] == "Bankruptcy"


@pytest.mark.parametrize("change,why", [
    ({"companyForms": [{"type": "5", "endDate": None}]}, "natural person"),
    ({"companyForms": [{"type": "54", "endDate": None}]}, "natural person"),
    ({"companyForms": [{"type": "16", "endDate": "2020-01-01"}]}, "no current company form"),
    ({"names": [{"name": "Old", "type": "1", "endDate": "2020-01-01"}]}, "no number or name"),
])
def test_finland_drops_people_and_unusable_records(change, why):
    with pytest.raises(Skip, match=why):
        fi_record({**FI, **change})


@pytest.mark.parametrize("chunk", [1, 7, 64, 1 << 20])
def test_the_json_stream_is_exact_whatever_the_chunk_size(chunk):
    objects = [{**FI, "businessId": {"value": f"{i:07d}-{i % 10}"}, "names": [{"name": f"Öljy-yhtiö {i} äö 🚢", "type": "1"}]}
               for i in range(40)]
    got = list(zipped_json_array(chunk=chunk)(io.BytesIO(zipped_json(objects))))
    assert got == objects


@pytest.mark.parametrize("text,why", [('{"a": 1}', "not a JSON array"), ('[{"a": 1}, {"b": ', "ends part-way|isn't closed"),
                                      ('[{"a": 1}', "isn't closed")])
def test_a_broken_file_is_an_error_not_a_short_read(text, why):
    with pytest.raises(ValueError, match=why):
        list(zipped_json_array(chunk=4)(io.BytesIO(zipped_json(None, text=text))))
