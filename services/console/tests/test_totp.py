import base64
import hashlib

import pytest

from lc_console.totp import LOCKOUT_SECONDS, MAX_FAILURES, Verifier, code_at, new_secret, otpauth_uri

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize("time,expected", [
    (59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
    (1234567890, "89005924"), (2000000000, "69279037"), (20000000000, "65353130"),
])
def test_rfc_6238_vectors(time, expected):
    assert code_at(RFC_SECRET, time // 30, digits=8, algorithm=hashlib.sha1) == expected


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def v():
    c = Clock()
    x = Verifier(c)
    x.clock = c
    return x


def now_code(v, s, offset=0):
    return code_at(s, int(v.clock.t) // 30 + offset)


def test_current_code_passes(v):
    s = new_secret()
    assert v.verify(s, now_code(v, s))


@pytest.mark.parametrize("offset", [-4, -1, 1, 4])
def test_two_minutes_of_drift_passes(v, offset):
    s = new_secret()
    assert v.verify(s, now_code(v, s, offset))


@pytest.mark.parametrize("offset", [-5, 5, 100])
def test_old_and_future_codes_fail(v, offset):
    s = new_secret()
    assert not v.verify(s, now_code(v, s, offset))


def test_a_code_cannot_be_used_twice(v):
    s = new_secret()
    c = now_code(v, s)
    assert v.verify(s, c)
    assert not v.verify(s, c)


def test_an_older_valid_code_cannot_follow_a_newer_one(v):
    s = new_secret()
    assert v.verify(s, now_code(v, s, 1))
    assert not v.verify(s, now_code(v, s, 0))


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "١٢٣٤٥٦", None, 123456])
def test_malformed_codes_fail(v, bad):
    assert not v.verify(new_secret(), bad)


def test_lockout_blocks_even_the_right_code_then_clears(v):
    s = new_secret()
    for _ in range(MAX_FAILURES):
        assert not v.verify(s, "000000" if now_code(v, s) != "000000" else "111111")
    assert v.locked()
    assert not v.verify(s, now_code(v, s))
    v.clock.t += LOCKOUT_SECONDS + 1
    assert not v.locked()
    assert v.verify(s, now_code(v, s))


def test_secrets_are_unique_and_160_bits():
    a, b = new_secret(), new_secret()
    assert a != b and len(base64.b32decode(a)) == 20


def test_otpauth_uri_escapes_the_account():
    assert "a%40b.test" in otpauth_uri("ABC", "a@b.test")
