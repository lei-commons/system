import gzip
import json

from lc_core.allowlist import AllowList
from lc_ingest.store import decode_records
from ingest_sg.job import COLLECTION, run

HEADER = b"uen,entity_name,entity_type_description,company_type_description,entity_status_description,registration_incorporation_date,block,street_name,level_no,unit_no,building_name,postal_code,primary_ssic_code,former_entity_name1\n"
FILES = [HEADER + b"201908346G,TIKET NETWORK PTE. LTD.,Local Company,Private Company Limited by Shares,Live Company,2019-03-15,1,RAFFLES PLACE,10,01,ONE TOWER,048616,62011,na\n"
         + b"53312345A,JOHN TAN TRADING,Sole Proprietorship/ Partnership,,Live,2010-01-01,2,ORCHARD RD,na,na,,238801,47,na",
         HEADER + b"T08LL1234A,ALPHA LLP,Limited Liability Partnership,,Live,2008-05-05,3,MARINA,na,na,,018989,69,OLD ALPHA LLP\n"]


class FakeGateway:
    def __init__(self, datasets=None):
        self.datasets = datasets or [f"d_{i}" for i in range(27)]
        self.polls = 0

    def get_json(self, target):
        if target == COLLECTION:
            return {"data": {"collectionMetadata": {"lastUpdatedAt": "2026-10-14T17:21:16+08:00", "childDatasets": self.datasets}}}
        if target.endswith("initiate-download"):
            return {"code": 0}
        self.polls += 1
        dataset = target.split("/")[-2]
        return {"data": {"status": "DOWNLOAD_SUCCESS", "url": f"https://s3.ap-southeast-1.amazonaws.com/x/{dataset}.csv"}}

    def download(self, target, file):
        i = int(target.rsplit("_", 1)[1].split(".")[0])
        data = FILES[i] if i < len(FILES) else HEADER
        file.write(data)
        return len(data)


class MemStore:
    def __init__(self):
        self.manifest, self.shards = {"records": 0, "processed": []}, {}

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
        pass

    def keep_source(self, name, path):
        pass


def test_joins_all_datasets_and_drops_sole_proprietors():
    store = MemStore()
    assert run(FakeGateway(), store, gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None, sleep=lambda s: None) == "done"
    recs = {r["registrationNumber"]: r for s in store.shards.values() for r in s.values()}
    assert set(recs) == {"201908346G", "T08LL1234A"} and "JOHN" not in json.dumps(recs)
    assert recs["201908346G"]["legalAddress"]["addressLines"] == ["1 RAFFLES PLACE", "#10-01", "ONE TOWER"]
    assert recs["T08LL1234A"]["otherNames"] == ["OLD ALPHA LLP"] and store.manifest["processed"] == ["acra-2026-10-14"]


def test_a_short_collection_fails_rather_than_loading_part_of_the_register():
    import pytest
    from lc_core.fetch import FetchError

    with pytest.raises(FetchError):
        run(FakeGateway(datasets=["d_0"]), MemStore(), gate=lambda: None, allowlist=AllowList.load(), log=lambda r: None,
            sleep=lambda s: None)
