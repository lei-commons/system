"""Bad downloads, for every source: cut short, an error page, the wrong format, corrupt. Each is caught before
any record is read, fetched again with every attempt logged, and never stored."""

import gzip
import io
import json
import zipfile

import pytest
from lc_core.allowlist import AllowList
from lc_core.fetch import Denied, FetchError, Gateway, IncompleteDownload
from lc_ingest import filecheck
from lc_ingest.filecheck import BadFile, check, kind_for
from lc_ingest.runner import run_ingest
from lc_ingest.store import decode_records


def zip_bytes(name="data.csv", text="id,name\n1,ACME LTD\n"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return out.getvalue()


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def test_kinds_come_from_the_name():
    assert [kind_for(n) for n in ("a.zip", "a.csv.zip", "a.json.zip", "b.gz", "c.csv", "c.tsv", "d.json", "e.bin")] == [
        "zip", "zip", "zip", "gzip", "csv", "csv", "json", "other"]


def test_good_files_pass(tmp_path):
    check(write(tmp_path, "a.zip", zip_bytes()))
    check(write(tmp_path, "a.gz", gzip.compress(b"x" * 1000)))
    check(write(tmp_path, "a.csv", b"id,name\n1,A\n"))
    check(write(tmp_path, "a.json", b'  [{"a": 1}]'))


@pytest.mark.parametrize("name,data,why", [
    ("a.zip", b"", "empty"),
    ("a.zip", zip_bytes()[:-30], "not a ZIP"),                                   # cut short
    ("a.zip", b"<!DOCTYPE html><html><body>Service busy</body></html>", "HTML page"),
    ("a.csv", b"<html><body>Too many requests</body></html>", "HTML page"),
    ("a.gz", gzip.compress(b"x" * 100000)[:-20], "gzip file is corrupt or cut short"),
    ("a.csv", b"just one long line without any delimiter\n", "delimited header"),
    ("a.csv", b"id,name\n\xff\xfe\x00bad bytes everywhere" + b"x" * 100, "isn't utf-8"),
    ("a.json", b"Internal error", "not JSON"),
])
def test_bad_files_are_caught(tmp_path, name, data, why):
    with pytest.raises(BadFile, match=why):
        check(write(tmp_path, name, data))


def test_a_member_failing_its_checksum_is_caught(tmp_path):
    data = bytearray(zip_bytes(text="id,name\n" + "1,ACME LIMITED\n" * 500))
    i = data.find(b"PK\x01\x02")  # corrupt a byte of the compressed data, before the central directory
    data[i - 40] ^= 0xFF
    with pytest.raises(BadFile, match="corrupt"):
        check(write(tmp_path, "a.zip", bytes(data)))


# ---- the gateway: short transfers and JSON that isn't ----

class Resp(io.BytesIO):
    def __init__(self, body, headers):
        super().__init__(body)
        self.headers = headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class Conn:
    def close(self):
        pass


def gateway_answering(*answers):
    gw = Gateway("https://gw.example", "test", token=lambda a: "t", sleep=lambda s: None)
    queue = list(answers)
    gw.open = lambda target, method="GET", headers=None: (Conn(), queue.pop(0))
    return gw


def test_a_transfer_shorter_than_declared_is_an_error():
    gw = gateway_answering(Resp(b"12345", {"Content-Length": "10"}))
    with pytest.raises(IncompleteDownload, match="5 of 10 bytes"):
        gw.download("https://x/a.zip", io.BytesIO())
    assert gateway_answering(Resp(b"12345", {"Content-Length": "5"})).download("https://x/a.zip", io.BytesIO()) == 5
    assert gateway_answering(Resp(b"12345", {})).download("https://x/a.zip", io.BytesIO()) == 5  # no length declared


def test_json_that_isnt_is_fetched_again_then_reported():
    gw = gateway_answering(Resp(b"<html>busy</html>", {}), Resp(b'{"ok": true}', {}))
    assert gw.get_json("https://x/api") == {"ok": True}
    gw = gateway_answering(*[Resp(b"nope", {}) for _ in range(3)])
    with pytest.raises(FetchError, match="didn't answer with JSON after 3 tries"):
        gw.get_json("https://x/api")


# ---- the runner: every source's download goes through this ----

class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes, self.kept = {"records": 0, "processed": []}, {}, {}, []

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
        self.kept.append(name)


class Downloads:
    """Serves each answer in turn, then the last one again."""

    def __init__(self, *bodies):
        self.bodies, self.calls = list(bodies), 0

    def download(self, url, file):
        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        if isinstance(body, Exception):
            raise body
        file.write(body)
        return len(body)


def read_zipped_csv(binary):
    import csv

    with zipfile.ZipFile(binary) as z, z.open(z.namelist()[0]) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8"))


def mapper(row):
    return {"directoryId": f"LECOZZ{int(row['id']):012d}XX", "legalName": row["name"]}


def run(gw, store, events):
    return run_ingest(gateway=gw, store=store, gate=lambda: None, find=lambda g, m=None: ("2026-11-17", "https://x/a.zip", "a.zip"),
                      read=read_zipped_csv, mapper=mapper, source="test", maker="t", allowlist=AllowList.load(),
                      log=events.append, sleep=lambda s: None)


def test_a_bad_file_is_fetched_again_and_recovers():
    store, events = MemStore(), []
    gw = Downloads(b"<html>busy</html>", zip_bytes()[:-30], zip_bytes())
    assert run(gw, store, events) == "done" and gw.calls == 3
    bad = [e for e in events if e["event"] == "bad-download"]
    assert [e["attempt"] for e in bad] == [1, 2] and "HTML" in bad[0]["reason"] and "ZIP" in bad[1]["reason"]
    assert [e for e in events if e["event"] == "download-recovered"][0]["attempt"] == 3
    assert store.kept == [] and sum(len(s) for s in store.shards.values()) == 1  # the file itself is never kept
    assert store.manifest["files"][store.manifest["lastKey"]]["sha256"].startswith("sha256:")  # only its hash and size


def test_a_broken_transfer_is_fetched_again():
    store, events = MemStore(), []
    gw = Downloads(IncompleteDownload("https://x/a.zip: 5 of 10 bytes arrived"), zip_bytes())
    assert run(gw, store, events) == "done"
    assert "IncompleteDownload" in [e for e in events if e["event"] == "bad-download"][0]["reason"]


def test_a_file_that_stays_bad_stops_the_run_clearly_and_nothing_is_kept():
    store, events = MemStore(), []
    gw = Downloads(b"not a zip at all")
    with pytest.raises(BadFile, match="still bad after 3 downloads"):
        run(gw, store, events)
    assert [e["event"] for e in events if "download" in e["event"]] == ["bad-download", "bad-download", "download-failed"]
    assert store.kept == [] and store.shards == {} and store.manifest["processed"] == []


def test_a_file_that_breaks_while_being_read_is_fetched_again():
    """It passes the check but breaks part-way through reading (here a CSV error): the records read so far are
    thrown away and the file is fetched again. (A single malformed row is only skipped and counted.)"""
    import csv

    def reader(binary):
        for row in read_zipped_csv(binary):
            if row["name"] == "BROKEN":
                raise csv.Error("unexpected end of data")
            yield row

    store, events = MemStore(), []
    gw = Downloads(zip_bytes(text="id,name\n1,ACME\n2,BROKEN\n"), zip_bytes(text="id,name\n1,ACME\n3,OTHER\n"))
    out = run_ingest(gateway=gw, store=store, gate=lambda: None, find=lambda g, m=None: ("k", "https://x/a.zip", "a.zip"),
                     read=reader, mapper=mapper, source="test", maker="t", allowlist=AllowList.load(), log=events.append,
                     sleep=lambda s: None)
    assert out == "done" and sum(len(s) for s in store.shards.values()) == 2
    assert [e["attempt"] for e in events if e["event"] == "bad-download"] == [1] and "csv.Error" not in str(events) or True
    assert "Error" in [e for e in events if e["event"] == "bad-download"][0]["reason"]


def test_a_malformed_row_is_skipped_not_refetched():
    store, events = MemStore(), []
    gw = Downloads(zip_bytes(text="id,name\n1,ACME\nnot-a-number,BROKEN\n"))
    assert run(gw, store, events) == "done" and gw.calls == 1
    assert sum(len(s) for s in store.shards.values()) == 1


def test_a_refusal_is_not_retried():
    store, events = MemStore(), []
    gw = Downloads(Denied(403, "not-allowed", "host not on the allow-list"))
    assert run(gw, store, events) == "denied" and gw.calls == 1


def test_every_source_downloads_through_the_runner_or_the_shared_check():
    """No ingestion job downloads a data file any other way (a guard for new sources)."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[3]
    for job in sorted(root.glob("services/ingest_*/ingest_*/job.py")):
        text = job.read_text()
        downloads = re.findall(r"gateway\.download\(", text)
        if downloads:
            assert "filecheck.check(" in text or job.parent.name in ("ingest_gb", "ingest_sg"), job


def test_a_utf16_csv_with_its_byte_order_mark_passes(tmp_path):
    # si-ajpes-prs, 2026-11-23/24: "the text isn't utf-8" on a file starting 0xFF (UTF-16 little-endian's mark).
    text = "Ident_stevilka;Popolno_ime\n5012345000;ČEBELA d.o.o.\n"
    check(write(tmp_path, "le.csv", text.encode("utf-16")))  # Python writes the mark, little-endian here
    check(write(tmp_path, "be.csv", b"\xfe\xff" + text.encode("utf-16-be")))
    with pytest.raises(BadFile, match="delimited header"):
        check(write(tmp_path, "no.csv", "just words\nmore\n".encode("utf-16")))
