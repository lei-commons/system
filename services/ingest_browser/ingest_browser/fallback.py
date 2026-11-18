"""Browser fallback (owner decision 2026-11-18): when a source's regular download fails consistently, and
not because the source refused us, a real browser fetches the same file from the source's public download
page, as a person would, and the regular reader and mapper process it.

Configured per source in the registry (pipeline.browser_fallback: page, link pattern, hosts) and active
only once the owner has approved those hosts as browser hosts (lc_core.sources.browser_fallbacks). The
daily cycle runs a source's fallback job (<job>-browser) after its retries are spent; the job first reads
the source's health (lc_ingest.health): it goes ahead only after browser_fallback_after_failures failed
runs in a row (config/cycle.yaml) and never when the last failure was a refusal (Article 2.1).
"""

from __future__ import annotations

import re
from datetime import date

from lc_core.fetch import FetchError
from lc_ingest.bulk import PROFILES as BULK

EXIT_REFUSED, EXIT_NOT_YET = 3, 4


def links(html: str, base: str, pattern: str) -> list:
    """The distinct links on a page whose address (without its query) matches ``pattern``."""
    from urllib.parse import urljoin

    rx = re.compile(pattern)
    found = []
    for href in re.findall(r'href="([^"]+)"', html):
        url = urljoin(base, href.replace("&amp;", "&"))
        if rx.search(url.split("?", 1)[0]) and url not in found:
            found.append(url)
    return found


def key_for(source: str, link: str, today: date) -> str:
    """The same key the regular job uses, so a file is never processed twice: the register's week, or for a
    register whose file moves with each publication, the file's own name (as its catalogue gives it)."""
    profile = BULK[source]
    if profile.locate:
        return link.split("?", 1)[0].rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return profile.key(today)


def find_for(source: str, config: dict, today: date):
    """The runner's ``find``: one plain read of the public page for the file's link (no browser yet)."""
    profile = BULK[source]

    def find(gateway, manifest=None):
        conn, resp = gateway.open(config["page"])
        try:
            html = resp.read(5_000_000).decode("utf-8", "replace")
        finally:
            conn.close()
        found = links(html, config["page"], config["link"])
        if len(found) != 1:
            raise FetchError(f"expected one link matching {config['link']} on {config['page']}, found {len(found)}")
        key = key_for(source, found[0], today)
        config["_link"] = found[0]
        return key, config["page"], f"{profile.filename}-{key}{profile.extension}"
    return find


def link_flow(config: dict):
    """What a person does: open the page and click the file's link."""
    def flow(page, step, timeout_ms):
        step(page, "open the public download page")
        page.goto(config["page"], wait_until="domcontentloaded")
        step(page, "click the file's link")
        rx = re.compile(config["link"])
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        index = next((i for i, h in enumerate(hrefs) if rx.search(h.split("?", 1)[0])), None)
        if index is None:
            raise FetchError(f"no link matching {config['link']} on the page")
        with page.expect_download(timeout=timeout_ms) as info:
            page.locator("a[href]").nth(index).click()
        return info.value
    return flow
