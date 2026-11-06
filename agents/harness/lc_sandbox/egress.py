"""Allow-list egress for the runner sandbox.

The agent container sits on a Docker network with no route out, and this is
its only neighbour with one. It offers CONNECT to allow-listed hosts on port
443 and nothing else.

Tunnelling is enough here, unlike the conduct gateway: the job is to limit
where an agent's tools can connect, not to inspect web traffic. Connections
are pinned to a checked public address, so names can't lead to private ones.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path

import yaml
from conduct_proxy.upstream import BlockedAddress, resolve_public

MAX_REQUEST_BYTES = 8192
CONNECT_TIMEOUT = 15


class AllowList:
    def __init__(self, cfg: dict):
        hosts, suffixes = cfg.get("hosts"), cfg.get("suffixes")
        if not hosts or suffixes is None:
            raise ValueError("sandbox egress config needs hosts and suffixes")
        if not all(s.startswith(".") and s.count(".") >= 2 for s in suffixes):
            raise ValueError("suffixes must look like .example.com")
        self.hosts = {h.lower() for h in hosts}
        self.suffixes = tuple(s.lower() for s in suffixes)

    @classmethod
    def from_file(cls, path) -> "AllowList":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    def allows(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return host in self.hosts or host.endswith(self.suffixes)


def parse_connect(head: bytes):
    """Return (host, port) from a CONNECT request, or None if it isn't one."""
    try:
        parts = head.split(b"\r\n", 1)[0].decode("ascii").split(" ")
    except UnicodeDecodeError:
        return None
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    host, sep, port = parts[1].rpartition(":")
    if not sep or not host or not port.isdigit():
        return None
    return host, int(port)


def decide(head: bytes, allow: AllowList):
    """Return (host, port, rule). Connect only when rule is 'allowed'."""
    target = parse_connect(head)
    if target is None:
        return "", 0, "not-connect"
    host, port = target
    if port != 443:
        return host, port, "bad-port"
    if not allow.allows(host):
        return host, port, "host-not-allowed"
    return host, port, "allowed"


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Handler(socketserver.BaseRequestHandler):
    allow: AllowList = None

    def _log(self, host, port, rule):
        print(json.dumps({"ts": time.time(), "host": host, "port": port, "rule": rule, "allowed": rule == "allowed"}), flush=True)

    def _read_head(self):
        head = b""
        while b"\r\n\r\n" not in head and len(head) < MAX_REQUEST_BYTES:
            chunk = self.request.recv(1024)
            if not chunk:
                break
            head += chunk
        return head

    def handle(self):
        self.request.settimeout(CONNECT_TIMEOUT)
        host, port, rule = decide(self._read_head(), self.allow)
        if rule != "allowed":
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return self._log(host, port, rule)
        try:
            upstream = socket.create_connection((resolve_public(host, port), port), CONNECT_TIMEOUT)
        except BlockedAddress:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
            return self._log(host, port, "non-public-address")
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return self._log(host, port, "upstream-error")
        self._log(host, port, "allowed")
        self.request.settimeout(None)
        upstream.settimeout(None)
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        back = threading.Thread(target=_pipe, args=(upstream, self.request), daemon=True)
        back.start()
        _pipe(self.request, upstream)
        back.join(5)
        upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(allow: AllowList, address) -> Server:
    return Server(address, type("BoundHandler", (Handler,), {"allow": allow}))


def main() -> int:
    allow = AllowList.from_file(os.environ.get("LC_SANDBOX_EGRESS", "config/sandbox-egress.yaml"))
    make_server(allow, ("0.0.0.0", 3128)).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
