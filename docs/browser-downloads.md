# Browser downloads

Owner decision 2026-11-18. Some official registers offer their public data only through a web page or form: a person opens the page, clicks a button, the site posts a form with a one-time token and a session cookie, and the file downloads. There is no login, no CAPTCHA and nothing else behind an access control, so a real browser fetches the file. Japan's National Tax Agency is the first such source (owner exception 2026-11-16).

## What it is, and what it isn't

- **For:** sources whose public download is a page or form flow. Each source's hosts are approved by the owner as `browser_hosts` in `config/egress-allowlist.yaml`, a protected file, and the source has its own flow in code.
- **Not for:** getting past a refusal. If a site blocks us (a 403, a firewall rule, a CAPTCHA, a login), the browser path is never used to get around it. Article 2.1 forbids that, and it would only make the block worse. The run stops and records the source refusing us; the owner asks the source to allow our fixed address. (GLEIF, 2026-11-18: an address-based 403 on its file storage. A browser from the same address is refused just the same.)

## How it works

```
Cloud Run job (sealed lc-jobs network)                    conduct gateway (lc-egress, fixed address)
  headless Chromium ──CONNECT──▶ relay (same container) ──/fetch, X-LC-Target──▶ policy ─▶ the site
```

- **The relay** (`lc_browser/relay.py`) is the browser's proxy. It ends the browser's TLS inside the container with a certificate made for that run; the browser context accepts it, and the real site's certificate is checked by the gateway. Each request becomes an ordinary gateway `/fetch` call, so the allow-list, robots.txt, pacing, the fixed address and the honest User-Agent all apply. Hosts not approved for the source never leave the container.
- **The gateway's browser profile** (`conduct_proxy/policy.py`) allows GET, HEAD and form POSTs to approved browser hosts. It passes the site's own session cookies both ways and never sends credentials. Login paths are still refused and robots.txt is honoured. The User-Agent is the browser's own plus `lei-commons-browser/0.1 (+https://github.com/lei-commons)`. Pacing is one request at a time per host, 1–3 s apart within a page, and 120 s after an error.
- **The runner** (`lc_browser/runner.py`) starts Chromium with the relay and runs the source's flow. Between steps it pauses like a person (5–15 s). Images, fonts, media and stylesheets are never fetched. Before each step it checks the page for a CAPTCHA, a login form or similar; any of them stops the run with `AccessControl`, recorded as a refusal (HTTP 403).
- **The job** (`services/ingest_browser`) first reads which publication is current with one plain request. If that publication is already processed, it stops without starting a browser. Otherwise the browser's download goes through the usual ingestion runner (`fetch` hook): file checks, retries, the volume guard and change files. The file's name must match the publication found, or it is fetched again.

## Adding a source

1. The owner approves the source and its hosts as browser hosts (`browser_hosts`, with `approved`).
2. Add a `Profile` to `services/ingest_browser/ingest_browser/sources.py`: `current` (one plain request to find the publication), the `flow` (what a person clicks), `expect_in_name`, and the reader and mapper (in `lc_ingest`).
3. Give the registry entry a pipeline with `image: ingest-browser`. `deploy-ingest-browser.yml` creates the job, and `deploy-cycle.yml` adds it to the daily cycle.

## Japan (jp-nta)

The download page is `https://www.houjin-bangou.nta.go.jp/download/zenken/`. The flow picks 全国 (nationwide) under CSV形式・Unicode. The file `00_zenkoku_all_YYYYMMDD.zip` is updated monthly and is about 270 MB. It holds a headerless 30-column CSV following the NTA's specification, and `lc_ingest/nta.py` reads it. Records are keyed with GLEIF's RA001075 (Corporate Number Publication Site) and the 13-digit corporate number. LEI holders that GLEIF lists under RA000412 (commercial registration, the corporate number without its check digit) merge once the GLEIF re-key adds that alias.
