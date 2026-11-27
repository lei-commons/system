"""Owner-supplied sources: the intake bundle, India's MCA reports, and the tidy-up of processed bundles."""

import io
import json
import tarfile
from collections import Counter

import pytest

from lc_ingest.gleif import Skip
from lc_ingest.owner_supplied import (INDIA_MCA, IntakeEmpty, fetch_for, locate_for, mca_read, mca_record, tidy)
from lc_ingest.pipeline import map_records
from lc_core.allowlist import AllowList
from lc_core.directory_id import mint

KEY = "a" * 40
OTHER = "b" * 40

FILES = {
    "mca/manifest.csv": "path,kind,month,rows\n",
    "mca/incorporated/2016-01/companies.csv":
        "cin,name,registered,class,category,sub_category,company_type,state,roc,activity_code,address\n"
        "U12345MH2016PTC123456,ALPHA TRADING PRIVATE LIMITED,2016-01-05,Private,Company limited by Shares,,"
        "Non-govt company,Maharashtra,RoC-Mumbai,46900,\"12 MG ROAD, ANDHERI EAST, MUMBAI 400069\"\n"
        "U99999DL2016PLC000001,BETA INFRA LIMITED,2016-01-09,Public,Company limited by Shares,,,,,,\n",
    "mca/incorporated/2016-01/llps.csv":
        "llpin,name,registered,state,roc,district,address\n"
        "AAA-1234,GAMMA ASSOCIATES LLP,2016-01-11,KARNATAKA,RoC-Bangalore,,\n",
    "mca/incorporated/2016-01/foreign-companies.csv":
        "fcrn,name,registered,office_type,country,state,activity_code,address\n"
        "F01234,DELTA GMBH,2016-01-20,BRANCH OFFICE,,Delhi,,\n",
    # The same company again in a later report: the first report's row stands.
    "mca/incorporated/2016-02/companies.csv":
        "cin,name,registered,class,category,sub_category,company_type,state,roc,activity_code,address\n"
        "U12345MH2016PTC123456,ALPHA TRADING PVT LTD,2016-02-01,Private,,,,,,,\n",
    "mca/struck-off/2023-10/companies.csv":
        "cin,name\nU99999DL2016PLC000001,BETA INFRA LIMITED\nU11111TN1999PTC022222,OLD MILLS PRIVATE LIMITED\n",
    "mca/struck-off/2023-11/llps.csv": "llpin,name\n",
    "mca/README.md": "# MCA\n",
}


def bundle(files=FILES) -> io.BytesIO:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, text in sorted(files.items()):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    out.seek(0)
    return out


class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def download_to_file(self, out):
        out.write(self.bucket.objects[self.name])

    def delete(self):
        del self.bucket.objects[self.name]


class Bucket:
    name = "lc-data"

    def __init__(self, objects):
        self.objects = dict(objects)

    def blob(self, name):
        return Blob(self, name)

    def list_blobs(self, prefix):
        return [Blob(self, n) for n in sorted(self.objects) if n.startswith(prefix)]


def pointer(key=KEY):
    return json.dumps({"key": key, "object": f"intake/in-mca/{key}.tar.gz", "commit": "c" * 40}).encode()


def records():
    return {r["id"]: r for r in mca_read(bundle())}


def test_each_company_once_with_the_strike_offs_applied():
    rows = records()
    assert set(rows) == {"U12345MH2016PTC123456", "U99999DL2016PLC000001", "AAA-1234", "F01234", "U11111TN1999PTC022222"}
    assert rows["U12345MH2016PTC123456"]["name"] == "ALPHA TRADING PRIVATE LIMITED"  # the first report's row
    assert rows["U99999DL2016PLC000001"]["struckOff"] == "2023-10"
    assert rows["U12345MH2016PTC123456"]["struckOff"] == ""
    # Registered before 2016, known only from the strike-off report.
    assert rows["U11111TN1999PTC022222"] == {"id": "U11111TN1999PTC022222", "kind": "company",
                                             "name": "OLD MILLS PRIVATE LIMITED", "struckOff": "2023-10"}


def test_a_company_maps_to_a_record_with_its_address_pin_and_activity():
    r = INDIA_MCA.record(records()["U12345MH2016PTC123456"])
    assert r["directoryId"] == mint("IN", "IN-MCA", "U12345MH2016PTC123456") and r["registrationNumber"] == "U12345MH2016PTC123456"
    assert r["legalForm"] == "Private - Company limited by Shares"
    assert r["incorporationDate"] == "2016-01-05" and r["industryCodes"] == ["46900"]
    assert r["legalAddress"] == {"addressLines": ["12 MG ROAD, ANDHERI EAST, MUMBAI 400069"], "region": "Maharashtra",
                                 "postalCode": "400069", "country": "IN"}
    assert "legalStatus" not in r  # a registration report's status isn't today's


def test_struck_off_llp_and_foreign_company_records():
    rows = records()
    assert INDIA_MCA.record(rows["U99999DL2016PLC000001"])["legalStatus"] == "Struck Off"
    old = INDIA_MCA.record(rows["U11111TN1999PTC022222"])
    assert old["legalStatus"] == "Struck Off" and old["legalForm"] == "Company" and "legalAddress" not in old
    llp = INDIA_MCA.record(rows["AAA-1234"])
    assert llp["legalForm"] == "Limited Liability Partnership" and llp["legalAddress"] == {"region": "Karnataka", "country": "IN"}
    assert INDIA_MCA.record(rows["F01234"])["legalForm"] == "Foreign company (Branch Office)"


def test_a_row_without_a_valid_identifier_or_name_is_skipped():
    with pytest.raises(Skip):
        mca_record({"kind": "company", "id": "U123", "name": "X"})
    with pytest.raises(Skip):
        mca_record({"kind": "llp", "id": "AAA-1234", "name": ""})


def test_every_record_passes_the_field_allowlist():
    counts = Counter()
    mapped = list(map_records(mca_read(bundle()), counts, AllowList.load(), mapper=INDIA_MCA.record))
    assert len(mapped) == 5


def test_locate_names_the_bundle_current_json_points_at_and_fetch_copies_it():
    data = bundle().getvalue()
    bucket = Bucket({"intake/in-mca/current.json": pointer(), f"intake/in-mca/{KEY}.tar.gz": data})
    key, url = locate_for("in-mca", bucket)(None)
    assert (key, url) == (KEY, f"gs://lc-data/intake/in-mca/{KEY}.tar.gz")
    out = io.BytesIO()
    assert fetch_for(bucket)(None, url, out) == len(data) and out.getvalue() == data


def test_nothing_copied_yet_or_a_pointer_elsewhere_is_an_error():
    with pytest.raises(IntakeEmpty):
        locate_for("in-mca", Bucket({}))(None)
    bad = json.dumps({"key": KEY, "object": "snapshots/x.tar.gz"}).encode()
    with pytest.raises(IntakeEmpty):
        locate_for("in-mca", Bucket({"intake/in-mca/current.json": bad}))(None)


def test_tidy_keeps_the_current_bundle_on_trial_and_deletes_it_once_processed_in_production():
    objects = {"intake/in-mca/current.json": pointer(), f"intake/in-mca/{KEY}.tar.gz": b"new",
               f"intake/in-mca/{OTHER}.tar.gz": b"old", "intake/other/x.tar.gz": b"x"}
    events = []
    trial = Bucket(objects)
    assert tidy(trial, "in-mca", [f"{KEY} @abc"], production=False, log=events.append) == 1
    assert set(trial.objects) == {"intake/in-mca/current.json", f"intake/in-mca/{KEY}.tar.gz", "intake/other/x.tar.gz"}
    prod = Bucket(objects)
    assert tidy(prod, "in-mca", [KEY], production=True, log=events.append) == 2
    assert set(prod.objects) == {"intake/in-mca/current.json", "intake/other/x.tar.gz"}
    # Production before the bundle is processed (a failed run) keeps it.
    waiting = Bucket(objects)
    assert tidy(waiting, "in-mca", [], production=True, log=events.append) == 1
    assert f"intake/in-mca/{KEY}.tar.gz" in waiting.objects
    assert events[0] == {"event": "intake-tidied", "source": "in-mca", "objects": 1}


@pytest.mark.parametrize("raw,form", [("Private", "Private"), ("Private Company", "Private"), ("PUBLIC", "Public"),
                                      ("Private(One Person Company)", "One Person Company"),
                                      ("Private One Person", "One Person Company"), ("One Person Company", "One Person Company")])
def test_the_class_reads_the_same_whatever_the_years_spelling(raw, form):
    assert mca_record({"kind": "company", "id": "U12345MH2016PTC123456", "name": "A", "class": raw})["legalForm"] == form
