"""The browser fallback for a bulk-file register: only after consistent failures, never around a refusal,
the same file from the public page, the regular reader and mapper."""

import io
import os
import zipfile
from datetime import date

import pytest

from ingest_browser import fallback as fb
from ingest_browser.job import run_fallback
from lc_core.allowlist import AllowList
from lc_core.sources import browser_fallbacks
from lc_ingest.bulk import PROFILES
from lc_ingest.health import HEALTH

from .test_job import Conn, MemStore, Resp

IE_PAGE = ('<a href="/dataset/companies">Companies</a> <a href="https://opendata.cro.ie/dataset/bf6/resource/3fe/download/'
           'companies.csv.zip">Download</a> <a href="https://opendata.cro.ie/dataset/bf6/resource/3fe/download/companies.csv.zip">'
           'Again</a> <a href="https://opendata.cro.ie/other/download/charities.csv.zip">x</a>').encode()
CONFIG = {"source": "ie-cro", "page": "https://opendata.cro.ie/dataset/companies", "link": r"/download/companies\.csv\.zip$",
          "hosts": ["opendata.cro.ie", "nl-ams-1.linodeobjects.com"]}


def allowlist():
    return AllowList.load(os.environ.get("LC_FIELDS_CONFIG")) if os.environ.get("LC_FIELDS_CONFIG") else AllowList.load()


def ie_zip():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("companies.csv", "company_num,company_name,company_status,company_type,company_reg_date\n"
                                    "12345,ACME LIMITED,Normal,LTD - Private Company Limited by Shares,2001-01-01\n")
    return out.getvalue()


class PageGateway:
    def __init__(self, html=IE_PAGE):
        self.html, self.opened = html, []

    def open(self, target, method="GET", headers=None):
        self.opened.append(target)
        return Conn(), Resp(self.html)


def failing_store(failures=3, refused=False):
    store = MemStore()
    store.json = {HEALTH: {"consecutiveFailures": failures, "lastFailure": {"error": "BadFile", "refused": refused,
                                                                             "status": 403 if refused else None}}}
    store.read_json = lambda name: store.json.get(name)
    return store


def browser(name, data):
    calls = []

    def download(flow, gateway, out, hosts, log=print):
        calls.append(hosts)
        log({"event": "browser-downloaded", "file": name})
        out.write(data)
        return len(data)
    return download, calls


def test_ireland_and_australia_have_fallbacks_and_canada_none_and_none_runs_until_the_owner_approves():
    configured = {c["source"] for c in browser_fallbacks(approved_only=False).values()}
    assert configured == {"ie-cro", "au-asic"}
    assert browser_fallbacks() == {} or all(c["source"] in configured for c in browser_fallbacks().values())


def test_the_link_is_found_once_and_the_key_is_the_regular_jobs():
    assert fb.links(IE_PAGE.decode(), CONFIG["page"], CONFIG["link"]) == [
        "https://opendata.cro.ie/dataset/bf6/resource/3fe/download/companies.csv.zip"]
    today = date(2026, 11, 18)
    assert fb.key_for("ie-cro", "https://x/companies.csv.zip", today) == PROFILES["ie-cro"].key(today)
    assert fb.key_for("au-asic", "https://data.gov.au/x/download/company_202611.zip?y=1", today) == "company_202611"


def test_after_consistent_failures_the_browser_fetches_the_same_file_and_the_regular_mapper_reads_it():
    store, events = failing_store(3), []
    download, calls = browser("companies.csv.zip", ie_zip())
    outcome, code = run_fallback("ie-cro", dict(CONFIG), PageGateway(), store, 3, gate=lambda: None, allowlist=allowlist(),
                                 log=events.append, download=download, sleep=lambda s: None, today=date(2026, 11, 18))
    assert (outcome, code) == ("done", 0) and calls == [("opendata.cro.ie", "nl-ams-1.linodeobjects.com")]
    records = [r for shard in store.shards.values() for r in shard.values()]
    assert [r["legalName"] for r in records] == ["ACME LIMITED"]
    assert [e["event"] for e in events if e["event"].startswith("fallback")] == ["fallback-started", "fallback-used"]


def test_a_refusal_never_starts_a_browser():
    download, calls = browser("companies.csv.zip", ie_zip())
    events = []
    outcome, code = run_fallback("ie-cro", dict(CONFIG), PageGateway(), failing_store(9, refused=True), 3, gate=lambda: None,
                                 log=events.append, download=download)
    assert (outcome, code) == ("refused", fb.EXIT_REFUSED) and calls == []
    assert "refused us" in events[0]["reason"]


def test_too_few_failures_never_start_a_browser():
    download, calls = browser("companies.csv.zip", ie_zip())
    assert run_fallback("ie-cro", dict(CONFIG), PageGateway(), failing_store(2), 3, gate=lambda: None, log=lambda e: None,
                        download=download)[1] == fb.EXIT_NOT_YET
    assert calls == []


def test_a_page_without_exactly_one_matching_link_or_the_wrong_file_is_an_error():
    download, _ = browser("companies.csv.zip", ie_zip())
    with pytest.raises(Exception, match="expected one link"):
        run_fallback("ie-cro", dict(CONFIG), PageGateway(b"<p>moved</p>"), failing_store(3), 3, gate=lambda: None,
                     allowlist=allowlist(), log=lambda e: None, download=download, sleep=lambda s: None)
    wrong, _ = browser("charities.csv.zip", ie_zip())
    with pytest.raises(Exception, match="still bad after 3 downloads"):
        run_fallback("ie-cro", dict(CONFIG), PageGateway(), failing_store(3), 3, gate=lambda: None, allowlist=allowlist(),
                     log=lambda e: None, download=wrong, sleep=lambda s: None)


def test_fallback_decisions_go_to_the_decision_log():
    from lc_core.decisions import Recorder

    r = Recorder("job:ingest-ie-browser", log=lambda e: None)
    r({"event": "fallback-skipped", "source": "ie-cro", "reason": "the source refused us (HTTP 403)"})
    r({"event": "fallback-started", "source": "ie-cro", "reason": "3 failed runs in a row"})
    r({"event": "fallback-used", "source": "ie-cro", "outcome": "done"})
    assert [(e.action, e.decision) for e in r.events] == [
        ("considered a browser fallback for", "skipped"), ("fell back to a browser for", "done"), ("fetched with a browser", "done")]
