"""The sources fetched with a browser (pipeline.image: ingest-browser), each a small profile: how to tell
which publication is current (one plain request, before any browser starts), the browser flow a person
would follow to get the file, and the reader and mapper for it. Each source's hosts must be approved by
the owner as browser hosts in config/egress-allowlist.yaml; the gateway refuses anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from lc_core.fetch import FetchError
from lc_ingest import nta


@dataclass(frozen=True)
class Profile:
    source: str
    job: str
    store: str
    hosts: tuple
    current: Callable      # gateway -> (key, url, filename): which publication, found with one plain request
    flow: Callable         # (page, step, timeout_ms) -> playwright Download
    expect_in_name: Callable  # key -> text the downloaded file's name must contain (the right publication)
    read: Callable
    record: Callable
    daily: Optional["Daily"] = None  # the source's daily difference files, if it has them


@dataclass(frozen=True)
class Daily:
    """A source's daily difference files, applied on top of its latest full file (pipeline.daily_diffs)."""
    base: Callable         # manifest["processed"] -> the date of the full file they apply on, or None
    listing: Callable      # (gateway, config) -> [{day, number, label}]: the files on offer, one plain request
    key: Callable          # (day, base) -> publish key
    flow: Callable         # (config, row) -> flow(page, step, timeout_ms) -> playwright Download
    read: Callable
    record: Callable
    prepare: Callable      # the runner's prepare hook


# ---- Japan: National Tax Agency corporate numbers ----

FIND_UNICODE_NATIONWIDE = """() => {
  const heading = [...document.querySelectorAll('h2, h3')].find(h => h.textContent.includes('CSV形式・Unicode'));
  if (!heading) return null;
  const links = [...document.querySelectorAll('[onclick^="return doDownload("]')];
  const first = links.find(a => heading.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING);
  if (!first) return null;
  const row = first.closest('tr');
  if (!row || !row.textContent.includes('全国')) return null;
  return first.getAttribute('onclick').match(/doDownload\\((\\d+)\\)/)[1];
}"""


def nta_current(gateway):
    """The date of the current nationwide Unicode CSV, read from the download page (a plain GET)."""
    conn, resp = gateway.open(nta.PAGE)
    try:
        html = resp.read(2_000_000).decode("utf-8", "replace")
    finally:
        conn.close()
    day = nta.page_date(html)
    return f"{day} zenkoku-unicode", nta.PAGE, f"jp-nta-{day}.zip"


def nta_flow(page, step, timeout_ms):
    """What a person does: open the page, pick 全国 under CSV形式・Unicode, and save the file."""
    step(page, "open the NTA full-data download page")
    page.goto(nta.PAGE, wait_until="domcontentloaded")
    step(page, "choose the nationwide file, CSV (Unicode)")
    number = page.evaluate(FIND_UNICODE_NATIONWIDE)
    if not number:
        raise FetchError("the nationwide Unicode CSV isn't on the NTA download page")
    with page.expect_download(timeout=timeout_ms) as info:
        page.click(f'[onclick="return doDownload({number});"]')
    return info.value


# The daily difference files: the row of the day's file under the configured section, found by its date
# as the page writes it (lc_ingest.nta.daily_files read the same page with one plain request).
FIND_DAILY_FILE = """({section, label}) => {
  const squash = s => (s || '').replace(/\\s+/g, '');
  const heads = [...document.querySelectorAll('h1, h2, h3, h4, h5, h6')];
  const i = heads.findIndex(h => squash(h.textContent).includes(squash(section)));
  if (i < 0) return null;
  const head = heads[i], next = heads[i + 1];
  const links = [...document.querySelectorAll('[onclick^="return doDownload("]')].filter(a =>
    (head.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING) &&
    (!next || (next.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_PRECEDING)));
  const hit = links.find(a => { const row = a.closest('tr'); return row && squash(row.textContent).includes(label); });
  return hit ? hit.getAttribute('onclick').match(/doDownload\\((\\d+)\\)/)[1] : null;
}"""


def nta_daily_listing(gateway, config):
    """The daily files on offer, read from the diff page with one plain GET (no browser)."""
    conn, resp = gateway.open(config["page"])
    try:
        html = resp.read(2_000_000).decode("utf-8", "replace")
    finally:
        conn.close()
    return nta.daily_files(html, config["section"])


def nta_daily_flow(config, row):
    """What a person does: open the diff page, find the day's file under the section, and save it."""
    def flow(page, step, timeout_ms):
        step(page, "open the NTA daily-diff download page")
        page.goto(config["page"], wait_until="domcontentloaded")
        step(page, f"choose the file of {row['day']}, {config['section']}")
        number = page.evaluate(FIND_DAILY_FILE, {"section": config["section"], "label": row["label"]})
        if not number:
            raise FetchError(f"the daily file of {row['day']} isn't under {config['section']} on the NTA page")
        with page.expect_download(timeout=timeout_ms) as info:
            page.click(f'[onclick="return doDownload({number});"]')
        return info.value
    return flow


JAPAN = Profile(
    source="jp-nta", job="ingest-jp", store="jp", hosts=(nta.HOST,), current=nta_current, flow=nta_flow,
    expect_in_name=lambda key: key[:10].replace("-", ""),  # 00_zenkoku_all_20261030.zip for 2026-10-30
    read=nta.read_zip, record=nta.nta_record,
    daily=Daily(base=nta.latest_full, listing=nta_daily_listing, key=nta.diff_key, flow=nta_daily_flow,
                read=nta.read_zip, record=nta.nta_diff_record, prepare=nta.keep_known_withdrawn))

PROFILES = {p.source: p for p in (JAPAN,)}
