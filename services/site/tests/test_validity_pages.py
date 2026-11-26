"""The Data sources and Trials pages show a source on trial marked invalid, with its date and reason, and offer
to re-approve it (lc_core.validity; owner decision 2026-11-22)."""

import pytest
import yaml

from lc_site.system import SystemView, _default_sources_path

MARK = {"failedDays": ["2026-11-23", "2026-11-24", "2026-11-25"],
        "invalid": {"at": "2026-11-25T03:00:00Z", "reason": "Failed on 3 separate days with no successful run between"}}


class Reader:
    def __init__(self, files):
        self.files = files

    def bucket_json(self, bucket, name):
        return self.files.get((bucket, name))


@pytest.fixture(autouse=True)
def on_trial(tmp_path, monkeypatch):
    # The real registry, with the two sources these tests mark kept on trial whatever their stage is today.
    registry = yaml.safe_load(_default_sources_path().read_text())
    for s in registry["sources"]:
        if s["id"] in ("cy-drcor", "si-ajpes-prs"):
            s["pipeline"]["stage"] = "trial"
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(registry, sort_keys=False))
    monkeypatch.setenv("LC_SOURCES_CONFIG", str(path))


def view(decisions=None, sources=None):
    files = {("data", "trials/cy-drcor/validity.json"): MARK,
             ("data", "trials/si-ajpes-prs/validity.json"): {"failedDays": ["2026-11-23"],
                                                              "lastFailure": {"error": "KeyError", "message": "no column"}},
             ("control", "source-approvals.json"): decisions or {}}
    return SystemView(Reader(files), sources=sources)


def row(page, sid):
    return next(s for s in page["sources"] if s["id"] == sid)


def test_an_invalid_trial_shows_as_invalid_with_its_date_reason_and_a_re_approve_action():
    page = view({"cy-drcor": {"status": "approved", "at": "2026-11-22T10:00:00Z"}}).sources_page()
    cy = row(page, "cy-drcor")
    assert cy["validity"]["state"] == "invalid" and cy["validity"]["at"] == "2026-11-25T03:00:00Z"
    assert cy["validity"]["reason"].startswith("Failed on 3") and cy["validity"]["threshold"] == 3
    assert "source-retry:cy-drcor" in [a["id"] for a in cy["actions"]]
    assert page["invalid"] == 1
    si = row(page, "si-ajpes-prs")
    assert si["validity"]["state"] == "failing" and "source-retry:si-ajpes-prs" not in [a["id"] for a in si["actions"]]
    assert row(page, "gleif")["validity"] is None  # production sources are never marked


def test_after_re_approval_it_shows_as_retrying_and_is_no_longer_offered():
    page = view({"cy-drcor": {"status": "approved", "at": "2026-11-26T09:00:00Z"}}).sources_page()
    cy = row(page, "cy-drcor")
    assert cy["validity"]["state"] == "retrying" and page["invalid"] == 0
    assert "source-retry:cy-drcor" not in [a["id"] for a in cy["actions"]]


def test_the_trials_page_carries_the_mark():
    trial = {"id": "cy-drcor", "label": "Cyprus", "job": "ingest-cy", "store": "cy", "stage": "trial"}
    page = view(sources=[trial]).trials()
    assert page["trials"][0]["validity"]["state"] == "invalid"
