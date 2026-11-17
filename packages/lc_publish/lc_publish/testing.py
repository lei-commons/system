"""An in-memory stand-in for a Cloud Storage bucket, for tests: just what the publishing code uses."""

from __future__ import annotations

import copy
import json
import random

from datetime import datetime, timezone
from pathlib import Path


class PreconditionFailed(Exception):
    pass


class _Listing(list):
    def __init__(self, blobs, prefixes):
        super().__init__(blobs)
        self.prefixes = prefixes


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    @property
    def _obj(self):
        return self.bucket.objects.get(self.name)

    @property
    def size(self):
        return len(self._obj["data"]) if self._obj else None

    @property
    def time_created(self):
        return self._obj["created"] if self._obj else None

    def exists(self):
        return self.name in self.bucket.objects

    def download_as_bytes(self):
        if self.name in self.bucket.fail_reads:
            raise OSError("unreadable")
        if not self.exists():
            from google.api_core.exceptions import NotFound

            raise NotFound(self.name)
        return self.bucket.objects[self.name]["data"]

    def open(self, mode="rb"):
        import io

        if mode != "rb":
            raise ValueError("read only")
        return io.BytesIO(self.download_as_bytes())

    def download_to_filename(self, path):
        Path(path).write_bytes(self.download_as_bytes())

    def upload_from_filename(self, path, content_type=None, if_generation_match=None):
        self._put(Path(path).read_bytes(), if_generation_match)

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        self._put(data.encode() if isinstance(data, str) else data, if_generation_match)

    def _put(self, data, if_generation_match):
        if if_generation_match == 0 and self.exists():
            from google.api_core.exceptions import PreconditionFailed as GcsPreconditionFailed

            raise GcsPreconditionFailed(self.name)
        if self.name in self.bucket.objects:
            self.bucket.noncurrent.append(self.name)
        self.bucket.objects[self.name] = {"data": data, "created": self.bucket.clock()}
        self.bucket.writes.append(self.name)

    def delete(self):
        self.bucket.objects.pop(self.name)
        self.bucket.noncurrent.append(self.name)
        self.bucket.deletes.append(self.name)


class FakeBucket:
    def __init__(self, clock=None):
        self.objects, self.writes, self.deletes, self.noncurrent, self.fail_reads = {}, [], [], [], set()
        self.clock = clock or (lambda: datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc))

    def blob(self, name):
        return FakeBlob(self, name)

    def get_blob(self, name):
        return FakeBlob(self, name) if name in self.objects else None

    def list_blobs(self, prefix="", delimiter=None):
        names = sorted(n for n in self.objects if n.startswith(prefix))
        if not delimiter:
            return _Listing([FakeBlob(self, n) for n in names], set())
        blobs, prefixes = [], set()
        for n in names:
            rest = n[len(prefix):]
            if delimiter in rest:
                prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
            else:
                blobs.append(FakeBlob(self, n))
        return _Listing(blobs, prefixes)


def did(i):
    return f"LECOZZ{i:012d}XX"


class World:
    """Three sources and some relationships, changing a little each day."""

    def __init__(self, seed=7, n=1200):
        self.rng = random.Random(seed)
        self.next_id = n * 3
        self.sources = {"gb-coh": {}, "br-rfb": {}, "gleif": {}}
        for i in range(n):
            self.sources["gb-coh"][did(i)] = {"directoryId": did(i), "legalName": f"GB CO {i} LIMITED",
                                              "legalStatus": "Active", "legalJurisdiction": "GB",
                                              "legalAddress": {"country": "GB", "city": "LONDON"}}
            self.sources["br-rfb"][did(n + i)] = {"directoryId": did(n + i), "legalName": f"BR EMPRESA {i} LTDA",
                                                  "legalJurisdiction": "BR", "legalStatus": "ATIVA"}
        for i in range(0, n, 4):  # GLEIF knows every fourth UK company, and some of its own
            self.sources["gleif"][did(i)] = {"directoryId": did(i), "lei": f"{i:018d}AA", "legalName": f"GB CO {i} LTD",
                                             "legalStatus": "ACTIVE", "legalJurisdiction": "GB"}
            self.sources["gleif"][did(2 * n + i)] = {"directoryId": did(2 * n + i), "lei": f"{2 * n + i:018d}AA",
                                                     "legalName": f"DE GMBH {i}", "legalJurisdiction": "DE",
                                                     "legalStatus": "ACTIVE"}
        leis = [r["lei"] for r in self.sources["gleif"].values()]
        self.relationships = {}
        for i in range(150):
            key = f"REL{i:06d}"
            self.relationships[key] = {"directoryId": key, "startNode": self.rng.choice(leis), "endNode": self.rng.choice(leis),
                                       "relationshipType": "IS_DIRECTLY_CONSOLIDATED_BY", "percentage": 50 + i % 50}

    def mutate(self, size=0.01):
        rng = self.rng
        for sid, records in self.sources.items():
            ids = sorted(records)
            for rid in rng.sample(ids, max(1, int(len(ids) * size))):
                records[rid] = {**records[rid], "legalName": records[rid]["legalName"] + " X"}
            for rid in rng.sample(ids, max(1, int(len(ids) * size / 2))):
                del records[rid]
            for _ in range(max(1, int(len(ids) * size / 2))):
                self.next_id += 1
                rid = did(self.next_id)
                country = {"gb-coh": "GB", "br-rfb": "BR", "gleif": "FR"}[sid]
                records[rid] = {"directoryId": rid, "legalName": f"NEW {self.next_id}", "legalJurisdiction": country}
        uk = sorted(self.sources["gb-coh"])
        moved = rng.choice(uk)  # a company moves to Ireland
        self.sources["gb-coh"][moved] = {**self.sources["gb-coh"][moved], "legalJurisdiction": "IE"}
        joined = rng.choice([r for r in uk if r not in self.sources["gleif"]])  # GLEIF picks up a UK company
        self.sources["gleif"][joined] = {"directoryId": joined, "lei": f"J{joined[-12:]}000000A", "legalName": "JOINED"}
        keys = sorted(self.relationships)
        for key in rng.sample(keys, 3):
            self.relationships[key] = {**self.relationships[key], "relationshipStatus": rng.choice(["ACTIVE", "INACTIVE"])}
        del self.relationships[rng.choice(keys)]
        self.next_id += 1
        self.relationships[f"REL9{self.next_id:05d}"] = {"directoryId": f"REL9{self.next_id:05d}", "startNode": "A" * 20,
                                                         "endNode": "B" * 20, "relationshipType": "IS_SUBFUND_OF"}

    def readers(self):
        from .shards import shard_of

        snap = copy.deepcopy(self.sources)
        return {sid: (lambda n, recs=recs: {k: v for k, v in recs.items() if shard_of(k) == n}) for sid, recs in snap.items()}

    def relationship_reader(self):
        from .shards import shard_of

        rels = copy.deepcopy(self.relationships)
        return lambda n: {k: v for k, v in rels.items() if shard_of(k) == n}

    def truth(self):
        from .snapshot import entity_row, merge

        merged = merge(copy.deepcopy(self.sources))
        from .snapshot import sources

        by_id = {x["id"]: x for x in sources()}
        return sorted(json.dumps(entity_row(merged[d], by_id), sort_keys=True) for d in merged)
