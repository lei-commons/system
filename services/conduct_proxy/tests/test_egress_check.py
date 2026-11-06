from conduct_proxy.egress_check import describe_run_app_route, run_checks

G = "http://10.20.0.10"
A = "https://gw.test"


def get_ok(url, headers):
    if "X-Serverless-Authorization" not in headers:
        return 403, {}
    if url.endswith("/health"):
        return 200, {}
    return 503, {"X-LC-Denied": "system-not-running"}


def checks(connects=lambda h, p: False, get=get_ok):
    return run_checks(G, A, connects=connects, get=get, token=lambda aud: "tok")


def test_all_pass_on_a_sealed_network():
    assert all(ok for _, ok, _ in checks())


def test_any_direct_connection_fails_the_check():
    failed = [n for n, ok, _ in checks(connects=lambda h, p: h == "example.org") if not ok]
    assert failed == ["direct example.org:443 is blocked"]


def test_open_gateway_fails_the_check():
    def open_gateway(url, headers):
        return (200, {}) if url.endswith("/health") else get_ok(url, headers)

    assert "gateway refuses callers without identity" in [n for n, ok, _ in checks(get=open_gateway) if not ok]


def test_gateway_ignoring_paused_state_fails_the_check():
    def ignores(url, headers):
        if "X-Serverless-Authorization" not in headers:
            return 403, {}
        return (200, {}) if url.endswith("/health") else (403, {"X-LC-Denied": "denied-path"})

    assert "gateway applies the control state" in [n for n, ok, _ in checks(get=ignores) if not ok]


def test_token_is_requested_for_the_service_not_the_load_balancer():
    seen = []
    run_checks(G, A, connects=lambda h, p: False, get=get_ok, token=lambda aud: seen.append(aud) or "tok")
    assert set(seen) == {A}


def test_route_evidence_never_raises():
    def boom(url, headers):
        raise OSError("down")

    assert describe_run_app_route("https://localhost", get=boom, token=lambda a: "t") == {"error": "OSError"}
