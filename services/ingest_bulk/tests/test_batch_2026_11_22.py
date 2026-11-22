"""Sources approved by the owner on 2026-11-22, batch 1: the IRS exempt organisations and the Charity Commission.

Each names the columns it needs; a file without them stops with the missing and present column names only.
"""

import csv
import io
import zipfile
from datetime import date

import pytest

from lc_core.allowlist import AllowList
from lc_core.directory_id import mint
from lc_ingest.bulk import CCEW, IRS, PROFILES, ccew_record, irs_record, plain_csv, zipped_csv
from lc_ingest.gleif import Skip

from ingest_bulk.job import run
from test_canada import MemStore

IRS_HEADER = ("EIN,NAME,ICO,STREET,CITY,STATE,ZIP,GROUP,SUBSECTION,AFFILIATION,CLASSIFICATION,RULING,DEDUCTIBILITY,"
              "FOUNDATION,ACTIVITY,ORGANIZATION,STATUS,TAX_PERIOD,ASSET_CD,INCOME_CD,FILING_REQ_CD,PF_FILING_REQ_CD,"
              "ACCT_PD,ASSET_AMT,INCOME_AMT,REVENUE_AMT,NTEE_CD,SORT_NAME\n")
IRS_ROW = ("010018922,AMERICAN LEGION POST,% JOHN SMITH,PO BOX 12,BANGOR,ME,04401-0012,0925,19,3,1200,195001,2,00,"
           "000000000,5,01,202312,1,1,01,0,12,5000,6000,6000,W30,\n")
IRS_ABROAD = ("980123456,FRIENDS OF A SCHOOL ABROAD,,1 RUE X,PARIS,,75001,0000,03,3,1000,201001,1,15,"
              "000000000,1,01,202312,0,0,02,0,12,0,0,0,B11,\n")

CCEW_HEADER = ("date_of_extract\torganisation_number\tregistered_charity_number\tlinked_charity_number\tcharity_name\t"
               "charity_type\tcharity_registration_status\tdate_of_registration\tdate_of_removal\t"
               "charity_contact_address1\tcharity_contact_postcode\tcharity_contact_phone\tcharity_contact_email\t"
               "charity_contact_web\tcharity_company_registration_number\n")


def ccew_line(number, linked, name, status="Registered", company="", web="www.example.org.uk"):
    return (f"2026-11-21 00:00:00\t1\t{number}\t{linked}\t{name}\tTrust\t{status}\t1963-05-01 00:00:00\t\t"
            f"1 Vicarage Lane\tAB1 2CD\t01234 567890\tsomeone@example.org\t{web}\t{company}\n")


def rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def zipped(name, text):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(name, text)
    out.seek(0)
    return out


def test_irs_keeps_organisations_and_never_the_care_of_name():
    r = IRS.record(rows(IRS_HEADER + IRS_ROW)[0])
    assert r["directoryId"] == mint("US", "US-IRS", "010018922") and r["registrationNumber"] == "010018922"
    assert r["legalName"] == "AMERICAN LEGION POST" and r["legalForm"] == "Association, exempt under 501(c)(19)"
    assert r["legalStatus"] == "Unconditional exemption"
    assert r["legalAddress"] == {"addressLines": ["PO BOX 12"], "city": "BANGOR", "region": "ME", "postalCode": "04401",
                                 "country": "US"}
    assert "JOHN SMITH" not in repr(r)  # the in-care-of column names a person
    abroad = IRS.record(rows(IRS_HEADER + IRS_ABROAD)[0])
    assert "legalAddress" not in abroad  # no address or country guessed for an organisation abroad
    assert abroad["legalForm"] == "Corporation, exempt under 501(c)(3)"
    for bad in ({"EIN": "12345"}, {"NAME": " "}):
        with pytest.raises(Skip):
            irs_record({**rows(IRS_HEADER + IRS_ROW)[0], **bad})


def test_irs_reads_all_four_regions_once_a_week():
    class Gateway:
        def __init__(self):
            self.fetched = []

        def download(self, target, file):
            self.fetched.append(target)
            body = (IRS_HEADER + (IRS_ROW if target.endswith("eo1.csv") else "")).encode()
            file.write(body)
            return len(body)

    store, gw = MemStore(), Gateway()
    out = run(IRS, gw, store, today=date(2026, 11, 23), gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None,
              stage="production")  # as it will run once promoted
    assert out == "done" and [u.rsplit("/", 1)[1] for u in gw.fetched] == ["eo1.csv", "eo2.csv", "eo3.csv", "eo4.csv"]
    assert all(u.startswith("https://www.irs.gov/pub/irs-soi/") for u in gw.fetched)
    records = {k: v for s in store.shards.values() for k, v in s.items()}
    assert list(records) == [mint("US", "US-IRS", "010018922")]


def test_ccew_keeps_main_charities_that_are_not_companies_and_no_contact_details():
    text = (CCEW_HEADER + ccew_line("200001", "0", "THE VILLAGE HALL TRUST")
            + ccew_line("200001", "1", "THE VILLAGE HALL TRUST - BUILDING FUND")
            + ccew_line("200002", "0", "A CHARITABLE COMPANY", company="01234567")
            + ccew_line("200003", "0", "AN OLD CHARITY", status="Removed", web=""))
    read = CCEW.read(zipped("publicextract.charity.txt", text))
    kept, why = [], []
    for row in read:
        try:
            kept.append(CCEW.record(row))
        except Skip as exc:
            why.append(str(exc))
    assert [r["registrationNumber"] for r in kept] == ["200001", "200003"]
    assert why == ["a linked charity: part of its main charity's record",
                   "also a company: in the directory through Companies House"]
    first = kept[0]
    assert first["directoryId"] == mint("GB", "GB-CCEW", "200001") and first["legalForm"] == "Trust"
    assert first["incorporationDate"] == "1963-05-01" and first["website"] == "www.example.org.uk"
    assert first["legalAddress"] == {"country": "GB"}
    assert not any(x in repr(kept) for x in ("Vicarage", "AB1 2CD", "01234", "someone@"))  # often a trustee's own
    assert kept[1]["legalStatus"] == "Removed" and "website" not in kept[1]
    with pytest.raises(Skip):
        ccew_record({"registered_charity_number": "x", "charity_name": "A"})


def test_a_file_without_the_expected_columns_names_them_and_nothing_else():
    with pytest.raises(ValueError) as exc:
        list(plain_csv(columns=("EIN", "NAME"))(io.BytesIO(b"ein,org_name\n123456789,SECRET VALUE\n")))
    assert "missing columns ['EIN', 'NAME']" in str(exc.value) and "['ein', 'org_name']" in str(exc.value)
    assert "SECRET VALUE" not in str(exc.value)
    with pytest.raises(ValueError, match="missing columns"):
        list(zipped_csv(member_suffix=".txt", delimiter="\t", columns=("charity_name",))(zipped("a.txt", "x\ty\n1\t2\n")))
    # A byte-order mark or stray spaces in the header don't hide a column.
    assert list(plain_csv(columns=("EIN",))(io.BytesIO("﻿ EIN ,NAME\n1,A\n".encode("utf-8"))))[0]["EIN"] == "1"


def test_the_new_profiles_are_registered_and_estonia_waits():
    from lc_core.sources import pipelines

    built = {p["id"]: p for p in pipelines()}
    for source in ("us-irs-eo-bmf", "gb-ccew"):
        assert source in PROFILES and built[source]["job"] == PROFILES[source].job
    assert "ee-rik" not in PROFILES and "ee-rik" not in built  # its terms must be accepted: the owner decides


class ReportStore(MemStore):
    def __init__(self):
        super().__init__()
        self.json = {}

    def write_json(self, name, data):
        self.json[name] = data


def irs_gateway(body, fetched=None):
    class Gateway:
        def download(self, target, file):
            if fetched is not None:
                fetched.append(target)
            text = (IRS_HEADER + (body if target.endswith("eo1.csv") else "")).encode()
            file.write(text)
            return len(text)
    return Gateway()


def trial(store, body, events, code="abc123", fetched=None, today=date(2026, 11, 23)):
    import os
    os.environ["LC_CODE_VERSION"] = code
    try:
        return run(IRS, irs_gateway(body, fetched), store, today=today, gate=lambda: None, allowlist=AllowList.load(),
                   log=events.append, stage="trial")
    finally:
        del os.environ["LC_CODE_VERSION"]


def test_a_trial_only_measures_and_logs_counts_only():
    body = IRS_ROW + IRS_ABROAD + IRS_ROW.replace("010018922", "010018923") + IRS_ROW.replace("010018922", "12")
    store, events = ReportStore(), []
    assert trial(store, body, events) == "done"
    quality = [e for e in events if e["event"] == "quality"]
    assert len(quality) == 4  # one per regional file; the three empty ones measure nothing
    q = quality[0]["quality"]
    assert q["records"] == 3 and q["skipped"] == {"no number or name": 1}
    assert q["fill"]["legalName"] == 100.0 and q["addressFill"]["country"] == 66.7 and q["statusUnmapped"] == 0
    assert q["anomalies"] == {"same name, country and postcode as an earlier record": 1}  # the repeated post
    assert [tuple(x) for x in q["numberShapes"]] == [("999999999", 3)]
    assert "AMERICAN LEGION" not in repr(events)  # no names in the public log
    # Nothing but the reports is kept: no records, no changes, no copy of the file.
    assert store.shards == {} and store.changes == {}
    region_1 = store.json["trial-reports/2026-W48_region-1_abc123.json"]  # one report per file and code
    assert region_1["records"] == 3 and region_1["sample"][0]["legalName"] == "AMERICAN LEGION POST"  # private sample
    assert store.manifest["records"] == 0 and store.manifest["processed"][0] == "2026-W48 region-1 @abc123"
    assert [e for e in events if e["event"] == "measured"][0]["records"] == 3


def test_a_trial_measures_again_only_for_new_code_or_a_new_publication():
    store, fetched = ReportStore(), []
    trial(store, IRS_ROW, [], fetched=fetched)
    assert len(fetched) == 4
    again = []
    assert trial(store, IRS_ROW, again, fetched=fetched) == "already-done" and len(fetched) == 4  # nothing fetched
    assert trial(store, IRS_ROW, [], code="def456", fetched=fetched) == "done" and len(fetched) == 8  # new code
    assert trial(store, IRS_ROW, [], code="def456", fetched=fetched, today=date(2026, 11, 30)) == "done"  # next week
    assert len(fetched) == 12


def test_production_runs_the_same_code_without_measuring():
    store, events = ReportStore(), []
    run(IRS, irs_gateway(IRS_ROW), store, today=date(2026, 11, 23), gate=lambda: None, allowlist=AllowList.load(),
        log=events.append, stage="production")
    assert not [e for e in events if e["event"] in ("quality", "measured")] and store.json == {}
    assert store.shards and store.manifest["processed"][0] == "2026-W48 region-1"  # no code in production's keys


def test_trials_are_deployed_by_production_s_own_workflow_with_its_commit():
    import yaml
    from pathlib import Path

    wf = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "deploy-ingest-bulk.yml").read_text()
    assert "LC_CODE_VERSION=$GITHUB_SHA" in wf and '"$IMAGE:$GITHUB_SHA"' in wf
    jobs_script = wf.split("python3 - <<'PY'", 1)[1].split("PY", 1)[0]
    # Every ingest-bulk pipeline, trial or production, gets the same image: the stage is only printed, for the label.
    assert '"trial"' not in jobs_script and '"production"' not in jobs_script and 'print(p["job"], s["id"], p["stage"])' in jobs_script
    # A trial dropped from the registry: its job runs once in cleanup mode, then goes. Production jobs never.
    removal = wf.split("Remove trials that left the registry", 1)[1]
    assert "metadata.labels.lc-stage=trial" in removal and "LC_CLEANUP=1 --wait" in removal
    assert removal.index("LC_CLEANUP=1") < removal.index("gcloud run jobs delete")
    assert '--labels "lc-image=ingest-bulk,lc-stage=$stage"' in wf and "read -r job source stage" in wf
    tf = (Path(__file__).resolve().parents[3] / "infra" / "terraform" / "storage.tf").read_text()
    net = tf.split("Source trials keep only small quality reports", 1)[1].split("matches_prefix = [\"tmp/\"]", 1)[0]
    assert "age            = 2" in net and "days_since_noncurrent_time = 1" in net and net.count('["trials/"]') == 2


class Blob:
    def __init__(self, bucket, name, generation, data=b"{}"):
        self.bucket, self.name, self.generation, self.data, self.content_type = bucket, name, generation, data, "application/json"

    def download_as_bytes(self):
        return self.data

    def upload_from_string(self, data, content_type=None):
        self.bucket.add(self.name, data)


class Bucket:
    """A versioned bucket: every write is a new generation; the newest is live."""

    def __init__(self):
        self.objects, self.client, self.generation = [], self, 0

    def add(self, name, data=b"{}"):
        self.generation += 1
        self.objects.append(Blob(self, name, self.generation, data))

    def blob(self, name):
        return Blob(self, name, None)

    def list_blobs(self, bucket, prefix, versions=False):
        found = [b for b in self.objects if b.name.startswith(prefix)]
        if versions:
            return list(found)
        live = {}
        for b in found:
            live[b.name] = b
        return list(live.values())

    def delete_blob(self, name, generation=None):
        self.objects = [b for b in self.objects if not (b.name == name and b.generation == generation)]


def test_a_trial_leaves_nothing_once_removed_and_stays_fresh_while_live():
    from lc_ingest import trials

    bucket = Bucket()
    for name in ("trials/gb-ccew/trial-report.json", "trials/gb-ccew/manifest.json", "trials/gb-ccew/trial-report.json",
                 "trials/gb-ccew-other/trial-report.json", "gb-ccew/manifest.json", "us-irs/manifest.json"):
        bucket.add(name)
    assert trials.refresh(bucket, "gb-ccew") == 2  # the live objects only, each rewritten (a new generation)
    assert len([b for b in bucket.objects if b.name.startswith("trials/gb-ccew/")]) == 5
    assert trials.remove(bucket, "gb-ccew") == 5  # every version
    assert sorted(b.name for b in bucket.objects) == ["gb-ccew/manifest.json", "trials/gb-ccew-other/trial-report.json",
                                                      "us-irs/manifest.json"]  # nothing else touched
    for bad in ("", "../gb", "gb-ccew/", "GB", "a"):
        with pytest.raises(ValueError):
            trials.prefix(bad)


def test_the_cleanup_mode_deletes_the_trial_and_says_so():
    from ingest_bulk.job import cleanup

    bucket, events = Bucket(), []
    bucket.add("trials/gb-ccew/trial-report.json")
    assert cleanup("gb-ccew", bucket, log=events.append) == 0 and bucket.objects == []
    assert events == [{"event": "trial-cleaned", "source": "gb-ccew", "objects": 1, "why": "the trial left the registry"}]


def test_quality_flags_implausible_data():
    from lc_ingest.quality import Quality

    q = Quality("x", today=date(2026, 11, 22))
    base = {"directoryId": "LE1", "legalName": "GOOD CO", "registrationNumber": "123", "legalAddress": {"country": "US"}}
    for record in (base, {**base, "directoryId": "LE1"}, {**base, "directoryId": "LE2", "legalName": "1234"},
                   {**base, "directoryId": "LE3", "legalName": "X", "incorporationDate": "2030-01-01"},
                   {**base, "directoryId": "LE4", "legalName": "OLD", "incorporationDate": "1999-01-01",
                    "dissolutionDate": "1990-01-01"},
                   {**base, "directoryId": "LE5", "legalName": "CALL CO", "otherNames": ["ring +44 20 7946 0000"]},
                   {**base, "directoryId": "LE6", "legalName": "MAIL CO", "website": "info@mail.example.com"}):
        q.add(record)
    a = q.summary()["anomalies"]
    assert a["directory ID repeated in the file"] == 1 and a["name without letters"] == 1
    assert a["name shorter than 2 characters"] == 1 and a["incorporationDate in the future"] == 1
    assert a["dissolved before incorporated"] == 1 and a["contact-like value (email or phone)"] == 2


def test_the_image_carries_the_status_map_a_trial_measures_against():
    from pathlib import Path

    docker = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    for name in ("status.yaml", "labels.yaml"):
        assert f"COPY config/{name} config/{name}" in docker and f"/app/config/{name}" in docker


def test_a_trial_without_the_status_map_still_measures(monkeypatch):
    import lc_core.status

    def broken(*a, **k):
        raise FileNotFoundError("status.yaml")
    monkeypatch.setattr(lc_core.status, "load", broken)
    store, events = ReportStore(), []
    assert trial(store, IRS_ROW, events) == "done"
    q = [e for e in events if e["event"] == "quality"][0]["quality"]
    assert q["records"] == 1 and q["statusUnmapped"] is None  # unknown, never a false "all mapped"
