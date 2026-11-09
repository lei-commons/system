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
from lc_core.decisions import Recorder, log_bucket_name
from lc_core.fetch import FetchError, Gateway
from lc_core.lease import Busy, lease
from lc_ingest.receita import Join, build_lookup, mapper, read_lookup, read_municipalities, rows
from lc_ingest.runner import run_ingest
from lc_ingest.store import SHARDS, GcsStore

COMPONENT = "job:ingest-br"
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


def _download(gateway, url, work):
    path = os.path.join(work, url.rsplit("/", 1)[-1])
    with open(path, "wb") as f:
        gateway.download(url, f)
    return path


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


def build(gateway, lookup, month, log=log) -> dict:
    counts = Counter()
    with tempfile.TemporaryDirectory(prefix="br-lookup-") as work:
        def companies():
            for k in range(FILES):
                path = _download(gateway, f"{SHARE}/{month}/Empresas{k}.zip", work)
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


def run(gateway, store, lookup_for, today=None, gate=lambda: require_running(COMPONENT), allowlist=None, log=log,
        budget=BUDGET_SECONDS, clock=time.monotonic) -> str:
    """Return the outcome: halted, done, already-done, held or denied."""
    try:
        gate()
    except SystemHalted as exc:
        log({"event": "halted", "source": "br", "reason": str(exc)})
        return "halted"
    started = clock()
    month = latest_month(gateway, today or date.today())
    lookup = lookup_for(month)
    if not lookup.ready():
        build(gateway, lookup, month, log=log)
    with tempfile.TemporaryDirectory(prefix="br-mun-") as work:
        with open(_download(gateway, f"{SHARE}/{month}/Municipios.zip", work), "rb") as f:
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

        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                outcome = run_ingest(gateway=gateway, store=store, gate=lambda: None, find=find, read=rows,
                                     mapper=to_record, source="br-rfb", maker="ingestion@br", allowlist=allowlist,
                                     log=log, prepare=Join(lookup.read))
                break
            except zipfile.BadZipFile:  # a cut-short download or an error page: nothing was stored, so fetch it again
                log({"event": "bad-download", "source": "br", "publish": key, "attempt": attempt})
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise
        if outcome not in ("done", "already-done"):
            break
    return outcome


def main() -> int:
    from google.cloud import storage

    gateway = Gateway(os.environ["LC_GATEWAY_URL"], COMPONENT)
    allowlist = AllowList.load(os.environ["LC_FIELDS_CONFIG"])
    bucket_name = os.environ["LC_DATA_BUCKET"]
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    record = Recorder(COMPONENT, log)
    try:
        with lease(bucket, "ingest-br", ttl_seconds=4 * 3600):
            outcome = run(gateway, GcsStore(bucket, "br"), lambda month: Lookup(bucket, month), allowlist=allowlist,
                          log=record)
    except Busy as exc:
        record({"event": "busy", "reason": str(exc)})
        outcome = "busy"
    except Exception as exc:
        record({"event": "failed", "error": type(exc).__name__})
        raise
    finally:
        record.write(client.bucket(log_bucket_name(bucket_name)))
    return 0 if outcome in ("done", "already-done", "halted", "busy") else 1


if __name__ == "__main__":
    sys.exit(main())
