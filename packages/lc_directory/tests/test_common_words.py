import pytest

pytest.importorskip("psycopg")
from lc_directory.pg import Directory  # noqa: E402


def directory(common):
    d = object.__new__(Directory)
    d.common = set(common)
    return d


def test_only_queries_made_entirely_of_common_words_take_the_scan():
    d = directory({"limited", "holdings", "uk"})
    assert d._all_common("Limited") and d._all_common("holdings limited") and d._all_common("UK, Limited")
    assert not d._all_common("acme limited")  # one rare word: the index finds it fast
    assert not d._all_common('"holdings limited"') and not d._all_common("limited -holdings")
    assert not d._all_common("limited or holdings") and not d._all_common("") and not d._all_common("!!")
