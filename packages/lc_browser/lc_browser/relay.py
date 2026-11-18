"""The browser's proxy: a relay in the same container that sends every request through the conduct gateway.

Chromium is started with this relay as its proxy. For an https page it asks CONNECT host:443; the relay
answers, ends the browser's TLS itself with a certificate made at start-up (the browser context is told
to accept it: the link is inside this container, and the real site's certificate is checked by the
gateway), reads each HTTP request, and sends it to the gateway's /fetch as an ordinary call with
X-LC-Target, so the gateway decides as for any fetch (browser profile, allow-list, robots.txt, pacing,
the fixed address). The answer streams back to the browser. Plain http, and anything the gateway
refuses, never leaves. One request per connection: simple, and the gateway paces them anyway.
"""

from __future__ import annotations

import datetime as dt
import http.client
import socketserver
import ssl
import tempfile
import threading
from pathlib import Path

CHUNK = 64 * 1024
MAX_BODY = 10 * 1024 * 1024
MAX_HEADER_LINES = 100
# Request headers the relay passes on (the gateway keeps only what its browser profile allows).
PASS_REQUEST = {"accept", "accept-encoding", "accept-language", "content-type", "cookie", "origin", "referer",
                "range", "user-agent", "if-modified-since", "if-none-match"}
# Response headers never passed back (hop-by-hop, or set by the relay itself).
DROP_RESPONSE = {"connection", "transfer-encoding", "keep-alive", "content-length", "x-lc-denied"}


def self_signed(directory: str) -> tuple:
    """A certificate and key for the relay's end of the browser's TLS, made fresh for each run."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lei-commons browser relay")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("*")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = Path(directory) / "relay.crt", Path(directory) / "relay.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def _read_head(stream):
    """(request line, headers) from a binary stream, or None at end of stream."""
    line = stream.readline(8192)
    if not line:
        return None
    headers = []
    for _ in range(MAX_HEADER_LINES):
        h = stream.readline(8192)
        if h in (b"\r\n", b"\n", b""):
            break
        name, _, value = h.decode("latin-1").partition(":")
        headers.append((name.strip(), value.strip()))
    else:
        raise ValueError("too many header lines")
    return line.decode("latin-1").strip(), headers


class Relay(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """``Relay(gateway, allowed_hosts, log).start()``; ``.proxy`` is the address for Chromium."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, gateway, allowed_hosts, log=print, actor="lc-browser"):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.gateway, self.allowed = gateway, {h.lower() for h in allowed_hosts}
        self.log, self.actor = log, actor
        self._dir = tempfile.mkdtemp(prefix="relay-")
        cert, key = self_signed(self._dir)
        self.tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.tls.load_cert_chain(cert, key)
        self.tls.set_alpn_protocols(["http/1.1"])
        self.requests, self.refused = 0, 0

    @property
    def proxy(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> "Relay":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()

    def forward(self, method, url, headers, body):
        """Send one request through the gateway. Returns (connection, response)."""
        gw = self.gateway
        conn = gw._connect()
        sent = {k: v for k, v in headers if k.lower() in PASS_REQUEST}
        sent.update({"X-LC-Target": url, "X-LC-Actor": self.actor,
                     "X-Serverless-Authorization": f"Bearer {gw._token(gw._url)}"})
        if body is not None:
            sent["Content-Length"] = str(len(body))
        conn.request(method, "/fetch", body=body, headers=sent)
        return conn, conn.getresponse()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        relay = self.server
        try:
            head = _read_head(self.rfile)
            if head is None:
                return
            line, _ = head
            method, target, _ = (line.split(" ") + ["", ""])[:3]
            if method != "CONNECT":
                return self._plain(403, "only https pages are fetched")
            host, _, port = target.rpartition(":")
            host = host.strip("[]").lower()
            if port != "443" or host not in relay.allowed:
                relay.refused += 1
                relay.log({"event": "browser-refused", "host": host, "reason": "not a browser host for this source"})
                return self._plain(403, "not a browser host for this source")
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            tls = relay.tls.wrap_socket(self.connection, server_side=True)
            self._serve(tls, host)
        except (OSError, ValueError, ssl.SSLError):
            pass  # the browser went away, or sent something unreadable

    def _plain(self, status, reason):
        body = reason.encode()
        self.wfile.write(b"HTTP/1.1 %d Refused\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                         % (status, len(body), body))

    def _serve(self, tls, host):
        relay = self.server
        reader, writer = tls.makefile("rb"), tls.makefile("wb")
        try:
            head = _read_head(reader)
            if head is None:
                return
            line, headers = head
            method, path, _ = (line.split(" ") + ["", ""])[:3]
            lowered = {k.lower(): v for k, v in headers}
            if "chunked" in lowered.get("transfer-encoding", "").lower():
                return _write_plain(writer, 411, "a request body needs a length")
            length = int(lowered.get("content-length") or 0)
            if length > MAX_BODY:
                return _write_plain(writer, 413, "request body too large")
            body = reader.read(length) if length else None
            relay.requests += 1
            try:
                conn, resp = relay.forward(method, f"https://{host}{path}", headers, body if method == "POST" else None)
            except (OSError, http.client.HTTPException) as exc:
                return _write_plain(writer, 502, f"gateway unreachable: {type(exc).__name__}")
            try:
                denied = resp.getheader("X-LC-Denied")
                if denied:
                    relay.log({"event": "browser-denied", "host": host, "rule": denied, "status": resp.status})
                writer.write(b"HTTP/1.1 %d %s\r\n" % (resp.status, (resp.reason or "").encode("latin-1", "replace")))
                for name, value in resp.getheaders():
                    if name.lower() not in DROP_RESPONSE:
                        writer.write(f"{name}: {value}\r\n".encode("latin-1", "replace"))
                writer.write(b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    writer.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                writer.write(b"0\r\n\r\n")
                writer.flush()
            finally:
                conn.close()
        finally:
            for f in (reader, writer):
                try:
                    f.close()
                except OSError:
                    pass
            try:
                tls.close()
            except OSError:
                pass


def _write_plain(writer, status, reason):
    body = reason.encode()
    writer.write(b"HTTP/1.1 %d Refused\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                 % (status, len(body), body))
    writer.flush()
