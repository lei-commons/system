"""Processed downloads and working data are deleted within 24 hours (owner, 2026-11-23): a run deletes any downloaded
file an earlier run kept, and never keeps one itself."""

from collections import Counter

from lc_ingest.store import GcsStore


class Blob:
    def __init__(self, bucket, name, generation=None, data=b"{}"):
        self.bucket, self.name, self.generation, self.data = bucket, name, generation, data

    def exists(self):
        return any(b.name == self.name for b in self.bucket.objects)

    def download_as_bytes(self):
        return next(b.data for b in reversed(self.bucket.objects) if b.name == self.name)

    def upload_from_string(self, data, content_type=None):
        self.bucket.add(self.name, data if isinstance(data, bytes) else data.encode())


class Bucket:
    """A versioned bucket: every write is a new generation."""

    def __init__(self):
        self.objects, self.client, self.generation = [], self, 0

    def add(self, name, data=b"{}"):
        self.generation += 1
        self.objects.append(Blob(self, name, self.generation, data))

    def blob(self, name):
        return Blob(self, name)

    def list_blobs(self, bucket, prefix, versions=False):
        found = [b for b in self.objects if b.name.startswith(prefix)]
        return found if versions else list({b.name: b for b in found}.values())

    def delete_blob(self, name, generation=None):
        self.objects = [b for b in self.objects if not (b.name == name and b.generation == generation)]


def names(bucket):
    return sorted({b.name for b in bucket.objects})


def test_kept_downloads_are_purged_in_every_version_and_nothing_else_is_touched():
    bucket = Bucket()
    for name in ("br/sources/2026-11-Estabelecimentos0.zip", "br/sources/2026-11-Estabelecimentos0.zip",
                 "br/sources/2026-10-Estabelecimentos1.zip", "br/records/000.jsonl.gz", "br/manifest.json",
                 "brx/sources/other-store.zip"):
        bucket.add(name)
    assert GcsStore(bucket, "br").purge_sources() == 3
    assert names(bucket) == ["br/manifest.json", "br/records/000.jsonl.gz", "brx/sources/other-store.zip"]
    assert GcsStore(bucket, "br").purge_sources() == 0


def test_a_publication_s_id_list_is_deleted_on_request():
    bucket = Bucket()
    store = GcsStore(bucket, "gb")
    store.keep_seen("2026-11-01 part1", {"A", "B"})
    assert store.read_seen("2026-11-01 part1") == {"A", "B"}
    assert store.delete_seen("2026-11-01 part1") == 1 and store.read_seen("2026-11-01 part1") is None


def test_a_run_purges_what_earlier_runs_kept_and_says_so():
    from lc_ingest.runner import run_ingest

    class Store:
        purged = 0

        def purge_sources(self):
            Store.purged += 1
            return 4

        def read_manifest(self):
            return {"records": 0, "processed": ["k"]}

    events = []
    outcome = run_ingest(gateway=None, store=Store(), gate=lambda: None, find=lambda g, m: ("k", "u", "f.zip"),
                         read=None, mapper=None, source="br-rfb", maker="m", allowlist=None, log=events.append,
                         guard_settings=object())
    assert outcome == "already-done" and Store.purged == 1
    assert events[0] == {"event": "sources-purged", "source": "br-rfb", "objects": 4}
    assert Counter(e["event"] for e in events) == Counter({"sources-purged": 1, "already-done": 1})
