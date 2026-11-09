import gzip
import io
import json
import zipfile
from datetime import date

from lc_core.allowlist import AllowList
from lc_core.fetch import FetchError
from lc_ingest.receita import head_office_cnpj, read_lookup
from lc_ingest.store import decode_records
from ingest_br.job import SHARE, run


def zipped(lines):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("F.CSV", "\n".join(";".join(f'"{v}"' for v in line) for line in lines).encode("latin-1"))
    return buf.getvalue()


def estab(base, status="02"):
    return [base, "0001", head_office_cnpj(base)[-2:], "1", "", status, "20240101", "00", "", "", "20100315", "4711302",
            "", "RUA", "A", "1", "", "", "01001000", "SP", "7107", "11", "988887777", "", "", "", "", "x@y.com", "", ""]


EMPRESAS = [zipped([["11222333", "ACME LTDA", "2062", "", "", "", ""], ["22333444", "FULANO 12345678901", "2135", "", "", "", ""]])]
EMPRESAS += [zipped([["33444555", "BETA S.A.", "2054", "", "", "", ""]])] + [zipped([])] * 8
ESTAB = [zipped([estab("11222333"), estab("22333444")]), zipped([estab("33444555")])] + [zipped([])] * 8


class Resp(io.BytesIO):
    pass


class Conn:
    def close(self):
        pass


class FakeGateway:
    def __init__(self, month="2026-10"):
        self.month, self.fetched = month, []
        self.files = {f"{SHARE}/{month}/Municipios.zip": zipped([["7107", "SAO PAULO"]])}
        for k in range(10):
            self.files[f"{SHARE}/{month}/Empresas{k}.zip"] = EMPRESAS[k]
            self.files[f"{SHARE}/{month}/Estabelecimentos{k}.zip"] = ESTAB[k]

    def open(self, target, method="GET", headers=None):
        if target not in self.files:
            raise FetchError(f"{target} answered 404")
        return Conn(), Resp(self.files[target])

    def download(self, target, file):
        self.fetched.append(target.rsplit("/", 1)[-1])
        _, resp = self.open(target)
        data = resp.read()
        file.write(data)
        return len(data)


class MemStore:
    def __init__(self):
        self.manifest, self.shards, self.changes = {"records": 0, "processed": []}, {}, {}

    def read_manifest(self):
        return {**self.manifest, "processed": list(self.manifest["processed"])}

    def write_manifest(self, m):
        self.manifest = m

    def read_shard(self, n):
        return dict(self.shards.get(n, {}))

    def write_shard_file(self, n, path):
        with open(path, "rb") as f:
            self.shards[n] = decode_records(f.read())

    def write_changes_file(self, name, path):
        with gzip.open(path, "rt") as f:
            self.changes[name] = [json.loads(line) for line in f]

    def keep_source(self, name, path):
        pass


class MemLookup:
    built = 0
    shards = {}

    def ready(self):
        return bool(self.shards)

    def write(self, paths, counts):
        MemLookup.built += 1
        for n, p in enumerate(paths):
            with open(p, "rb") as f:
                self.shards[n] = f.read()

    def read(self, n):
        return read_lookup(self.shards[n])


def records(store):
    return {k: v for s in store.shards.values() for k, v in s.items()}


def test_finds_the_release_builds_the_lookup_once_and_loads_head_offices():
    MemLookup.built, MemLookup.shards = 0, {}
    store, gw, events = MemStore(), FakeGateway(), []
    outcome = run(gw, store, lambda m: MemLookup(), today=date(2026, 11, 9), gate=lambda: None,
                  allowlist=AllowList.load(), log=events.append)
    assert outcome == "done" and MemLookup.built == 1
    names = sorted(r["legalName"] for r in records(store).values())
    assert names == ["ACME LTDA", "BETA S.A."]  # the MEI is not a company, so it isn't joined
    text = json.dumps([records(store), store.changes])
    assert "FULANO" not in text and "12345678901" not in text and "x@y.com" not in text and "988887777" not in text
    assert [e["publish"] for e in events if e["event"] == "done"][:2] == ["2026-10 estab0", "2026-10 estab1"]
    assert run(gw, store, lambda m: MemLookup(), today=date(2026, 11, 9), gate=lambda: None,
               allowlist=AllowList.load(), log=events.append) == "already-done" and MemLookup.built == 1


def test_a_spent_budget_stops_between_files_and_the_next_run_carries_on():
    MemLookup.built, MemLookup.shards = 0, {}
    store, gw, events = MemStore(), FakeGateway(), []
    ticks = iter([0, 0, 999, 999, 999] + [999] * 50)
    run(gw, store, lambda m: MemLookup(), today=date(2026, 11, 9), gate=lambda: None, allowlist=AllowList.load(),
        log=events.append, budget=10, clock=lambda: next(ticks))
    assert store.manifest["processed"] == ["2026-10 estab0"] and any(e["event"] == "budget-spent" for e in events)
    run(gw, store, lambda m: MemLookup(), today=date(2026, 11, 9), gate=lambda: None, allowlist=AllowList.load(),
        log=events.append)
    assert "2026-10 estab9" in store.manifest["processed"]
