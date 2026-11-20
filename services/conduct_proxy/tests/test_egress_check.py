from conduct_proxy.egress_check import direct_checks, gateway_checks

G = "http://10.20.0.10"
A = "https://gw.test"


def get_ok(url, headers):
    if "X-Serverless-Authorization" not in headers:
        return 403, {}
    if url.endswith("/health"):
        return 200, {}
    return 503, {"X-LC-Denied": "system-not-running"}


def checks(get=get_ok, token=lambda aud: "tok"):
    return gateway_checks("lb", G, A, get=get, token=token)


def failed(results):
    return [n for n, ok, _ in results if not ok]


def test_all_pass_on_a_sealed_network():
    assert failed(direct_checks(connects=lambda h, p: False)) == []
    assert failed(checks()) == []


def test_any_direct_connection_fails_the_check():
    assert failed(direct_checks(connects=lambda h, p: h == "example.org")) == ["direct example.org:443 is blocked"]


def test_open_gateway_fails_the_check():
    def open_gateway(url, headers):
        return (200, {}) if url.endswith("/health") else get_ok(url, headers)

    assert failed(checks(get=open_gateway)) == ["lb: refuses callers without identity"]


def test_gateway_ignoring_paused_state_fails_the_check():
    def ignores(url, headers):
        if "X-Serverless-Authorization" not in headers:
            return 403, {}
        return (200, {}) if url.endswith("/health") else (403, {"X-LC-Denied": "denied-path"})

    assert failed(checks(get=ignores)) == ["lb: applies the control state"]


def test_slow_target_still_shows_the_control_state_applied():
    # 2026-11-20 07:20: GLEIF was slow; the gateway let the request through and answered
    # 502 upstream-error. That is a running gateway applying the control state, not a fault.
    def slow_target(url, headers):
        if url.endswith("/fetch") and "X-Serverless-Authorization" in headers:
            return 502, {"X-LC-Denied": "upstream-error"}
        return get_ok(url, headers)

    results = checks(get=slow_target)
    assert failed(results) == []
    assert results[-1] == ("lb: applies the control state", True, "502 upstream-error")


def test_other_gateway_refusals_still_fail_the_control_state_check():
    def refuses(url, headers):
        if url.endswith("/fetch") and "X-Serverless-Authorization" in headers:
            return 502, {"X-LC-Denied": "bad-gateway"}
        return get_ok(url, headers)

    assert failed(checks(get=refuses)) == ["lb: applies the control state"]


def test_check_waits_longer_than_the_gateway_waits_for_its_target():
    from conduct_proxy.egress_check import FETCH_TIMEOUT_S
    from conduct_proxy.upstream import UPSTREAM_TIMEOUT_S, Upstream

    assert FETCH_TIMEOUT_S > UPSTREAM_TIMEOUT_S
    assert Upstream()._timeout == UPSTREAM_TIMEOUT_S


def test_ingress_404_fails_every_gateway_check():
    assert len(failed(checks(get=lambda url, headers: (404, {})))) == 3


def test_unreachable_route_is_a_failure_not_a_crash():
    def down(url, headers):
        raise OSError("no route")

    assert checks(get=down) == [("lb: connects", False, "OSError")]


def test_token_is_requested_for_the_service_not_the_load_balancer():
    seen = []
    checks(token=lambda aud: seen.append(aud) or "tok")
    assert set(seen) == {A}
