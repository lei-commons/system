"""The second factor's stored states: set up, rotate, confirm, cancel (lc_core.second_factor)."""

import pytest

from lc_core import second_factor as sf


def test_setup_only_when_nothing_is_set_up():
    first = sf.begin_setup(None)
    assert sf.status(first) == "setting-up" and sf.active(first) is None
    assert sf.begin_setup(first) is None  # a waiting key is kept, never replaced
    key = sf.confirm(first)
    assert sf.status(key) == "active" and sf.active(key) == key
    assert sf.begin_setup(key) is None


def test_rotation_keeps_the_current_key_until_confirmed():
    current = sf.new_key()
    rotating = sf.begin_rotation(current)
    assert sf.status(rotating) == "rotating" and sf.active(rotating) == current
    new = sf.confirm(rotating)
    assert new != current and sf.active(new) == new
    assert sf.cancel_rotation(sf.begin_rotation(current)) == current


def test_impossible_steps_refuse():
    with pytest.raises(ValueError):
        sf.begin_rotation(None)
    with pytest.raises(ValueError):
        sf.begin_rotation("pending:" + sf.new_key())
    with pytest.raises(ValueError):
        sf.confirm(sf.new_key())
    with pytest.raises(ValueError):
        sf.cancel_rotation(sf.new_key())


def test_keys_are_base32_and_the_uri_names_the_account():
    import base64

    key = sf.new_key()
    assert len(base64.b32decode(key)) == 20
    assert sf.otpauth_uri(key, "o+x@nfh.global").startswith("otpauth://totp/lei-commons:o%2Bx%40nfh.global?secret=")
