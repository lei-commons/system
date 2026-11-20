"""Ingestion for sources whose data is offered only through a web page or form, fetched with a real browser.

The job's LC_SOURCE names its source (jp-nta, ...). Each run first reads which publication is current
with one plain request; if it's already processed, it stops there and no browser starts. Otherwise a
headless browser follows the source's flow (lc_browser: every request through the conduct gateway, at a
person's pace) and the downloaded file goes through the usual runner: file checks, retries, the volume
guard, change files. Only while the system is running and the owner has approved the source.

A source with daily difference files (pipeline.daily_diffs, on) then applies the days after its latest
full file, oldest first, each once per full file (run_diffs).
"""

from __future__ import annotations

import json
import os
import sys

from lc_core.allowlist import AllowList
from lc_core.approvals import require_approved
from lc_core.control import SystemHalted, require_running
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.fetch import FetchError, Gateway
from lc_core.lease import Busy, lease
from lc_ingest.runner import run_ingest
from lc_ingest.store import GcsStore

from .sources import PROFILES, Profile


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def profile_for(source: str) -> Profile:
    if source not in PROFILES:
        raise SystemExit(f"LC_SOURCE must be one of {sorted(PROFILES)}, not {source!r}")
    return PROFILES[source]


def gate_for(profile: Profile):
    def gate():
        """Run only while the system is running and the owner has approved this source."""
        require_running(f"job:{profile.job}")
        require_approved(profile.source)
    return gate


def fetch_for(profile: Profile, key: str, log=log, download=None):
    """The runner's fetch hook: the browser downloads the file into the runner's file."""
    def fetch(gateway, _url, out) -> int:
        if download is None:
            from lc_browser.runner import download as browse
        else:
            browse = download
        names = []
        size = browse(profile.flow, gateway, out, profile.hosts, log=lambda e: (names.append(e.get("file")), log(e)))
        name = next((n for n in names if n), "")
        expected = profile.expect_in_name(key)
        if expected not in name:
            raise FetchError(f"the page offered {name or 'no file'}, not the publication of {key}")
        return size
    return fetch


def run(profile: Profile, gateway, store, gate=None, allowlist=None, log=log, download=None, sleep=None) -> str:
    found = {}

    def find(gw, manifest=None):
        found["key"], url, filename = profile.current(gw)
        return found["key"], url, filename

    def fetch(gw, url, out):
        return fetch_for(profile, found["key"], log=log, download=download)(gw, url, out)

    return run_ingest(gateway=gateway, store=store, gate=gate or gate_for(profile), find=find, read=profile.read,
                      mapper=profile.record, source=profile.source, maker=f"ingestion@{profile.store}",
                      allowlist=allowlist or AllowList.load(), log=log, fetch=fetch,
                      **({"sleep": sleep} if sleep else {}))


OK = ("done", "already-done")


def run_diffs(profile: Profile, config: dict, gateway, store, gate=None, allowlist=None, log=log, download=None,
              sleep=None) -> str:
    """Apply the source's daily difference files on top of its latest full file. Returns the outcome:
    halted, waiting (no full file yet), already-done, done, or the first diff's outcome that wasn't done.

    The files dated from ``overlap_days`` before the full file's date onwards are applied, oldest first,
    at most ``max_per_run`` in one run. Each one's publish key names the full file it applies on, so it is
    applied once per full file: a new month's full file supersedes the diffs before it, and the days
    around it are applied again on top (a diff applied twice changes nothing)."""
    from datetime import date, timedelta

    daily, gate = profile.daily, gate or gate_for(profile)
    if config["page"].split("/")[2].lower() not in profile.hosts:
        raise ValueError(f"{profile.source}: the daily-diff page must be on the source's own host {profile.hosts}")
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "source": profile.source, "reason": str(exc)})
        return "halted"
    base = daily.base(store.read_manifest()["processed"])
    if base is None:
        log({"event": "diffs-waiting", "source": profile.source, "reason": "no full file processed yet"})
        return "waiting"
    since = (date.fromisoformat(base) - timedelta(days=config["overlap_days"])).isoformat()
    done = set(store.read_manifest()["processed"])
    pending = [r for r in daily.listing(gateway, config) if r["day"] >= since and daily.key(r["day"], base) not in done]
    if not pending:
        log({"event": "already-done", "source": profile.source, "publish": f"diffs on {base}"})
        return "already-done"
    outcome, applied = "already-done", 0
    for row in pending[:config["max_per_run"]]:
        key = daily.key(row["day"], base)
        filename = f"{profile.source}-diff-{row['day']}-on-{base}.zip"
        name = date.fromisoformat(row["day"]).strftime(config["file_date_format"])
        browser = Profile(source=profile.source, job=profile.job, store=profile.store, hosts=profile.hosts,
                          current=None, flow=daily.flow(config, row), expect_in_name=lambda _key, name=name: name,
                          read=daily.read, record=daily.record)

        def find(gw, manifest=None, key=key, filename=filename):
            return key, config["page"], filename

        def fetch(gw, url, out, browser=browser, key=key):
            return fetch_for(browser, key, log=log, download=download)(gw, url, out)

        outcome = run_ingest(gateway=gateway, store=store, gate=gate, find=find, read=daily.read, mapper=daily.record,
                             prepare=daily.prepare, source=profile.source, maker=f"ingestion@{profile.store}",
                             allowlist=allowlist or AllowList.load(), log=log, fetch=fetch,
                             **({"sleep": sleep} if sleep else {}))
        if outcome not in OK:
            break
        applied += outcome == "done"
    log({"event": "diffs", "source": profile.source, "base": base, "applied": applied,
         "pending": len(pending) - applied, "outcome": outcome})
    return outcome


def run_fallback(source: str, config: dict, gateway, store, after_failures: int, gate=None, allowlist=None, log=log,
                 download=None, sleep=None, today=None):
    """A source's browser fallback. Returns (outcome, exit code): it runs only after the regular download has
    failed ``after_failures`` runs in a row, and never when the source refused us."""
    from datetime import date

    from lc_ingest.bulk import PROFILES as BULK
    from lc_ingest.health import HEALTH, fallback_decision

    from . import fallback as fb

    profile = BULK[source]
    if gate is None:
        require_running(f"job:{profile.job}-browser")
        require_approved(source)
    else:
        gate()
    allowed, why = fallback_decision(store.read_json(HEALTH), after_failures)
    if not allowed:
        log({"event": "fallback-skipped", "source": source, "reason": why})
        refused = "refused us" in why
        return ("refused" if refused else "not-needed"), (fb.EXIT_REFUSED if refused else fb.EXIT_NOT_YET)
    log({"event": "fallback-started", "source": source, "reason": why})
    find = fb.find_for(source, config, today or date.today())
    browser = Profile(source=source, job=profile.job, store=profile.store, hosts=tuple(config["hosts"]), current=None,
                      flow=fb.link_flow(config),
                      # the file the browser saved must be the one the page linked
                      expect_in_name=lambda key: config["_link"].split("?", 1)[0].rsplit("/", 1)[-1],
                      read=profile.read, record=profile.record)

    def fetch(gw, url, out):
        return fetch_for(browser, "", log=log, download=download)(gw, url, out)

    outcome = run_ingest(gateway=gateway, store=store, gate=lambda: None, find=find, read=profile.read,
                         mapper=profile.record, source=source, maker=f"ingestion@{profile.store}",
                         allowlist=allowlist or AllowList.load(), log=log, fetch=fetch, **({"sleep": sleep} if sleep else {}))
    log({"event": "fallback-used", "source": source, "outcome": outcome})
    return outcome, (0 if outcome in ("done", "already-done") else 1)


def fallback_main(source: str) -> int:
    from google.cloud import storage

    from lc_core.cycle import load as cycle_settings
    from lc_core.sources import browser_fallbacks
    from lc_ingest.bulk import PROFILES as BULK

    config = next((c for c in browser_fallbacks(approved_only=False).values() if c["source"] == source), None)
    if config is None or source not in BULK:
        raise SystemExit(f"{source} has no browser fallback")
    regular = BULK[source]
    gateway = Gateway(os.environ["LC_GATEWAY_URL"], f"job:{regular.job}-browser")
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    record = Recorder(f"job:{regular.job}-browser", log)
    code = 1
    try:
        with lease(client.bucket(bucket), regular.job):  # never at the same time as the regular job
            _, code = run_fallback(source, config, gateway, GcsStore(bucket, regular.store),
                                   cycle_settings()["browser_fallback_after_failures"],
                                   allowlist=AllowList.load(os.environ["LC_FIELDS_CONFIG"]), log=record)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        code = 1  # the regular job is running: not recovered by the fallback
    except Exception as exc:
        record(failure(exc))
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return code


def main() -> int:
    from google.cloud import storage

    from lc_core.sources import daily_diffs

    if os.environ.get("LC_MODE") == "fallback":
        return fallback_main(os.environ.get("LC_SOURCE", ""))
    profile = profile_for(os.environ.get("LC_SOURCE", ""))
    gateway = Gateway(os.environ["LC_GATEWAY_URL"], f"job:{profile.job}")
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    record = Recorder(f"job:{profile.job}", log)
    try:
        diffs = daily_diffs(profile.source) if profile.daily else None
        with lease(client.bucket(bucket), profile.job):
            store = GcsStore(bucket, profile.store)
            outcome = run(profile, gateway, store, allowlist=allowlist, log=record)
            if diffs and diffs["enabled"] and outcome in OK:
                daily = run_diffs(profile, diffs, gateway, store, allowlist=allowlist, log=record)
                outcome = outcome if daily in OK + ("waiting",) else daily
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        record(failure(exc))
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
