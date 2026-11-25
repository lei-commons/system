import pytest
import yaml

from lc_core import status
from lc_core.sources import pipelines
from lc_core.status import StatusConfigError, StatusMap

VOCABULARY = ["active", "inactive", "dissolved", "in-liquidation", "struck-off", "merged", "unknown"]


def test_the_vocabulary_is_labels_yaml_legal_status():
    assert status.categories() == VOCABULARY


def test_the_real_map_loads_and_covers_every_built_source():
    m = status.load()
    assert set(m.sources()) == {p["records_as"] for p in pipelines()}
    assert status.unregistered() == []


@pytest.mark.parametrize("source,word,category", [
    ("gleif", "ACTIVE", "active"), ("gleif", "INACTIVE", "inactive"),
    ("gb-coh", "Active", "active"), ("gb-coh", "Liquidation", "in-liquidation"), ("gb-coh", "Dissolved", "dissolved"),
    ("br-rfb", "Active", "active"), ("br-rfb", "Closed", "dissolved"), ("br-rfb", "Unfit", "inactive"),
    ("us-co-sos", "Good Standing", "active"), ("us-co-sos", "Administratively Dissolved", "dissolved"),
    ("ie-cro", "Normal", "active"), ("ie-cro", "Normal ", "active"),
    ("sg-acra", "Live Company", "active"), ("sg-acra", "Struck Off - Section 344", "struck-off"),
    ("au-asic", "Registered", "active"), ("au-asic", "Deregistered", "dissolved"),
    ("fi-prh", "Active", "active"), ("fi-prh", "Bankruptcy", "in-liquidation"),
    ("jp-nta", "Active", "active"), ("jp-nta", "Closed: dissolved by merger", "merged"),
    ("jp-nta", "Closed: liquidation completed", "dissolved"), ("jp-nta", "Closed: a new cause", "inactive"),
    ("ca-ised", "Inactive - Amalgamated", "merged"), ("ca-ised", "Inactive - Something", "inactive"),
    ("us-ny-dos", "Active", "active"), ("us-or-sos", "Active", "active"), ("us-ct-sots", "Forfeited", "struck-off"),
])
def test_register_words(source, word, category):
    assert status.load().category(source, word) == (category, True)


def test_words_match_trimmed_and_in_any_case_and_unlisted_words_are_unmapped():
    m = StatusMap({"x": {"active": ["Good  Standing"], "dissolved": ["Dissolved*"]}}, VOCABULARY)
    assert m.category("x", "  good standing ") == ("active", True)
    assert m.category("x", "DISSOLVED - BY COURT") == ("dissolved", True)
    assert m.category("x", "Lapsed") == ("unknown", False)
    assert m.category("y", "Active") == ("unknown", False)  # a source with no map
    assert m.category("x", None) == m.category("x", "  ") == ("unknown", True)  # no word: nothing to map


# Every status word Cyprus's trial measured (2026-W48: 48,190 records had a word the map missed).
CY_TRIAL_WORDS = {
    "Διαγραμμένη": "struck-off", "Εγγεγραμμένη": "active", "Στάληκε επιστολή Υπενθύμισης": "active",
    "Διάλυση λόγω Ολοκλήρωσης Εκούσιας Εκκαθάρισης": "dissolved", "Δημοσιεύτηκε η Τρίμηνη": "active",
    "Εκούσια Εκκαθάριση από Μετόχους": "in-liquidation", "Διαλύθηκε λόγω συγχώνευσης": "merged",
    "Εκκαθάριση με Διάταγμα Δικαστηρίου": "in-liquidation", "Διάλυση λόγω Ολοκλήρωσης Εκκαθάρισης": "dissolved",
    "Υπο Διαχείριση": "active", "Εκούσια Εκκαθάριση από Πιστωτές": "in-liquidation",
    "Υπο Διαχείριση & Υπο Εκκαθάριση": "in-liquidation", "Διορισμός Προσωρινού Εκκαθαριστή": "in-liquidation",
    "Υπο Εκκαθάριση": "in-liquidation", "Ευρωπαϊκή Εταιρεία": "active",
    "Αφαιρέθηκε από το Μητρώο ΕΕ με απόφαση Ανωτάτου Δικαστηρίου": "struck-off",
    "Εκκαθάριση με Διάταγμα Δικαστηρίου& Διορ.Ειδικού Διαχειριστή": "in-liquidation",
}


def test_every_word_cyprus_trial_measured_is_mapped():
    m = status.load()
    assert {w: m.category("cy-drcor", w) for w in CY_TRIAL_WORDS} == {w: (c, True) for w, c in CY_TRIAL_WORDS.items()}


def test_an_exact_word_wins_over_a_prefix_and_a_longer_prefix_over_a_shorter():
    m = StatusMap({"x": {"inactive": ["Inactive*"], "merged": ["Inactive - Amalgamated*"], "active": ["Inactive - ok"]}},
                  VOCABULARY)
    assert m.category("x", "Inactive - Amalgamated 2001")[0] == "merged"
    assert m.category("x", "inactive - ok")[0] == "active"
    assert m.category("x", "Inactive - other")[0] == "inactive"


@pytest.mark.parametrize("maps,why", [
    ({"x": {"closed": ["Closed"]}}, "isn't in labels.yaml"),
    ({"x": {"active": ["Active"], "inactive": ["ACTIVE"]}}, "mapped to both"),
    ({"x": {"active": []}}, "needs a list"),
    ({"x": {"active": [""]}}, "empty"),
    ({"x": {"active": ["*"]}}, "empty"),
    ({"x": ["Active"]}, "expected"),
])
def test_a_bad_map_is_refused(maps, why):
    with pytest.raises(StatusConfigError, match=why):
        StatusMap(maps, VOCABULARY)


def test_a_bad_file_stops_the_load(tmp_path):
    bad = tmp_path / "status.yaml"
    bad.write_text(yaml.safe_dump({"version": 1, "sources": {"gleif": {"gone": ["ACTIVE"]}}}))
    with pytest.raises(StatusConfigError, match="gone"):
        status.load(bad)
    bad.write_text(yaml.safe_dump({"sources": {}}))
    with pytest.raises(StatusConfigError, match="version"):
        status.load(bad)
    labels = tmp_path / "labels.yaml"
    labels.write_text(yaml.safe_dump({"legal_status": ["active"]}))
    with pytest.raises(StatusConfigError, match="unknown"):
        status.categories(labels)


def test_a_source_not_built_yet_is_checked_too(tmp_path):
    path = tmp_path / "status.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "sources": {"fr-insee-sirene": {"open": ["A"]}}}))
    with pytest.raises(StatusConfigError, match="open"):
        status.load(path)


def test_the_map_is_read_from_the_configured_files(tmp_path, monkeypatch):
    path = tmp_path / "status.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "sources": {"gleif": {"merged": ["ACTIVE"]}}}))
    monkeypatch.setenv("LC_STATUS_CONFIG", str(path))
    assert status.default().category("gleif", "ACTIVE") == ("merged", True)
    monkeypatch.delenv("LC_STATUS_CONFIG")
    assert status.default().category("gleif", "ACTIVE") == ("active", True)
