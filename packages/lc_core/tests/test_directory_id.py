import pytest

from lc_core.directory_id import check_digits, is_directory_id, is_valid, mint


def test_reference_example():
    assert mint("GB", "GB-COH", "01234567") == "LECOZZFZ6LZCOJBDKT73"


def test_deterministic_and_normalised():
    a = mint("gb", " gb-coh ", "0123 4567")
    b = mint("GB", "GB-COH", "01234567")
    assert a == b


def test_shape_and_check_digits():
    for n in range(500):
        code = mint("US", "US-NY-DOS", str(n))
        assert len(code) == 20
        assert code.startswith("LECOZZ")
        assert is_valid(code)
        assert is_directory_id(code)


def test_salt_changes_id():
    assert mint("SG", "SG-ACRA", "201912345K") != mint("SG", "SG-ACRA", "201912345K", salt=1)


def test_real_lei_is_valid_but_not_directory_id():
    # GLEIF's own LEI.
    lei = "506700GE1G29325QX363"
    assert is_valid(lei)
    assert not is_directory_id(lei)


def test_single_character_error_detected():
    code = mint("BR", "BR-RFB", "11222333000181")
    bad = code[:8] + ("A" if code[8] != "A" else "B") + code[9:]
    assert not is_valid(bad)


def test_check_digits_rejects_bad_body():
    with pytest.raises(ValueError):
        check_digits("short")


@pytest.mark.parametrize("args", [("GBR", "X", "1"), ("GB", "", "1"), ("GB", "X", " ")])
def test_bad_inputs(args):
    with pytest.raises(ValueError):
        mint(*args)
