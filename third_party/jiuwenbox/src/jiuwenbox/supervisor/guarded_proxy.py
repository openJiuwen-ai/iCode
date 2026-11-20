# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP proxy that lets a network-less sandbox reach public package hosts.

The sandbox has only its own loopback. It connects to this proxy through a
filesystem socket. The proxy dials the destination on the host network and
refuses loopback, link-local (including the cloud metadata address), private,
and shared address space. A name is refused when any resolved address is
refused, so a mixed DNS answer cannot be used to reach the host network.

A plaintext connection may carry more than one request. Each request is
dialed from its own target. A client that closes only its write side still
receives the origin's response.
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
# Header read and the upstream TCP handshake only. Cleared once both sockets
# are relaying, so a quiet client does not abort a download still arriving.
_HANDSHAKE_TIMEOUT_SECONDS = 10

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
    return any(address in network for network in _BLOCKED if network.version == address.version)


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


_REJECTED = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _header_value(header: bytes, name: bytes) -> str | None:
    wanted = name.lower()
    for line in header.split(b"\r\n")[1:]:
        if not line:
            continue
        key, separator, value = line.partition(b":")
        if separator and key.strip().lower() == wanted:
            return value.strip().decode("latin-1", errors="replace")
    return None


def _is_chunked(header: bytes) -> bool:
    value = _header_value(header, b"transfer-encoding")
    return value is not None and "chunked" in value.lower()


def _content_length(header: bytes) -> int | None:
    value = _header_value(header, b"content-length")
    if value is None:
        return None
    try:
        length = int(value)
    except ValueError:
        return None
    if length < 0:
        return None
    return length


def _requests_close(header: bytes) -> bool:
    """Whether the client will not send another request on this connection."""
    value = _header_value(header, b"connection")
    if value is not None:
        return "close" in value.lower()
    version = header.split(b"\r\n", 1)[0].rsplit(b" ", 1)[-1]
    return version == b"HTTP/1.0"


def _status_has_no_body(header: bytes) -> bool:
    parts = header.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2:
        return False
    try:
        code = int(parts[1])
    except ValueError:
        return False
    return code < 200 or code in (204, 304)


def _take_header(sock: socket.socket, buffered: bytes) -> tuple[bytes, bytes] | tuple[None, bytes]:
    """Split one HTTP header from ``buffered`` plus further reads.

    ``None`` means the peer closed before a complete header. Bytes after
    the header stay in the second value so the next request on a kept
    connection is not treated as part of this one.
    """
    data = bytearray(buffered)
    while b"\r\n\r\n" not in data:
        if len(data) > _HEADER_LIMIT:
            raise ValueError("proxy header is too large")
        chunk = sock.recv(4096)
        if not chunk:
            return None, bytes(data)
        data.extend(chunk)
    raw = bytes(data)
    end = raw.find(b"\r\n\r\n")
    header = raw[: end + 4]
    if len(header) > _HEADER_LIMIT:
        raise ValueError("proxy header is too large")
    return header, raw[end + 4 :]


def _take_exact(sock: socket.socket, buffered: bytes, count: int) -> tuple[bytes, bytes]:
    data = bytearray(buffered)
    while len(data) < count:
        chunk = sock.recv(_RELAY_BYTES)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data[:count]), bytes(data[count:])


def _take_chunked(sock: socket.socket, buffered: bytes) -> tuple[bytes, bytes]:
    """Return one chunked body, including its framing, and the unread tail."""
    raw = bytearray()
    pending = bytearray(buffered)
    while True:
        while b"\r\n" not in pending:
            chunk = sock.recv(_RELAY_BYTES)
            if not chunk:
                return bytes(raw), b""
            pending.extend(chunk)
        line, _, rest = bytes(pending).partition(b"\r\n")
        pending = bytearray(rest)
        raw.extend(line)
        raw.extend(b"\r\n")
        size_token = line.split(b";", 1)[0].strip()
        try:
            size = int(size_token, 16)
        except ValueError:
            return bytes(raw), bytes(pending)
        need = size + 2
        while len(pending) < need:
            chunk = sock.recv(_RELAY_BYTES)
            if not chunk:
                raw.extend(pending)
                return bytes(raw), b""
            pending.extend(chunk)
        raw.extend(pending[:need])
        del pending[:need]
        if size != 0:
            continue
        while True:
            while b"\r\n" not in pending:
                chunk = sock.recv(_RELAY_BYTES)
                if not chunk:
                    return bytes(raw), b""
                pending.extend(chunk)
            trailer, _, rest = bytes(pending).partition(b"\r\n")
            pending = bytearray(rest)
            raw.extend(trailer)
            raw.extend(b"\r\n")
            if trailer == b"":
                return bytes(raw), bytes(pending)


def _read_until_close(sock: socket.socket, buffered: bytes) -> bytes:
    data = bytearray(buffered)
    while True:
        chunk = sock.recv(_RELAY_BYTES)
        if not chunk:
            return bytes(data)
        data.extend(chunk)


def _take_request_body(sock: socket.socket, header: bytes, buffered: bytes) -> tuple[bytes, bytes]:
    """The request body, without reading a following request.

    A GET has no body. Waiting for the client to close would swallow the
    next request on a kept connection, or stall a client that has shut
    only its write side and is waiting for the response.
    """
    if _is_chunked(header):
        return _take_chunked(sock, buffered)
    length = _content_length(header)
    if length is None:
        return b"", buffered
    return _take_exact(sock, buffered, length)


def _force_upstream_close(header: bytes) -> bytes:
    """Ask the origin to end the response and close.

    The client connection stays open so its next request can name a
    different host. The origin connection does not: a kept origin would
    make the following request on this proxy connection land on the first
    host.
    """
    lines = [header.split(b"\r\n", 1)[0]]
    for line in header.split(b"\r\n")[1:]:
        if not line:
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name in (b"connection", b"proxy-connection"):
            continue
        lines.append(line)
    lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


def _read_upstream_response(upstream: socket.socket, method: str) -> bytes:
    """Read one origin response, however long the body takes to arrive."""
    header, pending = _take_header(upstream, b"")
    if header is None:
        return b""
    if method == "HEAD" or _status_has_no_body(header):
        return header
    if _is_chunked(header):
        body, _rest = _take_chunked(upstream, pending)
        return header + body
    length = _content_length(header)
    if length is None:
        return header + _read_until_close(upstream, pending)
    body, _rest = _take_exact(upstream, pending, length)
    return header + body


def _open_upstream(host: str, port: int) -> socket.socket | None:
    dial = dial_address(host)
    if dial is None:
        return None
    return socket.create_connection((dial, port), timeout=_HANDSHAKE_TIMEOUT_SECONDS)


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
        client.settimeout(_HANDSHAKE_TIMEOUT_SECONDS)
        buffered = b""
        served = False
        while True:
            try:
                header, buffered = _take_header(client, buffered)
            except TimeoutError:
                # A kept connection with no further request is finished.
                # The first header still fails as a bad gateway.
                if served:
                    return
                raise
            if header is None:
                return
            parsed = _target_of(header)
            if parsed is None or host_is_blocked(parsed[1]):
                client.sendall(_REJECTED)
                return
            method, host, port = parsed
            if method == "CONNECT":
                remote = _open_upstream(host, port)
                if remote is None:
                    client.sendall(_REJECTED)
                    return
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                client.settimeout(None)
                remote.settimeout(None)
                if buffered:
                    remote.sendall(buffered)
                _relay(client, remote)
                return
            body, buffered = _take_request_body(client, header, buffered)
            upstream = _open_upstream(host, port)
            if upstream is None:
                client.sendall(_REJECTED)
                return
            try:
                # The origin may take longer than the handshake to answer.
                # A client that has closed only its write side is still
                # waiting for that answer.
                upstream.settimeout(None)
                upstream.sendall(_force_upstream_close(header) + body)
                response = _read_upstream_response(upstream, method)
            finally:
                try:
                    upstream.close()
                except OSError:
                    pass
            if not response:
                client.sendall(_BAD_GATEWAY)
                return
            client.sendall(response)
            served = True
            if _requests_close(header):
                return
            client.settimeout(_HANDSHAKE_TIMEOUT_SECONDS)
    except (OSError, ValueError, UnicodeError):
        try:
            client.sendall(_BAD_GATEWAY)
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
    # The side that finishes first has half-closed. The other direction
    # still has to deliver a slow response; a short join drops it.
    worker.join()
