"""The sources fetched with a browser (pipeline.image: ingest-browser), each a small profile: how to tell
which publication is current (one plain request, before any browser starts), the browser flow a person
would follow to get the file, and the reader and mapper for it. Each source's hosts must be approved by
the owner as browser hosts in config/egress-allowlist.yaml; the gateway refuses anything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

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


JAPAN = Profile(
    source="jp-nta", job="ingest-jp", store="jp", hosts=(nta.HOST,), current=nta_current, flow=nta_flow,
    expect_in_name=lambda key: key[:10].replace("-", ""),  # 00_zenkoku_all_20261030.zip for 2026-10-30
    read=nta.read_zip, record=nta.nta_record)

PROFILES = {p.source: p for p in (JAPAN,)}
