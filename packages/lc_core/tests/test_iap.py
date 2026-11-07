import pytest

from lc_core.iap import IAP_ISSUER, verify_iap

AUD = "/projects/1/locations/us-central1/services/lei-commons-site"


@pytest.mark.parametrize("claims", [
    {"iss": "https://evil.test", "aud": AUD, "email": "o@x.test"}, {"iss": IAP_ISSUER, "aud": "/other", "email": "o@x.test"},
    {"iss": IAP_ISSUER, "aud": AUD}, {"iss": IAP_ISSUER, "aud": AUD, "email": ""}, {},
])
def test_bad_claims_refused(claims):
    assert verify_iap("t", AUD, verify=lambda t, a: claims) is None


def test_good_claims_accepted():
    assert verify_iap("t", AUD, verify=lambda t, a: {"iss": IAP_ISSUER, "aud": AUD, "email": "O@X.test"}) == "o@x.test"


def test_verifier_failure_refuses():
    def boom(t, a):
        raise ValueError("bad signature")

    assert verify_iap("t", AUD, verify=boom) is None


@pytest.mark.parametrize("token,aud", [(None, AUD), ("", AUD), ("t", ""), ("t", None)])
def test_missing_inputs_refuse(token, aud):
    assert verify_iap(token, aud, verify=lambda t, a: pytest.fail("verified")) is None
