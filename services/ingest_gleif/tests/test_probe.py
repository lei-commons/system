from ingest_gleif import probe as p


class Resp:
    def __init__(self, status, body, headers):
        self.status, self._body, self._headers = status, body, headers

    def read(self, n):
        return self._body[:n]

    def getheader(self, name, default=None):
        return {k.lower(): v for k, v in self._headers.items()}.get(name.lower(), default)

    def getheaders(self):
        return list(self._headers.items())


class Conn:
    def __init__(self, resp):
        self.resp, self.sent = resp, None

    def request(self, method, path, headers):
        self.sent = (method, path, headers)

    def getresponse(self):
        return self.resp

    def close(self):
        pass


class GW:
    _url = "https://gw"

    def __init__(self, resp):
        self.conn = Conn(resp)

    def _connect(self):
        return self.conn

    def _token(self, audience):
        return "t"


def test_an_error_shows_the_sites_own_explanation_and_only_a_kilobyte_is_asked_for():
    gw = GW(Resp(403, b"<html>Access denied: cloud provider ranges are blocked</html>", {"Content-Type": "text/html"}))
    out = p.probe(gw, "https://goldencopy.gleif.org/storage/x.zip")
    assert out["status"] == 403 and "cloud provider" in out["body"] and out["headers"]["content-type"] == "text/html"
    method, path, headers = gw.conn.sent
    assert (method, path) == ("GET", "/fetch") and headers["Range"] == "bytes=0-1023"
    assert headers["X-LC-Target"] == "https://goldencopy.gleif.org/storage/x.zip"


def test_a_success_reports_only_the_size():
    out = p.probe(GW(Resp(206, b"PK" + b"x" * 1022, {"Content-Range": "bytes 0-1023/3116234"})), "https://a.test/x")
    assert out["status"] == 206 and out["bytes"] == 1024 and "body" not in out


def test_only_an_https_url_is_accepted():
    assert p.main(["http://a.test/x"]) == 2 and p.main([]) == 2
