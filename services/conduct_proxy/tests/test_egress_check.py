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
