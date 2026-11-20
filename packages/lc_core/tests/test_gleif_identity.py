"""config/sources.yaml gleif_identity: GLEIF's authority and number read as the register's own key."""

import copy

import pytest

from lc_core import gleif_identity
from lc_core.directory_id import mint
from lc_core.gleif_identity import ConfigError, jp_check_digit, parse

BASE = {"version": 1, "gleif_identity": {
    "RA000585": {"registry": "GB-COH", "country": "GB"},
    "RA000631": {"country": "US", "number": ["strip-leading-zeros"]},
    "RA000473": {"registry": "RA000472", "country": "NO"},
    "RA000412": {"registry": "RA001075", "country": "JP", "number": ["jp-corporate-number"]},
}}


def with_section(**entries):
    data = copy.deepcopy(BASE)
    for code, entry in entries.items():
        if entry is ...:
            data["gleif_identity"].pop(code)
        else:
            data["gleif_identity"][code] = entry
    return data


@pytest.fixture(scope="module")
def repo():
    return gleif_identity.load()


def test_companies_house_ids_are_unchanged(repo):
    for code in ("RA000585", "RA000586", "RA000587"):
        assert repo.key(code, "01234567", "FR") == ("GB", "GB-COH", "01234567")
    assert mint(*repo.key("RA000585", "01234567", "GB")) == "LECOZZFZ6LZCOJBDKT73"  # the doctest's ID


def test_oregon_drops_leading_zeros_and_keeps_its_suffix(repo):
    # data.oregon.gov, 2026-11-20: GLEIF's "057491-14" is registry_number 5749114; "2125331-94" is 212533194.
    assert repo.key("RA000631", "057491-14", "US") == ("US", "RA000631", "5749114")
    assert mint(*repo.key("RA000631", "2125331-94", "US")) == mint("US", "RA000631", "212533194")
    assert mint(*repo.key("RA000631", "005744-18", "GB")) == mint("US", "RA000631", "574418")  # the register's country


def test_canada_drops_leading_zeros(repo):
    # Corporations Canada writes 463116 (check digit included, no leading zero) where GLEIF writes "046311-6".
    assert mint(*repo.key("RA000072", "046311-6", "CA")) == mint("CA", "RA000072", "463116")
    assert mint(*repo.key("RA000072", "454461-7", "CA")) == mint("CA", "RA000072", "4544617")
    assert repo.key("RA000072", "834033177RR0001", "CA") == ("CA", "RA000072", "834033177RR0001")  # a BN: as written


def test_norway_files_both_codes_under_one(repo):
    assert mint(*repo.key("RA000473", "967 675 040", "NO")) == mint("NO", "RA000472", "967675040")
    assert mint(*repo.key("RA000472", "967675040", "SE")) == mint("NO", "RA000472", "967675040")


def test_japan_commercial_registration_becomes_the_corporate_number(repo):
    assert jp_check_digit("180301018771") == "1"  # Toyota Motor Corporation, 1180301018771
    assert jp_check_digit("010401016070") == "3"  # the jp-nta tests' 3010401016070
    for base, full in (("0104-01-016070", "3010401016070"), ("180301018771", "1180301018771"),
                       ("0104－01－016070", "3010401016070"),  # full-width dashes (NFKC)
                       ("3010401016070", "3010401016070")):  # already a corporate number
        assert repo.key("RA000412", base, "JP") == ("JP", "RA001075", full), base
    # Not in a corporate number's form, or a wrong check digit: as GLEIF writes it, under its own code.
    for odd in ("200700200M", "4010401016070", "12345"):
        assert repo.key("RA000412", odd, "SG") == ("SG", "RA000412", odd)


def test_every_corporate_number_gleif_lists_checks():
    # GLEIF's RA001075 numbers in the 2026-11-07 golden copy all passed (405 of 405); a sample of them:
    for number in ("7010001089455", "4010001021288", "5010001181453", "1010401022566", "9010401169417",
                   "4010401061149", "3010401035491", "6010603007550", "2010401129724", "4080101000573"):
        assert jp_check_digit(number[1:]) == number[0], number


def test_an_authority_not_listed_mints_as_gleif_writes_it(repo):
    assert repo.key("RA000599", "20231234567", "US") == ("US", "RA000599", "20231234567")


def test_the_fingerprint_names_the_rules():
    a, b = parse(BASE), parse(copy.deepcopy(BASE))
    assert a.fingerprint == b.fingerprint and len(a.fingerprint) == 12
    assert parse(with_section(RA000072={"country": "CA"})).fingerprint != a.fingerprint
    noted = with_section(RA000585={"registry": "GB-COH", "country": "GB", "note": "Companies House"})
    assert parse(noted).fingerprint == a.fingerprint  # a note is documentation only


@pytest.mark.parametrize("entry, message", [
    ({"registry": "gb coh"}, "registry"),
    ({"country": "USA"}, "country"),
    ({"number": ["strip-zeros"]}, "unknown rule"),
    ({"number": "strip-leading-zeros"}, "list of rules"),
    ({"countries": "US"}, "unknown settings"),
    ({}, "says nothing"),
    ("US", "mapping"),
])
def test_bad_entries_stop_the_job(entry, message):
    with pytest.raises(ConfigError, match=message):
        parse(with_section(RA000999=entry))


def test_bad_codes_and_chained_aliases_stop_the_job():
    with pytest.raises(ConfigError, match="six digits"):
        parse(with_section(XX000001={"country": "US"}))
    with pytest.raises(ConfigError, match="aliased itself"):
        parse(with_section(RA000474={"registry": "RA000473"}))
    with pytest.raises(ConfigError, match="missing"):
        parse({"version": 1})
