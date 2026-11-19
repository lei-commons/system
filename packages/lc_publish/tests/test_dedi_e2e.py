"""DeDi over real snapshots: a simulated directory published day after day by the real build, then the DeDi set
published from the bucket and checked, and every country's files fetched by their DeDi URLs and checked
against the SHA-256 and Merkle root the signed DeDi file gives."""

import hashlib
import json
from datetime import timedelta

import pytest

pytest.importorskip("pyarrow")

from lc_dedi import jws  # noqa: E402
from lc_publish import dedi  # noqa: E402
from lc_publish.testing import World  # noqa: E402
from test_dedi import SOURCES, effective  # noqa: E402
from test_v2 import Env  # noqa: E402


def test_dedi_describes_real_snapshots(tmp_path):
    env, world = Env(tmp_path), World()
    cfg = dedi.load_config()
    signer = jws.SeedSigner("lc-dedi-1", bytes(range(32)))
    for _ in range(5):  # a full day, then changes days, then (every 3 days) a full day again
        manifest = env.publish(world)
        world.mutate()
        now = env.now[0] + timedelta(hours=1)
        status = dedi.publish(env.snapshots, cfg, signer, sources=SOURCES, effective=effective(),
                              now=lambda: now, record=env.record, wait=lambda s: None)
        assert status["ok"] and status["date"] == manifest["date"]
        assert dedi.check(env.snapshots, cfg, now=now)["ok"]
        countries = json.loads(env.snapshots.objects["dedi/dedi.countries.json"]["data"])
        for record in countries["records"]:
            d = record["details"]
            assert d["full_snapshot"]["date"] == manifest["anchor"] and d["current"]["date"] == manifest["date"]
            for part in d["parts"]:
                obj = part["url"][len(cfg.data_base_url) + 1:]
                data = env.snapshots.objects[obj]["data"]
                assert "sha256:" + hashlib.sha256(data).hexdigest() == part["sha256"] and len(data) == part["bytes"]
            anchor = env.snapshots.objects[f"{manifest['anchor']}/manifest.json"]["data"]
            assert hashlib.sha256(anchor).hexdigest() == d["data_url_checksum"]
        editions = json.loads(env.snapshots.objects["dedi/dedi.snapshots.json"]["data"])["records"]
        assert editions[0]["record_name"] == manifest["date"]
