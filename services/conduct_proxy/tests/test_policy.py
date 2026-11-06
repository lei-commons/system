from pathlib import Path

import pytest

from conduct_proxy.policy import ConfigError, Policy, normalise_path

CONFIG = Path(__file__).resolve().parents[3] / "config" / "egress-allowlist.yaml"


@pytest.fixture
def policy():
    return Policy.from_file(CONFIG)


@pytest.fixture
def approved():
    return Policy.from_file(CONFIG, {"gb-coh"})


def ev(p, url, method="GET", headers=None):
    return p.evaluate(method, url, headers or {})


COH = "https://api.company-information.service.gov.uk/company/01234567"
VERTEX = "https://aiplatform.googleapis.com/v1/projects/x:predict"


def test_real_config_loads_and_profiles_hosts(policy):
    assert ev(policy, "https://api.gleif.org/api/v1/lei-records").profile == "registry"
    assert ev(policy, "https://example.org/about").profile == "crawler"
    assert ev(policy, VERTEX, "POST").profile == "model"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"])
@pytest.mark.parametrize("url", ["https://example.org/x", "https://api.gleif.org/x"])
def test_open_web_and_registry_are_read_only(policy, method, url):
    d = ev(policy, url, method)
    assert not d.allow and d.rule == "method-not-allowed"


@pytest.mark.parametrize("path", [
    "/login", "/LOGIN", "/login/", "/a/signin", "/account", "/wp-admin/x", "/checkout", "/cart",
    "/%6cogin", "/%256cogin", "/login;jsessionid=1", "//login", "/login.php", "/sign-up", "/oauth2/authorize",
    "/register", "/forgot/password",
])
def test_login_and_account_paths_denied(policy, path):
    d = ev(policy, "https://example.org" + path)
    assert not d.allow and d.rule == "denied-path"


@pytest.mark.parametrize("path", ["/", "/about", "/blogin", "/companies/register-of-charities", "/author/x", "/data.csv"])
def test_ordinary_paths_allowed(policy, path):
    assert ev(policy, "https://example.org" + path).allow


@pytest.mark.parametrize("header", ["Authorization", "authorization", "X-Api-Key", "Proxy-Authorization"])
@pytest.mark.parametrize("url", ["https://example.org/", "https://api.gleif.org/x"])
def test_credentials_refused_on_open_hosts(policy, header, url):
    d = ev(policy, url, headers={header: "abc"})
    assert not d.allow and d.rule == "credentials-not-allowed"


@pytest.mark.parametrize("query", ["api_key=abc", "x=1&token=abc", "PASSWORD=abc", "access%5Ftoken=abc"])
def test_credentials_in_query_refused(policy, query):
    d = ev(policy, "https://example.org/x?" + query)
    assert not d.allow and d.rule == "credentials-not-allowed"


def test_owner_key_host_denied_until_approved(policy, approved):
    d = ev(policy, COH)
    assert not d.allow and d.rule == "source-not-approved"
    assert ev(approved, COH, headers={"Authorization": "Basic abc"}).allow


def test_unapproved_owner_key_host_cannot_fall_back_to_crawling(policy):
    assert ev(policy, COH, "HEAD").rule == "source-not-approved"


def test_approval_is_per_source():
    p = Policy.from_file(CONFIG, {"in-mca"})
    assert ev(p, COH).rule == "source-not-approved"


def test_credential_only_forwarded_to_approved_and_model_hosts(approved):
    assert ev(approved, COH, headers={"Authorization": "Basic abc"}).headers["authorization"] == "Basic abc"
    assert "authorization" in ev(approved, VERTEX, "POST", {"Authorization": "Bearer abc"}).headers


def test_cookies_never_forwarded(approved):
    for d in (ev(approved, "https://example.org/", headers={"Cookie": "a=1"}),
              ev(approved, COH, headers={"Cookie": "a=1"})):
        assert d.allow and "cookie" not in d.headers


def test_unknown_headers_dropped_and_user_agent_forced(policy):
    d = ev(policy, "https://example.org/", headers={"User-Agent": "something-else", "X-Forwarded-For": "1.2.3.4", "Accept": "*/*"})
    assert d.headers == {"accept": "*/*", "user-agent": policy.user_agent}


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://169.254.169.254/computeMetadata/v1/", "http://[::1]/", "http://10.0.0.1/",
    "http://2130706433/", "http://0x7f.1/", "http://127.1/",
])
def test_ip_literals_denied(policy, url):
    d = ev(policy, url)
    assert not d.allow and d.rule == "ip-literal"


@pytest.mark.parametrize("url,rule", [
    ("https://user:pw@example.org/", "url-credentials"),
    ("https://example.org:8443/", "bad-port"),
    ("http://example.org:443/", "bad-port"),
    ("ftp://example.org/", "bad-url"),
    ("file:///etc/passwd", "bad-url"),
    ("", "bad-url"),
    ("https://./", "bad-url"),
    ("https://example.org/a b", "bad-url"),
    ("https://example.org/\r\nX: y", "bad-url"),
    ("https://example.org\\@evil.test/", "bad-url"),
    ("https://exämple.org/", "bad-url"),
    ("https://example.org:notaport/", "bad-url"),
])
def test_malformed_targets_denied(policy, url, rule):
    d = ev(policy, url)
    assert not d.allow and d.rule == rule


def test_official_hosts_need_https(policy):
    assert ev(policy, "http://api.gleif.org/x").rule == "https-required"
    assert ev(policy, "http://example.org/x").allow


def test_host_match_is_exact_and_case_insensitive(approved, policy):
    assert ev(policy, "https://API.GLEIF.ORG./x").profile == "registry"
    assert ev(policy, "https://api.gleif.org.evil.test/x").profile == "crawler"


def test_model_host_only_takes_post(policy):
    assert ev(policy, VERTEX).rule == "method-not-allowed"


def test_normalise_path_keeps_directory_dots_harmless():
    assert normalise_path("/a/../login.html") == "/a/../login"
    assert normalise_path("") == "/"


def good_cfg():
    return {
        "registry_hosts": ["r.test"], "model_hosts": ["m.test"], "owner_key_hosts": [{"host": "k.test", "source": "s"}],
        "crawler_policy": {"methods": ["GET", "HEAD"], "max_rps_per_host": 1, "honour_robots_txt": True,
                           "send_cookies": False, "send_auth_headers": False, "user_agent": "ua"},
    }


@pytest.mark.parametrize("change", [
    {"methods": ["GET", "POST"]}, {"methods": []}, {"max_rps_per_host": 2}, {"max_rps_per_host": 0},
    {"honour_robots_txt": False}, {"send_cookies": True}, {"send_auth_headers": True},
    {"deny_paths_matching": ["("]},
])
def test_config_weakening_article_2_refuses_to_load(change):
    cfg = good_cfg()
    cfg["crawler_policy"].update(change)
    with pytest.raises(ConfigError):
        Policy(cfg)


@pytest.mark.parametrize("key", ["registry_hosts", "model_hosts", "owner_key_hosts", "crawler_policy"])
def test_incomplete_config_refuses_to_load(key):
    cfg = good_cfg()
    del cfg[key]
    with pytest.raises(ConfigError):
        Policy(cfg)


def test_missing_config_file_refuses_to_load(tmp_path):
    with pytest.raises(ConfigError):
        Policy.from_file(tmp_path / "nope.yaml")
