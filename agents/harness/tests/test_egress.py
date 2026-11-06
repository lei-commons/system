import socket
import threading
from pathlib import Path

import pytest

from lc_sandbox.egress import AllowList, decide, make_server, parse_connect
from lc_sandbox.selfcheck import run_checks

CONFIG = Path(__file__).resolve().parents[3] / "config" / "sandbox-egress.yaml"


@pytest.fixture
def allow():
    return AllowList.from_file(CONFIG)


@pytest.mark.parametrize("host", ["pypi.org", "PYPI.org", "api.github.com", "aiplatform.googleapis.com", "pypi.org."])
def test_allowed_hosts(allow, host):
    assert allow.allows(host)


@pytest.mark.parametrize("host", [
    "example.org", "pypi.org.evil.test", "evilpypi.org", "googleapis.com.evil.test", "evilgoogleapis.com", "", "10.0.0.1",
])
def test_refused_hosts(allow, host):
    assert not allow.allows(host)


@pytest.mark.parametrize("cfg", [{}, {"hosts": ["a.test"]}, {"hosts": [], "suffixes": []},
                                 {"hosts": ["a.test"], "suffixes": [".com"]}, {"hosts": ["a.test"], "suffixes": ["a.test"]}])
def test_bad_config_refuses_to_load(cfg):
    with pytest.raises(ValueError):
        AllowList(cfg)


def head(line):
    return line.encode() + b"\r\n\r\n"


def test_parse_connect():
    assert parse_connect(head("CONNECT pypi.org:443 HTTP/1.1")) == ("pypi.org", 443)


@pytest.mark.parametrize("line", ["GET http://pypi.org/ HTTP/1.1", "CONNECT pypi.org HTTP/1.1", "CONNECT :443 HTTP/1.1",
                                  "CONNECT pypi.org:abc HTTP/1.1", "CONNECT pypi.org:443", "junk", ""])
def test_anything_but_connect_is_refused(allow, line):
    assert decide(head(line), allow)[2] == "not-connect"


def test_only_port_443(allow):
    assert decide(head("CONNECT pypi.org:22 HTTP/1.1"), allow)[2] == "bad-port"


def test_host_not_on_the_list(allow):
    assert decide(head("CONNECT example.org:443 HTTP/1.1"), allow)[2] == "host-not-allowed"


def test_server_answers_403_and_closes(allow, capsys):
    srv = make_server(allow, ("127.0.0.1", 0))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = socket.create_connection(srv.server_address, timeout=5)
    s.sendall(head("CONNECT example.org:443 HTTP/1.1"))
    assert s.recv(100).startswith(b"HTTP/1.1 403")
    s.close()
    srv.shutdown()
    srv.server_close()


def test_selfcheck_fails_if_anything_gets_out():
    sealed = dict(direct=lambda h, p: False, via=lambda p, h: 200 if h == "pypi.org" else 403)
    assert all(ok for _, ok in run_checks("p", **sealed))
    leaky = {**sealed, "direct": lambda h, p: h == "169.254.169.254"}
    assert [n for n, ok in run_checks("p", **leaky) if not ok] == ["metadata server is blocked"]
    open_proxy = {**sealed, "via": lambda p, h: 200}
    assert [n for n, ok in run_checks("p", **open_proxy) if not ok] == ["proxy refuses example.org"]
