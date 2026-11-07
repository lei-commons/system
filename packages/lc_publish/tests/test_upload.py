from lc_publish.snapshot import build
from lc_publish.upload import upload

from test_snapshot import sources


class Bucket:
    def __init__(self):
        self.order = []

    def blob(self, name):
        bucket = self

        class B:
            def upload_from_filename(self, path):
                bucket.order.append(name)

            def upload_from_string(self, data, content_type=None):
                bucket.order.append(name)
                bucket.latest = data

        return B()


def test_files_first_then_latest_pointer(tmp_path):
    build(sources(), tmp_path, "2026-11-07")
    b = Bucket()
    latest = upload(b, tmp_path)
    assert b.order[-1] == "latest.json" and "2026-11-07/manifest.json" in b.order
    assert latest["files"] == ["GB.parquet", "US.parquet"] and latest["prefix"] == "2026-11-07"
