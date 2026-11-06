"""Outbound connections, pinned to an address that has been checked.

The name is resolved once, every address is checked, and the socket connects to
that exact address. A second lookup could return something else (DNS rebinding),
so the check and the connection must use the same result. TLS still verifies
the certificate against the host name.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl


class BlockedAddress(Exception):
    """The host resolves to a non-public address (Article 2.3)."""


def is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return addr.is_global and not addr.is_multicast


def resolve_public(host: str, port: int, getaddrinfo=socket.getaddrinfo) -> str:
    """Return a public address for ``host``. One private answer rejects the host."""
    try:
        infos = getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BlockedAddress(f"cannot resolve {host}") from exc
    ips = [info[4][0] for info in infos]
    if not ips:
        raise BlockedAddress(f"no address for {host}")
    bad = [ip for ip in ips if not is_public(ip)]
    if bad:
        raise BlockedAddress(f"{host} resolves to non-public address {bad[0]}")
    return ips[0]


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Upstream:
    def __init__(self, timeout: float = 30.0, resolve=resolve_public):
        self._timeout = timeout
        self._resolve = resolve
        self._tls = ssl.create_default_context()

    def open(self, scheme, host, port, method, target, headers, body=None):
        """Send the request. Returns (connection, response); close the connection when done."""
        ip = self._resolve(host, port)
        if scheme == "https":
            conn = _PinnedHTTPS(host, ip, port, self._timeout, self._tls)
        else:
            conn = _PinnedHTTP(host, ip, port, self._timeout)
        try:
            conn.request(method, target, body=body, headers=headers)
            return conn, conn.getresponse()
        except Exception:
            conn.close()
            raise
