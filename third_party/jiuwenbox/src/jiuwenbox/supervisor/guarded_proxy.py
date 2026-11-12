# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP proxy that lets a network-less sandbox reach public package hosts.

The sandbox has only its own loopback. It connects to this proxy through a
filesystem socket. The proxy dials the destination on the host network and
refuses loopback, link-local (including the cloud metadata address), private,
and shared address space. A name is refused when any resolved address is
refused, so a mixed DNS answer cannot be used to reach the host network.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_HEADER_LIMIT = 16 * 1024
_RELAY_BYTES = 65536

# Ranges a sandboxed command must not reach through the proxy.
_BLOCKED = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


def address_is_blocked(value: str) -> bool:
    """Whether ``value`` is an IP the proxy must not dial."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            address = mapped
    return any(
        address in network
        for network in _BLOCKED
        if network.version == address.version
    )


def dial_address(host: str) -> str | None:
    """Return one address to dial, or None when ``host`` must be refused.

    A name is refused when resolution fails or any answer is blocked. The
    returned address is one of the answers that was checked, so a later DNS
    change cannot retarget the connection.
    """
    host = host.strip().strip("[]")
    if not host or "/" in host or "\\" in host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = _resolve(host)
    else:
        addresses = [host]
    if not addresses or any(address_is_blocked(item) for item in addresses):
        return None
    return addresses[0]


def host_is_blocked(host: str) -> bool:
    """Whether the proxy must refuse ``host``."""
    return dial_address(host) is None


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    found: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in found:
            found.append(address)
    return found


class GuardedProxy:
    """One unix-socket HTTP proxy for a single sandbox."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def serve(self, path: Path) -> None:
        """Bind ``path`` and accept connections until :meth:`close`."""
        if path.exists():
            path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.listen(128)
        except OSError:
            listener.close()
            raise
        self.path = path
        self._listener = listener
        self._thread = threading.Thread(
            target=self._accept,
            name="jiuwenbox-guarded-proxy",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._closed = True
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        path = self.path
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def _accept(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._closed:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(
                target=_handle_client,
                args=(client,),
                name="jiuwenbox-guarded-proxy-conn",
                daemon=True,
            ).start()


def _read_header(client: socket.socket) -> bytes:
    client.settimeout(10)
    data = bytearray()
    while b"\r\n\r\n" not in data:
        if len(data) > _HEADER_LIMIT:
            raise ValueError("proxy header is too large")
        chunk = client.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _target_of(header: bytes) -> tuple[str, str, int] | None:
    """Return method, host, and port from a proxy request header."""
    line = header.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    parts = line.split(" ")
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]
    if method == "CONNECT":
        return _split_host_port(method, target, 443)
    if target.startswith("http://"):
        rest = target[len("http://") :]
        host_port, _sep, _path = rest.partition("/")
        return _split_host_port(method, host_port, 80)
    return None


def _split_host_port(method: str, value: str, default_port: int) -> tuple[str, str, int] | None:
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            try:
                port = int(rest[1:])
            except ValueError:
                return None
        elif rest == "":
            port = default_port
        else:
            return None
    elif ":" in value:
        host, raw_port = value.rsplit(":", 1)
        try:
            port = int(raw_port)
        except ValueError:
            return None
    else:
        host = value
        port = default_port
    if not host or not 1 <= port <= 65535:
        return None
    return method, host, port


def _handle_client(client: socket.socket) -> None:
    remote: socket.socket | None = None
    try:
        header = _read_header(client)
        parsed = _target_of(header)
        if parsed is None or host_is_blocked(parsed[1]):
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        method, host, port = parsed
        dial = dial_address(host)
        if dial is None:
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        remote = socket.create_connection((dial, port), timeout=10)
        if method == "CONNECT":
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            remote.sendall(header)
        _relay(client, remote)
    except (OSError, ValueError, UnicodeError):
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass
    finally:
        if remote is not None:
            try:
                remote.close()
            except OSError:
                pass
        try:
            client.close()
        except OSError:
            pass


def _relay(left: socket.socket, right: socket.socket) -> None:
    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(_RELAY_BYTES)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    worker = threading.Thread(target=pump, args=(right, left), daemon=True)
    worker.start()
    pump(left, right)
    worker.join(timeout=2)
