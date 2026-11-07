import copy
import json

import pytest

from lc_core.directory_id import is_directory_id
from lc_ingest.gleif import Skip, to_record
from lc_ingest.ledger import VolumeGuardTripped, apply, diff, leaf_hash, volume_guard
from lc_ingest.pipeline import ingest

ADDRESS = {"language": "en", "addressLines": ["1 Example Street"], "addressNumber": None, "mailRouting": None,
           "city": "London", "region": "GB-LND", "country": "GB", "postalCode": "EC1A 1AA"}


def raw(**entity):
    base = {
        "legalName": {"name": "EXAMPLE LIMITED", "language": "en"},
        "otherNames": [{"name": "EXAMPLE LTD", "type": "ALTERNATIVE_LANGUAGE_LEGAL_NAME"}],
        "legalAddress": ADDRESS, "headquartersAddress": ADDRESS,
        "registeredAt": {"id": "RA000585", "other": None}, "registeredAs": "01234567",
        "jurisdiction": "GB", "category": "GENERAL", "legalForm": {"id": "H0PO", "other": None},
        "status": "ACTIVE", "creationDate": "2001-02-03T00:00:00Z",
    }
    base.update(entity)
    return {"type": "lei-records", "attributes": {"lei": "5493001KJTIIGC8Y1R12", "entity": base,
                                                  "registration": {"status": "ISSUED", "managingLou": "x"}}}


def test_maps_the_fields():
    r = to_record(raw())
    assert r["legalName"] == "EXAMPLE LIMITED" and r["otherNames"] == ["EXAMPLE LTD"]
    assert r["incorporationDate"] == "2001-02-03" and r["legalStatus"] == "ACTIVE" and r["legalForm"] == "H0PO"
    assert r["legalAddress"] == {"addressLines": ["1 Example Street"], "city": "London", "region": "GB-LND",
                                 "country": "GB", "postalCode": "EC1A 1AA"}
    assert is_directory_id(r["directoryId"])


def test_uk_records_share_the_id_companies_house_will_mint():
    assert to_record(raw())["directoryId"] == "LECOZZFZ6LZCOJBDKT73"


@pytest.mark.parametrize("authority", ["RA000585", "RA000586", "RA000587"])
def test_all_three_uk_companies_registers_are_one_registry(authority):
    r = to_record(raw(registeredAt={"id": authority, "other": None}))
    assert r["directoryId"] == "LECOZZFZ6LZCOJBDKT73"


def test_other_registers_get_their_own_id():
    assert to_record(raw(registeredAt={"id": "RA000999", "other": None}))["directoryId"] != "LECOZZFZ6LZCOJBDKT73"


def test_no_registration_number_falls_back_to_the_lei():
    r = to_record(raw(registeredAs=None))
    assert is_directory_id(r["directoryId"]) and "registrationNumber" not in r


def test_natural_persons_never_become_records():
    with pytest.raises(Skip, match="natural person"):
        to_record(raw(category="SOLE_PROPRIETOR"))


def test_no_name_is_skipped():
    with pytest.raises(Skip):
        to_record(raw(legalName=None))


def test_no_country_is_skipped():
    with pytest.raises(Skip):
        to_record(raw(legalAddress=None, jurisdiction=None))


def run(raws, current=None):
    return ingest(raws, current or {}, "src:gleif-2026-11-07#1", "ingestion@gleif")


def test_first_load_creates_a_change_per_field():
    changes, counts = run([raw()])
    assert counts["records"] == 1 and {c["fieldPath"] for c in changes} >= {"legalName", "lei"}
    assert all(c["oldLeafHash"] is None for c in changes)


def test_sole_proprietors_never_reach_the_ledger():
    changes, counts = run([raw(category="SOLE_PROPRIETOR")])
    assert changes == [] and counts["skipped: natural person"] == 1


def test_everything_stored_is_on_the_allow_list():
    from lc_core.allowlist import AllowList

    allowed = AllowList.load().allowed["entity"]
    assert {c["fieldPath"] for c in run([raw()])[0]} <= allowed


def test_nothing_personal_from_the_source_survives():
    bad = raw(contactEmail="someone@example.test", phone="+44 7700 900000")
    assert "example.test" not in json.dumps(run([bad])[0])


def test_malformed_records_are_counted_not_fatal():
    changes, counts = run([{"attributes": {}}, {}, raw()])
    assert counts["skipped: malformed"] == 2 and counts["records"] == 1


def test_unchanged_record_makes_no_changes():
    current = to_record(raw())
    assert run([raw()], {current["directoryId"]: current})[0] == []


def test_only_the_changed_field_is_recorded():
    old = to_record(raw())
    new = to_record(raw(status="INACTIVE"))
    (change,) = diff(old, new, "src:x", "m")
    assert change["fieldPath"] == "legalStatus" and change["newValue"] == "INACTIVE"
    assert change["oldLeafHash"] == leaf_hash("ACTIVE") and change["sourceIds"] == ["src:x"]


def test_change_lines_do_not_repeat_the_old_value():
    old, new = {"directoryId": "X", "legalName": "OLD NAME"}, {"directoryId": "X", "legalName": "NEW NAME"}
    assert "OLD NAME" not in json.dumps(diff(old, new, "s", "m"))


def test_diff_is_deterministic():
    a, b = to_record(raw()), to_record(raw(status="INACTIVE", legalForm={"id": "ZZZZ"}))
    assert diff(a, b, "s", "m") == diff(copy.deepcopy(a), copy.deepcopy(b), "s", "m")


def test_replaying_the_ledger_rebuilds_the_record():
    a, b = to_record(raw()), to_record(raw(status="INACTIVE", registeredAs="01234567"))
    start = {"directoryId": a["directoryId"]}
    built = apply(apply(start, diff(start, a, "s", "m")), diff(a, b, "s", "m"))
    assert built == b


def test_removed_field_replays_as_removed():
    a = {"directoryId": "X", "website": "a.test"}
    assert apply(a, diff(a, {"directoryId": "X"}, "s", "m")) == {"directoryId": "X"}


def test_tampered_history_is_detected():
    a = {"directoryId": "X", "legalName": "A"}
    changes = diff(a, {"directoryId": "X", "legalName": "B"}, "s", "m")
    with pytest.raises(ValueError, match="chain broken"):
        apply({"directoryId": "X", "legalName": "TAMPERED"}, changes)


def test_volume_guard_holds_a_mass_change():
    with pytest.raises(VolumeGuardTripped):
        volume_guard(500, 1000)


@pytest.mark.parametrize("changed,total", [(100, 1000), (5, 10), (0, 0)])
def test_volume_guard_allows_normal_batches(changed, total):
    volume_guard(changed, total)


def test_mass_change_returns_nothing():
    current = {}
    for i in range(200):
        r = to_record(raw(registeredAs=str(i)))
        current[r["directoryId"]] = r
    with pytest.raises(VolumeGuardTripped):
        run([raw(registeredAs=str(i), status="INACTIVE") for i in range(200)], current)
