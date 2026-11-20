"""The daily-diff flow in a real headless Chromium, offline: the page is served by Playwright's router (no
request leaves the machine), and its doDownload(n) saves a file named after n, so the test sees which row
was clicked. The same page is read by lc_ingest.nta.daily_files, so the two must agree on the row."""

import pytest

from ingest_browser.sources import FIND_DAILY_FILE, nta_daily_flow
from lc_core.fetch import FetchError
from lc_ingest import nta

sync_api = pytest.importorskip("playwright.sync_api")

CONFIG = {"page": "https://www.houjin-bangou.nta.go.jp/download/sabun/", "section": "CSV形式・Unicode"}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>差分データ</title></head><body>
<h2>CSV形式・Shift_JIS</h2><table>
<tr><td>令和8年11月19日</td><td><a href="#" onclick="return doDownload(30001);">zip</a></td></tr></table>
<h2 class="title">CSV形式・<span>Unicode</span></h2><table>
<tr><td>令和8年11月18日</td><td><a href="#" onclick="return doDownload(30102);">zip 13KB</a></td></tr>
<tr><td>令和8年 11月 19日</td><td><a href="#" onclick="return doDownload(30103);">zip 14KB</a></td></tr></table>
<h2>XML形式・Unicode</h2><table>
<tr><td>令和8年11月20日</td><td><a href="#" onclick="return doDownload(30201);">zip</a></td></tr></table>
<script>function doDownload(n) {
  const a = document.createElement('a');
  a.href = 'data:application/zip;base64,UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==';
  a.download = 'diff_' + n + '.zip'; document.body.appendChild(a); a.click(); return false; }</script>
</body></html>"""


@pytest.fixture
def page():
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        context.route("**/*", lambda route: route.fulfill(status=200, body=PAGE, content_type="text/html; charset=utf-8")
                      if route.request.url == CONFIG["page"] else route.abort())
        yield context.new_page()
        browser.close()


def test_the_browser_and_the_plain_read_pick_the_same_row(page):
    page.goto(CONFIG["page"])
    for row in nta.daily_files(PAGE, CONFIG["section"]):
        assert page.evaluate(FIND_DAILY_FILE, {"section": CONFIG["section"], "label": row["label"]}) == row["number"]
    # a day only in another section is not found under this one
    assert page.evaluate(FIND_DAILY_FILE, {"section": CONFIG["section"], "label": "令和8年11月20日"}) is None


def test_the_flow_clicks_the_days_file_and_saves_it(page):
    row = {"day": "2026-11-19", "label": "令和8年11月19日"}
    steps = []
    download = nta_daily_flow(CONFIG, row)(page, lambda p, what: steps.append(what), 30_000)
    assert download.suggested_filename == "diff_30103.zip"
    assert steps == ["open the NTA daily-diff download page", "choose the file of 2026-11-19, CSV形式・Unicode"]


def test_a_day_not_on_the_page_is_an_error(page):
    with pytest.raises(FetchError, match="2026-11-21"):
        nta_daily_flow(CONFIG, {"day": "2026-11-21", "label": "令和8年11月21日"})(page, lambda p, what: None, 30_000)
