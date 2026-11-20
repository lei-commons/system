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

## Browser fallback (owner decision 2026-11-18)

A source whose regular download fails consistently, for a reason a browser can genuinely fix (the file moved, the link is now built by the page, a page arrives instead of the file), is fetched once by a browser from its public download page, as a person would. The regular reader and mapper then process it.

- **Never around a refusal.** If the source refused us (401, 403, 429 or 451, a CAPTCHA or a login), the fallback doesn't run. The failure is recorded as a refusal and the owner asks the source to allow us.
- **Health:** every regular run writes `<store>/health.json` (`lc_ingest.health`): consecutive failures, and whether the last failure was a refusal.
- **The cycle:** after the retries, a source still failing that has an approved fallback runs its fallback job, `<job>-browser`. The job goes ahead only after `sources.browser_fallback_after_failures` failed runs in a row (`config/cycle.yaml`, default 3). A plain read of the page finds the link and the key, so a file that has already been processed starts no browser. The browser then clicks the link, and the file is checked, read and mapped as usual. Success counts the source as recovered.
- **Configured in the registry** (`pipeline.browser_fallback`: page, link pattern, hosts). It's **active only once the owner approves the hosts** as `browser_hosts` for the source (`lc_core.sources.browser_fallbacks`). Until then it neither deploys nor joins the cycle.
- **The gateway:** a registry host may also be a browser host. The browser profile then applies only to the browser's own requests (the relay sends `X-LC-Browser: 1`), and every other request keeps the registry profile.
- **Sources:**
  - Ireland: `opendata.cro.ie/dataset/companies`.
  - Australia: `data.gov.au/data/dataset/asic-companies`.
  - Not Finland: the file is only an API endpoint.
  - Not Canada: open.canada.ca rejects automated requests, an access control.
  - Not the Socrata states: their export carries registered-agent columns we never request.

## Adding a source

1. The owner approves the source and its hosts as browser hosts (`browser_hosts`, with `approved`).
2. Add a `Profile` to `services/ingest_browser/ingest_browser/sources.py`: `current` (one plain request to find the publication), the `flow` (what a person clicks), `expect_in_name`, and the reader and mapper (in `lc_ingest`).
3. Give the registry entry a pipeline with `image: ingest-browser`. `deploy-ingest-browser.yml` creates the job, and `deploy-cycle.yml` adds it to the daily cycle.

## Japan (jp-nta)

The download page is `https://www.houjin-bangou.nta.go.jp/download/zenken/`. The flow picks 全国 (nationwide) under CSV形式・Unicode. The file `00_zenkoku_all_YYYYMMDD.zip` is updated monthly and is about 270 MB. It holds a headerless 30-column CSV following the NTA's specification, and `lc_ingest/nta.py` reads it. Records are keyed with GLEIF's RA001075 (Corporate Number Publication Site) and the 13-digit corporate number. LEI holders that GLEIF lists under RA000412 (commercial registration, the corporate number without its check digit) merge too: `config/sources.yaml` `gleif_identity` reads RA000412 as RA001075 and adds the check digit (2026-11-20; docs/snapshots-v2.md, "GLEIF identity and the re-key").

### Daily difference files (差分)

Between monthly full files Japan is kept current from the NTA's daily difference files: same host, same form, the same 30 columns. They are configured as `pipeline.daily_diffs` for jp-nta in `config/sources.yaml` and are **off** (`enabled: false`) until the owner turns them on for a first watched run, because the diff page's layout (`page`, `section`) is not yet confirmed from the live page.

- **When:** in the same `ingest-jp` run, after the full file is done or already done. Nothing happens until a full file has been processed.
- **Which files:** one plain read of the diff page lists the dated rows under `section` (`lc_ingest.nta.daily_files`: a row with a `doDownload(n)` link and exactly one date). Files dated from `overlap_days` before the latest full file's date onwards are applied, oldest first, at most `max_per_run` a run; the rest wait for the next run. The browser clicks the row with the same date, and the saved file's name must contain the date (`file_date_format`), or it is fetched again and the run fails with nothing written.
- **Once each, superseded by the next full file:** a diff's publish key names the full file it applies on (`2026-11-02 diff-unicode on 2026-10-30`). A new month's full file supersedes the diffs before it; the days around it are applied again on top, which changes nothing that is already current.
- **Process codes (処理区分):** a row is the corporation's state after the change, so every code is an upsert through the full file's mapper: 01 new, 11 name, 12/13 address, 21 closed and 71 absorbed in a merger (`Closed: …` from the row's close date and cause), 22 and 72 reopened (`Active`), 81 trade-name registration erased. 99 (deleted: the NTA withdrew the number) never deletes: a record we hold is labelled `absent-from-source:jp-nta`, and one we never held isn't added. Only a corporation's latest row in the file (最新履歴 = 1) is used; an unknown code is skipped and counted.
- **First live run:** turn `enabled` on, deploy, and run `ingest-jp`. Check the events: a `browser-step` choosing the day's file, `browser-downloaded` with a file name holding the date, `done` per file with small `changes` and `new_records`, then `diffs` with `applied` and `pending`. A `ValueError` about the heading or no dated files means the page's layout differs from the config: fix `section` (or the parser) before turning it on again.
