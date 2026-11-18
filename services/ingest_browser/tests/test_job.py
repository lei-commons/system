"""The browser-fetched sources' job: which publication is current, the right file, and the usual runner."""

import csv
import gzip
import io
import json
import zipfile

import pytest

from ingest_browser import job
from ingest_browser.sources import JAPAN, PROFILES
from lc_core.allowlist import AllowList
from lc_core.fetch import FetchError
from lc_core.sources import pipelines
from lc_ingest import nta
from lc_ingest.store import decode_records

PAGE_HTML = ("<h2>CSV形式・Shift_JIS</h2> 令和8年10月30日更新 <h2>CSV形式・Unicode</h2> 令和8年10月30日更新 "
             "<table><tr><td>全国</td><td><a onclick=\"return doDownload(28341);\">zip</a></td></tr></table>").encode()


def nta_zip(n=3):
    text = io.StringIO()
    w = csv.writer(text, lineterminator="\r\n")
    for i in range(n):
        values = dict.fromkeys(nta.COLUMNS, "")
        values.update(sequenceNumber=str(i + 1), corporateNumber=f"{3010401016070 + i * 10}", kind="301",
                      name=f"株式会社テスト{i}", prefectureName="東京都", cityName="千代田区", streetNumber="丸の内1-1",
                      postCode="1000005", latest="1", hihyoji="0")
        w.writerow([values[c] for c in nta.COLUMNS])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("00_zenkoku_all_20261030.csv", text.getvalue())
    return out.getvalue()


class Resp(io.BytesIO):
    pass


class Conn:
    def close(self):
        pass


class FakeGateway:
    def __init__(self):
        self.opened = []

    def open(self, target, method="GET", headers=None):
        self.opened.append(target)
        return Conn(), Resp(PAGE_HTML)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.kept = {"records": 0, "processed": []}, {}, []

    def read_manifest(self):
        return json.loads(json.dumps(self.manifest))

    def write_manifest(self, m):
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard_file(self, n, path):
        self.shards[n] = decode_records(open(path, "rb").read())

    def write_changes_file(self, name, path):
        with gzip.open(path, "rt") as f:
            f.read()

    def keep_source(self, name, path):
        self.kept.append(name)


def fake_browser(name, data):
    calls = []

    def download(flow, gateway, out, hosts, log=print):
        calls.append(hosts)
        log({"event": "browser-downloaded", "file": name, "bytes": len(data)})
        out.write(data)
        return len(data)
    return download, calls


def test_every_browser_profile_has_a_registry_pipeline_on_this_image():
    by_id = {p["id"]: p for p in pipelines()}
    for source, profile in PROFILES.items():
        assert by_id[source]["job"] == profile.job and by_id[source]["store"] == profile.store


def test_the_current_publication_comes_from_the_page_without_a_browser():
    gw = FakeGateway()
    key, url, filename = JAPAN.current(gw)
    assert key == "2026-10-30 zenkoku-unicode" and filename == "jp-nta-2026-10-30.zip" and gw.opened == [nta.PAGE]


def test_a_run_downloads_with_the_browser_and_stores_the_records():
    download, calls = fake_browser("00_zenkoku_all_20261030.zip", nta_zip(3))
    store, events = MemStore(), []
    out = job.run(JAPAN, FakeGateway(), store, gate=lambda: None, allowlist=AllowList.load(), log=events.append,
                  download=download)
    assert out == "done" and calls == [(nta.HOST,)]
    assert sum(len(s) for s in store.shards.values()) == 3 and store.manifest["processed"] == ["2026-10-30 zenkoku-unicode"]
    assert store.kept == ["jp-nta-2026-10-30.zip"]


def test_an_already_processed_month_never_starts_a_browser():
    download, calls = fake_browser("00_zenkoku_all_20261030.zip", nta_zip(1))
    store = MemStore()
    store.manifest["processed"] = ["2026-10-30 zenkoku-unicode"]
    assert job.run(JAPAN, FakeGateway(), store, gate=lambda: None, allowlist=AllowList.load(), log=lambda e: None,
                   download=download) == "already-done"
    assert calls == []


def test_a_file_from_another_month_is_refused_and_fetched_again():
    download, calls = fake_browser("00_zenkoku_all_20260929.zip", nta_zip(1))
    events = []
    with pytest.raises(Exception, match="still bad after 3 downloads"):
        job.run(JAPAN, FakeGateway(), MemStore(), gate=lambda: None, allowlist=AllowList.load(), log=events.append,
                download=download, sleep=lambda s: None)
    assert len(calls) == 3
    assert "not the publication of 2026-10-30" in [e for e in events if e["event"] == "download-failed"][0]["reason"]


def test_a_missing_file_name_is_an_error():
    download, _ = fake_browser("", b"x")
    with pytest.raises(FetchError, match="no file"):
        job.fetch_for(JAPAN, "2026-10-30 zenkoku-unicode", log=lambda e: None, download=download)(None, "", io.BytesIO())
