"""The browser-fetched sources' job: which publication is current, the right file, and the usual runner."""

import csv
import gzip
import io
import json
import os
import zipfile

import pytest

from ingest_browser import job
from ingest_browser.sources import JAPAN, PROFILES
from lc_core.allowlist import AllowList
from lc_core.fetch import FetchError
from lc_core.sources import pipelines
from lc_ingest import nta
from lc_ingest.store import decode_records

def allowlist():
    """As the job loads it: the image names the file (LC_FIELDS_CONFIG); in the repository it's found by path."""
    return AllowList.load(os.environ.get("LC_FIELDS_CONFIG")) if os.environ.get("LC_FIELDS_CONFIG") else AllowList.load()


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
    out = job.run(JAPAN, FakeGateway(), store, gate=lambda: None, allowlist=allowlist(), log=events.append,
                  download=download)
    assert out == "done" and calls == [(nta.HOST,)]
    assert sum(len(s) for s in store.shards.values()) == 3 and store.manifest["processed"] == ["2026-10-30 zenkoku-unicode"]
    assert store.kept == ["jp-nta-2026-10-30.zip"]


def test_an_already_processed_month_never_starts_a_browser():
    download, calls = fake_browser("00_zenkoku_all_20261030.zip", nta_zip(1))
    store = MemStore()
    store.manifest["processed"] = ["2026-10-30 zenkoku-unicode"]
    assert job.run(JAPAN, FakeGateway(), store, gate=lambda: None, allowlist=allowlist(), log=lambda e: None,
                   download=download) == "already-done"
    assert calls == []


def test_a_file_from_another_month_is_refused_and_fetched_again():
    download, calls = fake_browser("00_zenkoku_all_20260929.zip", nta_zip(1))
    events = []
    with pytest.raises(Exception, match="still bad after 3 downloads"):
        job.run(JAPAN, FakeGateway(), MemStore(), gate=lambda: None, allowlist=allowlist(), log=events.append,
                download=download, sleep=lambda s: None)
    assert len(calls) == 3
    assert "not the publication of 2026-10-30" in [e for e in events if e["event"] == "download-failed"][0]["reason"]


def test_a_missing_file_name_is_an_error():
    download, _ = fake_browser("", b"x")
    with pytest.raises(FetchError, match="no file"):
        job.fetch_for(JAPAN, "2026-10-30 zenkoku-unicode", log=lambda e: None, download=download)(None, "", io.BytesIO())


# ---- daily difference files ----

DIFFS = {"enabled": True, "page": "https://www.houjin-bangou.nta.go.jp/download/sabun/", "section": "CSV形式・Unicode",
         "file_date_format": "%Y%m%d", "max_per_run": 5, "overlap_days": 3}


def diff_page(*days):
    rows = "".join(f'<tr><td>令和8年{int(d[5:7])}月{int(d[8:])}日</td><td><a onclick="return doDownload({30000 + i});">zip</a></td></tr>'
                   for i, d in enumerate(days))
    return f"<h2>CSV形式・Shift_JIS</h2><table></table><h2>CSV形式・Unicode</h2><table>{rows}</table>".encode()


def diff_zip(*rows):
    text = io.StringIO()
    w = csv.writer(text, lineterminator="\r\n")
    for over in rows:
        values = dict.fromkeys(nta.COLUMNS, "")
        values.update(sequenceNumber="1", process="12", corporateNumber="3010401016070", kind="301", name="株式会社テスト0",
                      prefectureName="東京都", cityName="千代田区", streetNumber="丸の内1-1", postCode="1000005", latest="1",
                      hihyoji="0")
        values.update(over)
        w.writerow([values[c] for c in nta.COLUMNS])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("diff.csv", text.getvalue())
    return out.getvalue()


class DiffGateway(FakeGateway):
    def __init__(self, page):
        super().__init__()
        self.page = page

    def open(self, target, method="GET", headers=None):
        self.opened.append(target)
        return Conn(), Resp(self.page if target == DIFFS["page"] else PAGE_HTML)


def queued_browser(files):
    """Each browser download takes the next (name, data); the flow is kept to check what was clicked."""
    files, flows = list(files), []

    def download(flow, gateway, out, hosts, log=print):
        flows.append(flow)
        name, data = files.pop(0)
        log({"event": "browser-downloaded", "file": name, "bytes": len(data)})
        out.write(data)
        return len(data)
    return download, flows


def loaded_store():
    """A store holding the 2026-10-30 full file (3 corporations)."""
    store = MemStore()
    download, _ = fake_browser("00_zenkoku_all_20261030.zip", nta_zip(3))
    assert job.run(JAPAN, FakeGateway(), store, gate=lambda: None, allowlist=allowlist(), log=lambda e: None,
                   download=download) == "done"
    return store


def held(store):
    return {r["registrationNumber"]: r for s in store.shards.values() for r in s.values()}


def test_diffs_wait_for_a_full_file():
    events = []
    out = job.run_diffs(JAPAN, DIFFS, DiffGateway(diff_page("2026-11-02")), MemStore(), gate=lambda: None,
                        allowlist=allowlist(), log=events.append, download=queued_browser([])[0])
    assert out == "waiting" and events[-1]["event"] == "diffs-waiting"


def test_the_days_after_the_full_file_are_applied_oldest_first_and_each_once():
    store = loaded_store()
    page = diff_page("2026-10-20", "2026-10-28", "2026-11-02", "2026-11-04")  # 10-20: before the overlap window
    download, flows = queued_browser([
        ("diff_20261028.zip", diff_zip({"streetNumber": "丸の内2-2"})),
        ("diff_20261102.zip", diff_zip({"process": "11", "name": "株式会社新名", "streetNumber": "丸の内2-2"},
                                       {"corporateNumber": "3010401016080", "process": "21", "closeDate": "2026-11-01", "closeCause": "01"},
                                       {"corporateNumber": "3010401016090", "process": "99"},
                                       {"corporateNumber": "9999999999999", "process": "99"},
                                       {"corporateNumber": "5010001000001", "process": "01", "name": "株式会社新設"})),
        ("diff_20261104.zip", diff_zip({"process": "11", "name": "株式会社旧名", "latest": "0"})),
    ])
    events = []
    out = job.run_diffs(JAPAN, DIFFS, DiffGateway(page), store, gate=lambda: None, allowlist=allowlist(),
                        log=events.append, download=download)
    assert out == "done" and len(flows) == 3
    records = held(store)
    # moved on 10-28, renamed on 11-02 (each row is the whole state after the change); 11-04's earlier state is skipped
    assert records["3010401016070"]["legalName"] == "株式会社新名"
    assert records["3010401016070"]["legalAddress"]["addressLines"] == ["丸の内2-2"]
    assert records["3010401016080"]["legalStatus"] == "Closed: liquidation completed"
    assert records["3010401016090"]["labels"] == [nta.ABSENT] and records["3010401016090"]["legalName"]  # kept
    assert "9999999999999" not in records and records["5010001000001"]["legalStatus"] == "Active"
    assert store.manifest["records"] == 4
    assert store.manifest["processed"][1:] == ["2026-10-28 diff-unicode on 2026-10-30",
                                              "2026-11-02 diff-unicode on 2026-10-30",
                                              "2026-11-04 diff-unicode on 2026-10-30"]
    assert store.kept[1:] == ["jp-nta-diff-2026-10-28-on-2026-10-30.zip", "jp-nta-diff-2026-11-02-on-2026-10-30.zip",
                              "jp-nta-diff-2026-11-04-on-2026-10-30.zip"]
    assert events[-1] == {"event": "diffs", "source": "jp-nta", "base": "2026-10-30", "applied": 3, "pending": 0,
                          "outcome": "done"}
    # the next run finds nothing new and starts no browser
    again, flows2 = queued_browser([])
    assert job.run_diffs(JAPAN, DIFFS, DiffGateway(page), store, gate=lambda: None, allowlist=allowlist(),
                         log=lambda e: None, download=again) == "already-done" and flows2 == []


def test_a_new_full_file_supersedes_the_diffs_before_it_and_applies_the_days_around_it_again():
    store = loaded_store()
    store.manifest["processed"] += ["2026-11-02 diff-unicode on 2026-10-30", "2026-11-27 diff-unicode on 2026-10-30",
                                    "2026-11-28 zenkoku-unicode"]
    page = diff_page("2026-11-02", "2026-11-27", "2026-12-01")
    download, flows = queued_browser([("diff_20261127.zip", diff_zip({})), ("diff_20261201.zip", diff_zip({}))])
    assert job.run_diffs(JAPAN, DIFFS, DiffGateway(page), store, gate=lambda: None, allowlist=allowlist(),
                         log=lambda e: None, download=download) == "done"
    assert len(flows) == 2 and store.manifest["processed"][-2:] == ["2026-11-27 diff-unicode on 2026-11-28",
                                                                    "2026-12-01 diff-unicode on 2026-11-28"]


def test_a_run_takes_at_most_max_per_run_files_and_the_rest_wait():
    store = loaded_store()
    download, flows = queued_browser([("diff_20261102.zip", diff_zip({})), ("diff_20261104.zip", diff_zip({}))])
    events = []
    assert job.run_diffs(JAPAN, {**DIFFS, "max_per_run": 2}, DiffGateway(diff_page("2026-11-02", "2026-11-04", "2026-11-05")),
                         store, gate=lambda: None, allowlist=allowlist(), log=events.append, download=download) == "done"
    assert len(flows) == 2 and events[-1]["applied"] == 2 and events[-1]["pending"] == 1


def test_a_wrong_days_file_is_refused_and_the_later_days_wait():
    store = loaded_store()
    download, flows = queued_browser([("diff_20261101.zip", diff_zip({}))] * 3)
    with pytest.raises(Exception, match="still bad after 3 downloads"):
        job.run_diffs(JAPAN, DIFFS, DiffGateway(diff_page("2026-11-02", "2026-11-04")), store, gate=lambda: None,
                      allowlist=allowlist(), log=lambda e: None, download=download, sleep=lambda s: None)
    assert len(flows) == 3 and store.manifest["processed"] == ["2026-10-30 zenkoku-unicode"]


def test_the_diff_page_must_be_on_the_sources_own_host():
    with pytest.raises(ValueError, match="own host"):
        job.run_diffs(JAPAN, {**DIFFS, "page": "https://elsewhere.example/sabun/"}, DiffGateway(b""), MemStore(),
                      gate=lambda: None, allowlist=allowlist(), log=lambda e: None)


def test_a_halted_system_reads_nothing():
    from lc_core.control import SystemHalted

    def gate():
        raise SystemHalted("paused")
    gw = DiffGateway(diff_page("2026-11-02"))
    assert job.run_diffs(JAPAN, DIFFS, gw, loaded_store(), gate=gate, allowlist=allowlist(), log=lambda e: None) == "halted"
    assert gw.opened == []
