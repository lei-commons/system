import gzip
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lc_core.control import SystemHalted
from lc_core.fetch import FetchError
from web_claims import candidates as cand
from web_claims.job import CANDIDATES, CLAIMS, PROGRESS, REPORT, run
from web_claims.models import ModelError
from web_claims.settings import load

ROOT = Path(__file__).resolve().parents[3]
COLS = ["directoryId", "lei", "legalName", "registrationNumber", "statusCategory", "website", "country", "otherNames",
        "legalAddress", "sources"]


def settings(**kw):
    s = load(ROOT / "config" / "web_claims.yaml", ROOT / "config" / "models.yaml")
    small = dict(with_website_count=2, without_website_count=4, discovery_truth_sample=1, workers=2,
                 without_website_groups={"UK": ("GB",), "SG": ("SG",)}, max_candidates=4)
    return replace(s, **{**small, **kw})


def entity(i, country, website=None, lei=None, status="active", name=None, number=None):
    return {"directoryId": f"{country}-{i}", "lei": lei, "legalName": name or f"Company {i} Oy",
            "registrationNumber": number or f"{1000000 + i}-{i % 10}", "statusCategory": status, "website": website,
            "country": country, "otherNames": None, "legalAddress": None, "sources": "x"}


class FakeSnapshots:
    def __init__(self, tmp, parts, rels=(), columns=None, kind="changes"):
        self.dir, self.objects = Path(tmp), {}
        files = []
        for n, (name, rows) in enumerate(parts.items()):
            obj = f"parts/{n}.parquet"
            pq.write_table(pa.Table.from_pylist(rows, schema=pa.schema([(c, pa.string()) for c in COLS])), self.dir / f"{n}.parquet")
            self.objects[obj] = self.dir / f"{n}.parquet"
            files.append({"name": name, "kind": "entities", "object": obj})
        if rels:
            pq.write_table(pa.Table.from_pylist(list(rels)), self.dir / "rel.parquet")
            self.objects["parts/rel.parquet"] = self.dir / "rel.parquet"
            files.append({"name": "relationships.p0of1.parquet", "kind": "relationships", "object": "parts/rel.parquet"})
        full = {"schemaVersion": 2, "date": "2026-11-24", "kind": "full", "anchor": "2026-11-24", "files": files,
                "columns": {"entities": columns or COLS}}
        self.json = {"latest.json": {"date": "2026-11-25"}, "2026-11-24/manifest.json": full,
                     "2026-11-25/manifest.json": {"schemaVersion": 2, "date": "2026-11-25", "kind": kind, "anchor": "2026-11-24", "files": []}}

    def read_json(self, name):
        return self.json.get(name)

    def download(self, name, local):
        shutil.copy(self.objects[name], local)


def snapshot(tmp, **kw):
    fi = [entity(i, "FI", website=f"https://www.company{i}.fi", lei=("5493001KJTIIGC8Y1R1" + str(i % 10)) if i < 2 else None)
          for i in range(5)] + [entity(9, "FI", website="https://x.fi", status="dissolved")]
    gb = [entity(i, "GB", name=f"Acme {i} Limited", number=f"0{1234560 + i}") for i in range(4)]
    gb.append(entity(7, "GB", website="https://has.co.uk"))  # has a website: not a without-website candidate
    sg = [entity(i, "SG", name=f"Lion {i} Pte Ltd", number=f"2019{i:05d}K", lei="LION1LEI000000000001" if i == 1 else None)
          for i in range(3)]
    parent = entity(50, "FI", lei="PARENTLEI00000000099", name="Emo Group Oyj", number="7654321-0")
    parts = {"FI.fi-prh.p0of1.parquet": fi, "GB.gb-coh.p0of1.parquet": gb, "SG.sg-acra.p0of1.parquet": sg,
             "FI.fi-prh_gleif.p0of1.parquet": [parent], "DE.gleif.p0of1.parquet": [entity(1, "DE", website="https://de.de")]}
    rels = [{"startNode": "5493001KJTIIGC8Y1R10", "endNode": "PARENTLEI00000000099",
             "relationshipType": cand.DIRECT, "relationshipStatus": "ACTIVE"}]
    return FakeSnapshots(tmp, parts, rels, **kw)


def test_candidates_active_lei_holders_first_and_a_mix(tmp_path):
    picked = cand.select(snapshot(tmp_path), settings())
    recs = picked["records"]
    assert picked["date"] == "2026-11-24"
    with_site = [r for r in recs if r["set"] == cand.WITH]
    assert len(with_site) == 2 and all(r["lei"] for r in with_site)  # LEI holders first
    without = [r for r in recs if r["set"] == cand.WITHOUT]
    assert sorted(r["group"] for r in without) == ["SG", "SG", "UK", "UK"]
    assert not any(r["id"] == "GB-7" for r in recs) and not any(r["id"] == "FI-9" for r in recs)
    assert sum(r["discoveryTruth"] for r in recs) == 1
    fi0 = next(r for r in recs if r["id"] == "FI-0")
    assert fi0["truthParents"]["direct"]["legalName"] == "Emo Group Oyj"


def test_waits_for_a_snapshot_with_the_website_column(tmp_path):
    snaps = snapshot(tmp_path, columns=[c for c in COLS if c != "website"])
    with pytest.raises(cand.NotReady):
        cand.select(snaps, settings())
    events, store = [], MemStore()
    assert run(store, snaps, None, settings(), log=events.append) == "waiting"
    assert store.files == {} and events[-1]["event"] == "web-claims-waiting"


# --- a whole run, with Common Crawl and the models faked ---

def warc(body: bytes) -> bytes:
    http = b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + body
    return gzip.compress(b"WARC/1.0\r\nWARC-Type: response\r\n\r\n" + http)


def site_page(name, number, extra=""):
    return (f"<html><body><p>{name}</p><p>Business ID {number}</p><p>{extra}</p>"
            f"<p>CEO Jane Person, jane.person@example.com, +358 40 123 4567</p></body></html>").encode()


class FakeArchive:
    """index.commoncrawl.org and data.commoncrawl.org: domains -> {path: html}."""

    def __init__(self, sites):
        self.sites, self.asked, self.blobs = sites, [], {}
        for domain, pages in sites.items():
            for path, html in pages.items():
                self.blobs[f"crawl-data/CC-MAIN-2026-44/segments/1/warc/{domain}{path.replace('/', '_')}.warc.gz"] = warc(html)

    def open(self, target, method="GET", headers=None):
        self.asked.append(target)
        if target.endswith("collinfo.json"):
            return Conn(), Resp(b'[{"id": "CC-MAIN-2026-44"}]')
        if target.startswith("https://index.commoncrawl.org/"):
            domain = target.split("url=")[1].split("&")[0]
            if domain not in self.sites:
                raise FetchError("none", 404)
            rows = [json.dumps({"url": f"https://www.{domain}{p}", "timestamp": "20261101000000", "status": "200",
                                "mime": "text/html", "offset": "0", "length": str(len(self.blobs[self._name(domain, p)])),
                                "filename": self._name(domain, p)}) for p in self.sites[domain]]
            return Conn(), Resp("\n".join(rows).encode())
        name = target.split("data.commoncrawl.org/")[1]
        return Conn(), Resp(self.blobs[name])

    @staticmethod
    def _name(domain, path):
        return f"crawl-data/CC-MAIN-2026-44/segments/1/warc/{domain}{path.replace('/', '_')}.warc.gz"


class Resp:
    def __init__(self, data):
        self.data = data

    def read(self, n=-1):
        return self.data if n < 0 else self.data[:n]


class Conn:
    def close(self):
        pass


class MemStore:
    def __init__(self):
        self.files = {}

    def read(self, name):
        return self.files.get(name)

    def write(self, name, data, content_type):
        self.files[name] = data


class FakeMaker:
    def __init__(self, parent=None, fail=False):
        self.parent, self.fail, self.seen = parent, fail, []

    def extract(self, record, domain, excerpts, usage):
        self.seen.append(excerpts)
        if self.fail:
            raise ModelError("maker-call", "NotFoundError 404")
        usage.add(settings().maker, 1000, 100)
        return {"operator": {"legalName": record["legalName"], "registrationNumbers": [record["registrationNumber"]], "lei": None},
                "parent": self.parent}


class FakeChecker:
    def __init__(self, parent_ok=True):
        self.parent_ok, self.seen = parent_ok, []

    def check(self, record, domain, excerpts, claims, usage):
        self.seen.append(excerpts)
        usage.add(settings().checker, 1000, 20)
        return {"operatorSupported": True, "parentSupported": self.parent_ok if claims["parent"] else None}


def archive():
    return FakeArchive({
        "company0.fi": {"/": site_page("Company 0 Oy", "1000000-0", "Company 0 Oy is part of the Emo Group Oyj."),
                        "/yhteystiedot": b"<p>Contact us</p>"},
        "company1.fi": {"/": b"<html><p>Nothing here</p></html>"},
        "acme0.co.uk": {"/": site_page("Acme 0 Limited", "01234560")},
        "lion1.com.sg": {"/": site_page("Lion 1 Pte Ltd", "201900001K")},
    })


def go(tmp_path, maker=None, checker=None, store=None, gw=None, **kw):
    events = []
    store = store if store is not None else MemStore()
    outcome = run(store, snapshot(tmp_path), gw or archive(), settings(**kw), maker=maker, checker=checker,
                  log=events.append, today="2026-11-25", code_version="abc")
    return outcome, store, events


def test_a_whole_run_claims_only_with_evidence_and_stores_no_page_text(tmp_path):
    parent = {"legalName": "Emo Group Oyj", "registrationNumber": None, "lei": None, "country": "FI", "relation": "direct"}
    maker, checker = FakeMaker(parent), FakeChecker()
    outcome, store, events = go(tmp_path, maker, checker)
    assert outcome == "done"
    assert set(store.files) == {CANDIDATES, PROGRESS, CLAIMS, REPORT}
    claims = [json.loads(line) for line in gzip.decompress(store.files[CLAIMS]).decode().splitlines()]
    by = {(c["directoryId"], c["kind"]): c for c in claims}
    assert by[("FI-0", "website")]["value"] == "https://www.company0.fi/"
    assert by[("FI-0", "website")]["matchEvidence"] == "registration-number"
    assert by[("FI-0", "parent")]["value"]["legalName"] == "Emo Group Oyj"
    assert by[("GB-0", "website")]["value"] == "https://www.acme0.co.uk/"  # found from the name
    assert by[("SG-1", "website")]["value"] == "https://www.lion1.com.sg/"
    assert ("FI-1", "website") not in by  # in the archive, but no evidence on the page
    rep = json.loads(store.files[REPORT])
    # the fake maker names the same parent for every site: right for FI-0 (GLEIF agrees), unjudged elsewhere
    assert rep["parents"]["truth"]["correct"] == 1 and "wrong" not in rep["parents"]["truth"]
    assert rep["gates"]["parent"] == {"judged": 1, "precision": 1.0, "bar": 0.98, "passes": True}
    assert rep["withoutWebsite"]["UK"]["found"] == 1 and rep["finished"] is True
    # no person's details anywhere: not stored, not logged, not sent to a model
    everything = b"".join(gzip.decompress(v) if k.endswith(".gz") else v for k, v in store.files.items())
    everything += json.dumps(events).encode() + "".join(maker.seen + checker.seen).encode()
    assert b"Jane" not in everything and b"jane.person" not in everything and b"358 40" not in everything
    assert b"Business ID" not in gzip.decompress(store.files[PROGRESS])  # page text is never kept
    public = next(e for e in events if e["event"] == "web-claims-report")
    assert "sample" not in public and "Emo" not in json.dumps(public)


def test_a_rerun_goes_on_where_the_last_stopped_and_then_only_rewrites(tmp_path):
    clock = iter(range(0, 10**6, 60)).__next__  # each look at the clock is a minute later: a 5-minute run ends early
    store, events = MemStore(), []
    s = settings(run_minutes=5)
    out = run(store, snapshot(tmp_path), archive(), s, log=events.append, clock=clock, today="2026-11-25")
    assert out == "progress"
    first = json.loads(gzip.decompress(store.files[PROGRESS]))["results"]
    assert 0 < len(first) < 6
    out = run(store, snapshot(tmp_path), archive(), settings(), log=events.append, today="2026-11-26")
    assert out == "done"
    again, gw = MemStore(), archive()
    again.files = dict(store.files)
    assert run(again, snapshot(tmp_path), gw, settings(), log=events.append) == "already-done"
    assert gw.asked == [] and set(again.files) == {CANDIDATES, PROGRESS, CLAIMS, REPORT}


def test_model_errors_stop_the_run_and_leave_records_pending(tmp_path):
    outcome, store, events = go(tmp_path, FakeMaker(fail=True), FakeChecker(), max_model_errors_in_a_row=1)
    assert outcome == "model-errors"
    run_event = next(e for e in events if e["event"] == "web-claims-run")
    assert run_event["lastErrorModel"] == "maker-call" and run_event["remaining"] > 0


def test_a_parent_that_might_be_a_person_is_never_kept(tmp_path):
    person = {"legalName": "Jane Person", "registrationNumber": None, "lei": None, "country": None, "relation": "direct"}
    outcome, store, _ = go(tmp_path, FakeMaker(person), FakeChecker())
    claims = gzip.decompress(store.files[CLAIMS]).decode()
    assert "Jane" not in claims and json.loads(store.files[REPORT])["parents"]["guardRejected"] >= 1


def test_spend_cap_stops_model_calls(tmp_path):
    maker = FakeMaker()
    outcome, store, events = go(tmp_path, maker, FakeChecker(), max_model_usd_per_run=0.0)
    assert maker.seen == []  # no model call once the cap is reached


def test_paused_does_nothing(tmp_path):
    def gate():
        raise SystemHalted("paused")

    gw, store = archive(), MemStore()
    assert run(store, snapshot(tmp_path), gw, settings(), gate=gate, log=lambda r: None) == "halted"
    assert gw.asked == [] and store.files == {}


def test_disabled_does_nothing(tmp_path):
    gw, store = archive(), MemStore()
    assert run(store, snapshot(tmp_path), gw, settings(enabled=False), log=lambda r: None) == "disabled"
    assert gw.asked == [] and store.files == {}
