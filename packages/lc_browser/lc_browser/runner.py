"""Run a source's browser flow: a real headless Chromium, every request through the gateway (relay.py).

A flow is a small function for one source: ``flow(page, step) -> Download`` that opens the page, finds
the file a person would pick and clicks it. ``step`` pauses like a person (5-15 s by default) and checks
the page first: a CAPTCHA, a login form or any other access control stops the run, recorded as the
source refusing us (Article 2.1: nothing behind an access control). Images, fonts, media and stylesheets are never
fetched. The download is saved to the file the ingestion runner gave (lc_ingest.runner's ``fetch``
hook), so the usual file checks, retries and logging apply.
"""

from __future__ import annotations

import random
import shutil
import time

from lc_core.fetch import FetchError

from .relay import Relay

SKIP_TYPES = {"image", "font", "media", "stylesheet"}  # never needed to find and click a link
# Signs that a page is behind an access control: we stop rather than go around it.
GUARD_SELECTORS = [
    "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", "iframe[src*='challenges.cloudflare.com']",
    "[class*='g-recaptcha']", "[class*='h-captcha']", "[class*='cf-turnstile']", "input[type='password']",
]


class AccessControl(FetchError):
    """The page asks for a CAPTCHA, a login or similar: an outside refusal (status 403), never worked around."""

    def __init__(self, what: str):
        super().__init__(f"the page is behind an access control ({what}); not worked around (Article 2.1)", 403)


class Steps:
    """Pauses like a person between steps, and checks each page for an access control first."""

    def __init__(self, log=print, think=(5.0, 15.0), sleep=time.sleep, rand=random.uniform):
        self.log, self.think, self._sleep, self._rand = log, think, sleep, rand
        self.count = 0

    def __call__(self, page, what: str) -> None:
        self.guard(page)
        if self.count:
            self._sleep(self._rand(*self.think))
        self.count += 1
        self.log({"event": "browser-step", "step": what})

    @staticmethod
    def guard(page) -> None:
        for selector in GUARD_SELECTORS:
            if page.query_selector(selector) is not None:
                raise AccessControl(selector)
        title = (page.title() or "").lower()
        if "captcha" in title or "verify you are human" in title:
            raise AccessControl("a CAPTCHA page")


def download(flow, gateway, out, hosts, log=print, think=(5.0, 15.0), sleep=time.sleep, timeout_s=3600,
             launch=None) -> int:
    """Run ``flow`` in a headless browser and write the file it downloads to the open binary file ``out``.
    Returns the bytes written. ``hosts`` are the only hosts the browser may reach (the source's own)."""
    relay = Relay(gateway, hosts, log=log, actor=f"{getattr(gateway, '_actor', 'lc-browser')}/browser").start()
    try:
        with (launch or _launch)(relay.proxy) as context:
            context.route("**/*", lambda route: route.abort() if route.request.resource_type in SKIP_TYPES
                          else route.continue_())
            page = context.new_page()
            page.set_default_timeout(120_000)
            steps = Steps(log=log, think=think, sleep=sleep)
            dl = flow(page, steps, timeout_s * 1000)
            failure = dl.failure()
            if failure:
                raise FetchError(f"the browser download failed: {failure}")
            start = out.tell()
            with open(dl.path(), "rb") as f:
                shutil.copyfileobj(f, out, 1 << 20)
            size = out.tell() - start
            log({"event": "browser-downloaded", "file": dl.suggested_filename, "bytes": size,
                 "requests": relay.requests, "refused": relay.refused, "steps": steps.count})
            return size
    finally:
        relay.stop()


class _launch:
    """Headless Chromium through the relay; its TLS to the relay is accepted (the site's real certificate
    is checked by the gateway)."""

    def __init__(self, proxy: str):
        self.proxy = proxy

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True, proxy={"server": self.proxy},
                                                 args=["--disable-quic", "--disable-background-networking"])
        return self._browser.new_context(ignore_https_errors=True, accept_downloads=True, locale="en-GB",
                                         service_workers="block")

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()
