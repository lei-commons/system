"""The Brazil run: Receita Federal's monthly open CNPJ release.

Each month:
1. Lookup: all 10 company files (Empresas) become a lookup of companies by CNPJ
   base, in 256 shards under ``br-co/lookup/<month>/``. Natural persons are
   dropped there (see lc_ingest.receita), so nothing about them is stored.
2. Head offices: each establishment file (Estabelecimentos0..9) goes through
   the usual runner, keyed "<month> estab<k>". Head offices are joined to their
   company, active companies are kept, and companies already stored get their
   status updated. A run keeps taking the next file until its time budget is
   spent, so the first month's load takes a few runs. Later runs skip what's done.

The release is found by trying this month's folder, then the previous ones.
Downloads go through the conduct gateway like every source.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import zipfile
from collections import Counter
from datetime import date, timedelta

from lc_core.allowlist import AllowList
from lc_core.control import SystemHalted, require_running
from lc_core.approvals import require_approved
from lc_core.decisions import Recorder, failure, log_bucket_name
from lc_core.fetch import FetchError, Gateway
from lc_core.lease import Busy, lease
from lc_ingest import filecheck
from lc_ingest.receita import Join, build_lookup, mapper, read_lookup, read_municipalities, rows
from lc_ingest.runner import run_ingest
from lc_ingest.store import SHARDS, GcsStore
from lc_ingest import health

COMPONENT = "job:ingest-br"
SOURCE_ID = "br-rfb-cnpj"  # its entry in config/sources.yaml


def gate():
    """Run only while the system is running and the owner has approved this source."""
    require_running(COMPONENT)
    require_approved(SOURCE_ID)

SHARE = "https://arquivos.receitafederal.gov.br/public.php/dav/files/gn672Ad4CF8N6TK/Dados/Cadastros/CNPJ"
FILES = 10
DOWNLOAD_ATTEMPTS = 3
BUDGET_SECONDS = int(os.environ.get("LC_BR_BUDGET_SECONDS", str(100 * 60)))


def log(record):
    print(json.dumps(record, sort_keys=True), flush=True)


def months(today: date, back: int = 3):
    first = today.replace(day=1)
    for _ in range(back):
        yield first.strftime("%Y-%m")
        first = (first - timedelta(days=1)).replace(day=1)


def latest_month(gateway, today: date) -> str:
    """The newest month whose release is published (its small Municipios file answers)."""
    for month in months(today):
        try:
            conn, resp = gateway.open(f"{SHARE}/{month}/Municipios.zip")
            conn.close()
            return month
        except FetchError as exc:
            if "answered 404" not in str(exc):
                raise  # anything but "not published yet" is a real error, reported as itself
            continue
    raise FetchError("no CNPJ release in the last three months")


def _download(gateway, url, work, log=log, attempts: int = None, sleep=time.sleep):
    """Download a ZIP and check it (lc_ingest.filecheck: complete, an archive, every member's checksum): a
    cut-short download, an error page or a corrupt file is fetched again, up to DOWNLOAD_ATTEMPTS times, each
    logged (2026-11-17: one bad Empresas download failed the whole cycle)."""
    attempts = attempts or DOWNLOAD_ATTEMPTS
    path = os.path.join(work, url.rsplit("/", 1)[-1])
    what = url.rsplit("/", 2)[-2] + " " + url.rsplit("/", 1)[-1]
    for attempt in range(1, attempts + 1):
        try:
            with open(path, "wb") as f:
                gateway.download(url, f)
            filecheck.check(path, "zip")
            if attempt > 1:
                log({"event": "download-recovered", "source": "br", "publish": what, "attempt": attempt})
            return path
        except (filecheck.BadFile, FetchError) as exc:
            if isinstance(exc, FetchError) and "answered 4" in str(exc):
                raise  # not there, or refused: fetching again won't help
            reason = f"{type(exc).__name__}: {str(exc)[:160]}"
            if attempt == attempts:
                log({"event": "download-failed", "source": "br", "publish": what, "attempts": attempts, "reason": reason})
                if os.path.exists(path):
                    os.remove(path)
                raise zipfile.BadZipFile(f"{url.rsplit('/', 1)[-1]} isn't a valid ZIP after {attempts} downloads ({reason})") from exc
            log({"event": "bad-download", "source": "br", "publish": what, "attempt": attempt, "reason": reason})
            sleep(10 * attempt)


class Lookup:
    """The month's company lookup in the data bucket."""

    def __init__(self, bucket, month):
        self._bucket, self._prefix = bucket, f"br-co/lookup/{month}"

    def ready(self) -> bool:
        return self._bucket.blob(f"{self._prefix}/DONE").exists()

    def write(self, paths, counts):
        for n, path in enumerate(paths):
            self._bucket.blob(f"{self._prefix}/{n:03d}.tsv.gz").upload_from_filename(path)
        self._bucket.blob(f"{self._prefix}/DONE").upload_from_string(json.dumps(dict(counts), sort_keys=True))

    def read(self, n):
        return read_lookup(self._bucket.blob(f"{self._prefix}/{n:03d}.tsv.gz").download_as_bytes())


def drop_lookups(bucket, keep: str = None) -> int:
    """Delete every month's lookup except ``keep`` (every version). A lookup only serves its month's run: once the
    month's files are all done it is deleted (owner, 2026-11-23: processed working data goes within 24 hours)."""
    deleted = 0
    for blob in bucket.client.list_blobs(bucket, prefix="br-co/lookup/", versions=True):
        if keep and blob.name.startswith(f"br-co/lookup/{keep}/"):
            continue
        bucket.delete_blob(blob.name, generation=blob.generation)
        deleted += 1
    return deleted


def build(gateway, lookup, month, log=log, sleep=time.sleep) -> dict:
    counts = Counter()
    with tempfile.TemporaryDirectory(prefix="br-lookup-") as work:
        def companies():
            for k in range(FILES):
                path = _download(gateway, f"{SHARE}/{month}/Empresas{k}.zip", work, log=log, sleep=sleep)
                with open(path, "rb") as f:
                    yield f
                os.remove(path)  # the disk is memory on Cloud Run

        shards = os.path.join(work, "shards")
        os.mkdir(shards)
        paths = build_lookup(companies(), shards, counts)
        lookup.write(paths, counts)
    log({"event": "lookup-built", "source": "br", "publish": month, "companies": counts["companies"],
         "natural_persons_dropped": counts["companies skipped: natural person"]})
    return dict(counts)


def run(gateway, store, lookup_for, today=None, gate=gate, allowlist=None, log=log,
        budget=BUDGET_SECONDS, clock=time.monotonic, sleep=time.sleep, drop=lambda keep: 0) -> str:
    """Return the outcome: halted, done, already-done, held or denied."""
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "source": "br", "reason": str(exc)})
        return "halted"
    started = clock()
    month = latest_month(gateway, today or date.today())
    processed = set(store.read_manifest().get("processed", []))
    if all(f"{month} estab{k}" in processed for k in range(FILES)):
        # Nothing to do this month: say so (the decision log and the System page see the run), and fetch nothing.
        _dropped(drop, None, month, log)
        log({"event": "already-done", "source": "br", "publish": month})
        return "already-done"
    _dropped(drop, month, month, log)  # earlier months' lookups
    lookup = lookup_for(month)
    if not lookup.ready():
        build(gateway, lookup, month, log=log, sleep=sleep)
    with tempfile.TemporaryDirectory(prefix="br-mun-") as work:
        with open(_download(gateway, f"{SHARE}/{month}/Municipios.zip", work, log=log, sleep=sleep), "rb") as f:
            municipalities = read_municipalities(f)
    to_record = mapper(municipalities)
    allowlist = allowlist or AllowList.load()
    outcome = "already-done"
    for k in range(FILES):
        key = f"{month} estab{k}"
        if key in store.read_manifest().get("processed", []):
            continue
        if clock() - started > budget:
            log({"event": "budget-spent", "source": "br", "publish": month, "next": key})
            break

        def find(gateway, manifest=None, k=k, key=key):
            name = f"Estabelecimentos{k}.zip"
            return key, f"{SHARE}/{month}/{name}", f"{month}-{name}"

        # The runner checks the file and fetches it again if it is bad (lc_ingest.filecheck).
        outcome = run_ingest(gateway=gateway, store=store, gate=lambda: None, find=find, read=rows,
                             mapper=to_record, source="br-rfb", maker="ingestion@br", allowlist=allowlist,
                             log=log, prepare=Join(lookup.read), attempts=DOWNLOAD_ATTEMPTS, sleep=sleep)
        if outcome not in ("done", "already-done"):
            break
    if all(f"{month} estab{k}" in store.read_manifest().get("processed", []) for k in range(FILES)):
        _dropped(drop, None, month, log)  # the month is done: its lookup has served its purpose
    return outcome


def _dropped(drop, keep, month, log) -> None:
    deleted = drop(keep)
    if deleted:
        log({"event": "lookup-deleted", "source": "br", "publish": month, "objects": deleted})


def main() -> int:
    from google.cloud import storage

    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket_name = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    record = Recorder(COMPONENT, log)
    store = GcsStore(bucket, "br")
    try:
        with lease(bucket, "ingest-br", ttl_seconds=4 * 3600):
            outcome = run(gateway, store, lambda month: Lookup(bucket, month), allowlist=allowlist, log=record,
                          drop=lambda keep: drop_lookups(bucket, keep))
        health.note(store, outcome=outcome, log=record)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        failed = failure(exc)
        record(failed)
        health.note(store, failure=failed, log=record)  # beside the store: a browser fallback reads it
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket_name)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
