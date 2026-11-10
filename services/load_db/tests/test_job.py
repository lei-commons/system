import contextlib
import json
import os

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("psycopg")

import load_db.job as job  # noqa: E402


class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def download_as_bytes(self):
        return self.bucket.objects[self.name]

    def download_to_filename(self, path):
        with open(path, "wb") as f:
            f.write(self.bucket.objects[self.name])


class Bucket:
    def __init__(self, objects):
        self.objects = objects

    def blob(self, name):
        return Blob(self, name)


def test_snapshot_files_are_on_disk_one_at_a_time(monkeypatch):
    latest = {"date": "2026-11-10", "prefix": "2026-11-10", "snapshotRoot": "r", "files": ["BR.parquet", "GB.parquet", "US.parquet"],
              "relationships": ["relationships.parquet"]}
    objects = {"latest.json": json.dumps(latest).encode()}
    objects.update({f"2026-11-10/{n}": b"x" * 10 for n in latest["files"] + latest["relationships"]})
    on_disk = []

    def fake_load(conn, files, relationships, meta, readers=()):
        for group in (files, relationships):
            for path in group:
                on_disk.append(len(os.listdir(os.path.dirname(path))))
        return {"records": 3, "relationships": 1}

    import lc_directory.load as load_module

    monkeypatch.setattr(load_module, "load", fake_load)
    monkeypatch.setattr(load_module, "already_loaded", lambda conn, root: False)

    class Conn:
        def execute(self, sql):
            class Result:
                def fetchone(self):
                    return (True,)  # the advisory lock is free
            return Result()

    @contextlib.contextmanager
    def connect():
        yield Conn()

    assert job.run(bucket=Bucket(objects), connect=connect, readers=[], gate=lambda: None, log=lambda r: None) == "loaded"
    assert on_disk == [1, 1, 1, 1]
