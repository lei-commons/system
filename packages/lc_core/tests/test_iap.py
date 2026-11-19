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


# ---- several audiences while the site moves behind a load balancer ----

from lc_core.iap import IAP_ISSUER as _ISS, audiences as _audiences, verify_iap as _verify  # noqa: E402

DIRECT = "/projects/551369190140/locations/us-central1/services/lei-commons-site"
LB = "/projects/551369190140/global/backendServices/1234567890123456789"


def _claims(aud):
    return lambda jwt, accepted: {"iss": _ISS, "aud": aud, "email": "Owner@nfh.global"}


def test_either_audience_is_accepted_during_the_move():
    both = f"{DIRECT},{LB}"
    assert _audiences(both) == [DIRECT, LB] and _audiences(" a , ,b ") == ["a", "b"]
    assert _verify("t", both, verify=_claims(DIRECT)) == "owner@nfh.global"
    assert _verify("t", both, verify=_claims(LB)) == "owner@nfh.global"


def test_after_the_move_only_the_load_balancers_audience_is():
    assert _verify("t", LB, verify=_claims(LB)) == "owner@nfh.global"
    assert _verify("t", LB, verify=_claims(DIRECT)) is None  # a direct-IAP token is refused once the site has moved
    assert _verify("t", f"{LB},", verify=_claims("/projects/1/global/backendServices/other")) is None
    assert _verify("t", "", verify=_claims(LB)) is None and _verify("t", " , ", verify=_claims(LB)) is None
    assert _verify("", LB, verify=_claims(LB)) is None


def test_the_accepted_audiences_are_given_to_the_signature_check():
    seen = []

    def verify(jwt, accepted):
        seen.append(accepted)
        return {"iss": _ISS, "aud": LB, "email": "o@x"}
    _verify("t", f"{DIRECT},{LB}", verify=verify)
    _verify("t", LB, verify=verify)
    assert seen == [[DIRECT, LB], LB]
