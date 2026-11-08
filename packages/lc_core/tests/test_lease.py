import json

import pytest

pytest.importorskip("google.api_core")
from google.api_core.exceptions import NotFound, PreconditionFailed  # noqa: E402

from lc_core.lease import Busy, lease  # noqa: E402


class Store:
    def __init__(self):
        self.objects = {}  # name -> (generation, bytes)
        self.counter = 0


class Blob:
    def __init__(self, store, name):
        self.store, self.name, self.generation = store, name, None

    def upload_from_string(self, data, if_generation_match=None):
        current = self.store.objects.get(self.name)
        if if_generation_match == 0 and current:
            raise PreconditionFailed("exists")
        if if_generation_match not in (None, 0) and (not current or current[0] != if_generation_match):
            raise PreconditionFailed("changed")
        self.store.counter += 1
        self.store.objects[self.name] = (self.store.counter, data.encode())

    def reload(self):
        if self.name not in self.store.objects:
            raise NotFound("gone")
        self.generation = self.store.objects[self.name][0]

    def download_as_bytes(self, if_generation_match=None):
        generation, data = self.store.objects[self.name]
        if if_generation_match is not None and generation != if_generation_match:
            raise PreconditionFailed("changed")
        return data

    def delete(self, if_generation_match=None):
        current = self.store.objects.get(self.name)
        if not current:
            raise NotFound("gone")
        if if_generation_match is not None and current[0] != if_generation_match:
            raise PreconditionFailed("changed")
        del self.store.objects[self.name]


class Bucket:
    def __init__(self):
        self.store = Store()

    def blob(self, name):
        return Blob(self.store, name)


def test_one_holder_at_a_time_and_released_after():
    b = Bucket()
    with lease(b, "job"):
        with pytest.raises(Busy):
            with lease(b, "job"):
                pass
    with lease(b, "job"):
        pass
    assert b.store.objects == {}


def test_released_even_when_the_run_fails():
    b = Bucket()
    with pytest.raises(RuntimeError):
        with lease(b, "job"):
            raise RuntimeError("boom")
    assert b.store.objects == {}


def test_a_crashed_runs_lease_is_taken_over_after_its_ttl():
    b = Bucket()
    b.blob("leases/job").upload_from_string(json.dumps({"holder": "crashed", "since": 1000}))
    with pytest.raises(Busy):
        with lease(b, "job", ttl_seconds=100, clock=lambda: 1050):
            pass
    with lease(b, "job", ttl_seconds=100, clock=lambda: 1200):
        assert b"crashed" not in b.store.objects["leases/job"][1]


def test_different_jobs_do_not_block_each_other():
    b = Bucket()
    with lease(b, "a"):
        with lease(b, "b"):
            assert set(b.store.objects) == {"leases/a", "leases/b"}


def test_never_deletes_a_lease_someone_else_took_over():
    b = Bucket()
    with lease(b, "job", ttl_seconds=10, clock=lambda: 0):
        b.store.objects["leases/job"] = (999, json.dumps({"holder": "other", "since": 50}).encode())
    assert "leases/job" in b.store.objects
