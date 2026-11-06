from lc_core.allowlist import AllowList


def test_forbidden_personal_fields_dropped():
    al = AllowList.load()
    rec = {
        "personName": "A. Director", "role": "director",
        "email": "a@example.com", "phone": "+44 7700 900000", "address": "1 Home St",
        "dateOfBirth": "1970-01-01",
    }
    kept, dropped = al.filter("person_role", rec)
    assert kept == {"personName": "A. Director", "role": "director"}
    assert set(dropped) == {"email", "phone", "address", "dateOfBirth"}


def test_forbidden_never_in_allowed():
    al = AllowList.load()
    assert not (al.forbidden & al.allowed["person_role"])


def test_entity_fields_kept():
    kept, dropped = AllowList.load().filter("entity", {"legalName": "Acme Ltd", "ceoHomeAddress": "x"})
    assert kept == {"legalName": "Acme Ltd"} and dropped == ["ceoHomeAddress"]


def test_round5_exclusions_enforced():
    al = AllowList.load()
    for f in ("nationality", "birthMonthYear", "countryOfResidence", "occupation", "controlNature", "ownershipBand"):
        assert f in al.forbidden
        assert f not in al.allowed["person_role"]
