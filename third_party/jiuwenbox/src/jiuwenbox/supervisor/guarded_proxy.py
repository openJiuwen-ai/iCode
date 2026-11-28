# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HTTP proxy that lets a network-less sandbox reach public package hosts.

The sandbox has only its own loopback. It connects to this proxy through a
filesystem socket. The proxy dials the destination on the host network and
refuses loopback, link-local (including the cloud metadata address), private,
shared, IPv6 transition (NAT64, 6to4, Teredo) and any other address that is
not globally routable. A name is refused when any resolved address is
refused, so a mixed DNS answer cannot be used to reach the host network; the
checked addresses are then dialed in order until one answers.

The proxy runs in the host server, outside the sandbox's memory limit, so it
never holds a whole message: bodies are copied in pieces of at most
``_RELAY_BYTES`` and a head may not exceed ``_HEADER_LIMIT``.

Plaintext requests use the absolute form (``GET http://host/path``). Each
request is dialed from its own target with ``Connection: close`` and is sent
to the origin in origin form with the target's ``Host``. Request framing is
strict: one ``Content-Length`` value, or ``Transfer-Encoding`` ending in
``chunked`` without a length, and no folded or malformed header line;
anything else is ``400`` and closes the connection, so a smuggled second
request is never read. Interim ``1xx`` responses are passed on and the final
response follows. An origin that ends a response early, or a response
delimited by the origin closing, closes the client connection too, so the
client sees the cut instead of a later response spliced into the body.

``CONNECT`` relays bytes both ways. Every connection, plaintext or tunnel,
ends after ``_IDLE_TIMEOUT_SECONDS`` with no bytes moving in either
direction; a slow transfer that keeps moving is not cut. :meth:`GuardedProxy.close`
stops accepting and shuts down every open connection.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import selectors
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

_HEADER_LIMIT = 16 * 1024
_CHUNK_LINE_LIMIT = 4096
_RELAY_BYTES = 65536
# Request header read and each upstream TCP handshake.
_HANDSHAKE_TIMEOUT_SECONDS = 10
# A connection with no bytes moving in either direction for this long ends.
_IDLE_TIMEOUT_SECONDS = 300.0
# How long a request with ``Expect: 100-continue`` waits for the origin's
# answer before the body is sent anyway, as clients do.
_CONTINUE_WAIT_SECONDS = 1.0
# How long the rest of a request body is read and dropped after an early
# response, so that closing does not reset the connection under it.
_LINGER_SECONDS = 2.0

# Ranges a sandboxed command must not reach through the proxy, on top of
# every address ``ipaddress`` does not call global.
_BLOCKED = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/96"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0:0/96"),
    # NAT64 (well-known and local-use), 6to4 and Teredo carry an IPv4
    # address the host network would reach on the sandbox's behalf.
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("2001::/32"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

_TOKEN = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_DIGITS = re.compile(r"[0-9]+")
_HEX = re.compile(rb"[0-9A-Fa-f]+")
_VERSION = re.compile(rb"HTTP/1\.[01]")
_STATUS = re.compile(rb"HTTP/1\.[01] ([0-9]{3})(?: .*)?")
# Hop-by-hop fields, removed before a head is passed on. ``Transfer-Encoding``
# is kept: the body is passed on with the framing it arrived in.
_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"upgrade",
    }
)


def address_is_blocked(value: str) -> bool:
    """Whether ``value`` is an IP the proxy must not dial."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            return True
        mapped = address.ipv4_mapped
        if mapped is not None:
            address = mapped
    if any(address in network for network in _BLOCKED if network.version == address.version):
        return True
    return not address.is_global


def dial_addresses(host: str) -> list[str] | None:
    """Every address to try for ``host``, or None when it must be refused.

    A name is refused when resolution fails or any answer is blocked. Only
    checked answers are returned, so a later DNS change cannot retarget the
    connection.
    """
    host = host.strip().strip("[]")
    if not host or "/" in host or "\\" in host or "%" in host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = _resolve(host)
    else:
        addresses = [host]
    if not addresses or any(address_is_blocked(item) for item in addresses):
        return None
    return addresses


def host_is_blocked(host: str) -> bool:
    """Whether the proxy must refuse ``host``."""
    return dial_addresses(host) is None


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        return []
    found: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in found:
            found.append(address)
    return found


class _Connections:
    """Open sockets of one proxy, so :meth:`GuardedProxy.close` can end them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: set[socket.socket] = set()
        self._closed = False

    def add(self, sock: socket.socket) -> bool:
        """Track ``sock``; False once the proxy is closing."""
        with self._lock:
            if self._closed:
                return False
            self._open.add(sock)
            return True

    def close(self, sock: socket.socket) -> None:
        # Under the lock, so ``shut_all`` never shuts down a socket whose
        # descriptor number another connection may already reuse.
        with self._lock:
            self._open.discard(sock)
            _close_quietly(sock)

    def shut_all(self) -> None:
        """Wake every connection thread. Each closes its own sockets."""
        with self._lock:
            self._closed = True
            for sock in self._open:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @contextmanager
    def tracked(self, sock: socket.socket) -> Iterator[socket.socket]:
        if not self.add(sock):
            _close_quietly(sock)
            raise _Closing
        try:
            yield sock
        finally:
            self.close(sock)


class GuardedProxy:
    """One unix-socket HTTP proxy for a single sandbox."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._connections = _Connections()

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
            args=(listener,),
            name="jiuwenbox-guarded-proxy",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop accepting and shut down every open connection."""
        listener = self._listener
        self._listener = None
        if listener is not None:
            # ``close`` alone does not wake a thread blocked in ``accept``.
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            _close_quietly(listener)
        self._connections.shut_all()
        path = self.path
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(
                target=_handle_client,
                args=(client, self._connections),
                name="jiuwenbox-guarded-proxy-conn",
                daemon=True,
            ).start()


_REJECTED = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_REQUEST = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class _Sink(Protocol):
    def sendall(self, data: bytes, /) -> None: ...


class _BadMessage(ValueError):
    """A head or body framing the proxy will not pass on."""


class _BadGateway(OSError):
    """The origin sent a response the proxy will not pass on."""


class _Truncated(OSError):
    """The peer closed in the middle of a message."""


class _Closing(Exception):
    """The proxy is closing; the connection ends without a reply."""


class _OriginAnswered(Exception):
    """The origin sent its final response before the request body was complete."""

    def __init__(self, head: _Head) -> None:
        super().__init__("origin answered during the request body")
        self.head = head


def _close_quietly(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


class _Stream:
    """A socket with a small read-ahead buffer."""

    def __init__(self, sock: socket.socket, buffered: bytes = b"") -> None:
        self.sock = sock
        self.buffer = bytearray(buffered)

    def _fill(self) -> bool:
        chunk = self.sock.recv(_RELAY_BYTES)
        if not chunk:
            return False
        self.buffer.extend(chunk)
        return True

    def read_head(self) -> bytes | None:
        """One head through its blank line; None when the peer closed before any byte."""
        while True:
            end = self.buffer.find(b"\r\n\r\n")
            if end >= 0:
                if end + 4 > _HEADER_LIMIT:
                    raise _BadMessage("head is too large")
                head = bytes(self.buffer[: end + 4])
                del self.buffer[: end + 4]
                return head
            if len(self.buffer) > _HEADER_LIMIT:
                raise _BadMessage("head is too large")
            if not self._fill():
                if self.buffer:
                    raise _Truncated("peer closed inside a head")
                return None

    def read_line(self, limit: int) -> bytes:
        """One CRLF-terminated line without its CRLF."""
        while True:
            end = self.buffer.find(b"\r\n")
            if end >= 0:
                if end > limit:
                    raise _BadMessage("line is too long")
                line = bytes(self.buffer[:end])
                del self.buffer[: end + 2]
                return line
            # One more byte may be the CR of a CRLF split across reads.
            if len(self.buffer) > limit + 1:
                raise _BadMessage("line is too long")
            if not self._fill():
                raise _Truncated("peer closed inside a line")

    def read_some(self, limit: int) -> bytes:
        """Up to ``limit`` bytes; empty only when the peer has closed."""
        if not self.buffer and not self._fill():
            return b""
        piece = bytes(self.buffer[:limit])
        del self.buffer[:limit]
        return piece

    def has_data(self, timeout: float) -> bool:
        if self.buffer:
            return True
        with selectors.DefaultSelector() as selector:
            selector.register(self.sock, selectors.EVENT_READ)
            return bool(selector.select(timeout))


class _Upload(_Stream):
    """A request body on its way from the client to the origin.

    An origin may answer before the body is complete (``413`` is typical)
    and stop reading it. Every wait for client bytes, and for room to send
    them upstream, also watches the origin: an interim response is passed on
    and the body continues, a final one ends the body with
    :class:`_OriginAnswered`. Reads share the client stream's buffer.
    """

    def __init__(self, client: _Stream, origin: _Stream, *, interim_ok: bool) -> None:
        super().__init__(client.sock)
        self.buffer = client.buffer
        self._origin = origin
        self._interim_ok = interim_ok

    def _fill(self) -> bool:
        while not self._ready(self.sock, selectors.EVENT_READ):
            pass
        return super()._fill()

    def sendall(self, data: bytes) -> None:
        upstream = self._origin.sock
        view = memoryview(data)
        while view:
            if not self._ready(upstream, selectors.EVENT_WRITE):
                continue
            upstream.setblocking(False)
            try:
                sent = upstream.send(view)
            except BlockingIOError:
                continue
            except OSError:
                # An origin that answered and closed resets the upload.
                if self._origin.has_data(0):
                    self._answer()
                raise
            finally:
                upstream.settimeout(_IDLE_TIMEOUT_SECONDS)
            view = view[sent:]

    def _ready(self, sock: socket.socket, event: int) -> bool:
        """Wait until ``sock`` is ready for ``event``; False after passing on an interim response."""
        if self._origin.buffer:
            self._answer()
            return False
        upstream = self._origin.sock
        with selectors.DefaultSelector() as selector:
            if sock is upstream:
                selector.register(upstream, selectors.EVENT_READ | event)
            else:
                selector.register(sock, event)
                selector.register(upstream, selectors.EVENT_READ)
            events = selector.select(_IDLE_TIMEOUT_SECONDS)
        if not events:
            raise TimeoutError("request body stalled")
        for key, mask in events:
            if key.fileobj is upstream and mask & selectors.EVENT_READ:
                self._answer()
                return False
        return True

    def _answer(self) -> None:
        """Read what the origin sent: pass an interim response on, raise on a final one."""
        try:
            raw = self._origin.read_head()
            if raw is None:
                raise _Truncated("origin closed during the request body")
            head = _parse_head(raw)
            status = _status_of(head)
        except _BadMessage as exc:
            raise _BadGateway(str(exc)) from exc
        if status == 101 or status >= 200:
            raise _OriginAnswered(head)
        if self._interim_ok:
            self.sock.sendall(raw)


@dataclass(frozen=True)
class _Head:
    start: bytes
    fields: list[tuple[bytes, bytes]]

    def values(self, name: bytes) -> list[bytes]:
        """Comma-separated values of every ``name`` field, in order."""
        found: list[bytes] = []
        for key, value in self.fields:
            if key.lower() != name:
                continue
            found.extend(item.strip() for item in value.split(b",") if item.strip())
        return found

    def has(self, name: bytes) -> bool:
        return any(key.lower() == name for key, _value in self.fields)


def _check_line(line: bytes) -> bytes:
    if b"\n" in line or b"\r" in line or b"\x00" in line:
        raise _BadMessage("stray line break or NUL")
    return line


def _parse_field(line: bytes) -> tuple[bytes, bytes]:
    _check_line(line)
    if line[:1] in (b" ", b"\t"):
        raise _BadMessage("folded header line")
    name, separator, value = line.partition(b":")
    if not separator or not _TOKEN.fullmatch(name):
        raise _BadMessage("malformed header line")
    return name, value.strip(b" \t")


def _parse_head(raw: bytes) -> _Head:
    lines = raw[:-4].split(b"\r\n")
    return _Head(_check_line(lines[0]), [_parse_field(line) for line in lines[1:]])


def _connection_tokens(head: _Head) -> set[bytes]:
    return {item.lower() for item in head.values(b"connection")}


def _without_hop_by_hop(head: _Head, *, keep: tuple[bytes, ...] = ()) -> list[tuple[bytes, bytes]]:
    dropped = _HOP_BY_HOP | _connection_tokens(head)
    return [
        (name, value)
        for name, value in head.fields
        if name.lower() in keep or name.lower() not in dropped
    ]


def _serialize(start: bytes, fields: list[tuple[bytes, bytes]]) -> bytes:
    lines = [start, *(name + b": " + value for name, value in fields)]
    return b"\r\n".join(lines) + b"\r\n\r\n"


@dataclass(frozen=True)
class _Request:
    head: _Head
    method: str
    version: bytes
    host: str
    port: int
    authority: bytes
    path: bytes


def _parse_request(raw: bytes) -> _Request:
    head = _parse_head(raw)
    parts = head.start.split(b" ")
    if len(parts) != 3 or not _TOKEN.fullmatch(parts[0]) or not _VERSION.fullmatch(parts[2]):
        raise _BadMessage("malformed request line")
    method = parts[0].decode("ascii").upper()
    target = parts[1]
    if method == "CONNECT":
        parsed = _split_host_port(target.decode("latin-1"), 443)
        if parsed is None:
            raise _BadMessage("malformed CONNECT target")
        return _Request(head, method, parts[2], parsed[0], parsed[1], target, b"")
    if target[:7].lower() != b"http://":
        raise _BadMessage("the request target is not an absolute http URL")
    rest = target[7:].split(b"#", 1)[0]
    cut = len(rest)
    for marker in (b"/", b"?"):
        index = rest.find(marker)
        if index >= 0:
            cut = min(cut, index)
    authority, path = rest[:cut], rest[cut:]
    if path.startswith(b"?"):
        path = b"/" + path
    if not path:
        path = b"/"
    if b"@" in authority:
        raise _BadMessage("userinfo in the request target")
    parsed = _split_host_port(authority.decode("latin-1"), 80)
    if parsed is None:
        raise _BadMessage("malformed request target")
    return _Request(head, method, parts[2], parsed[0], parsed[1], authority, path)


def _split_host_port(value: str, default_port: int) -> tuple[str, int] | None:
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        host = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            raw_port = rest[1:]
        elif rest == "":
            raw_port = str(default_port)
        else:
            return None
    elif ":" in value:
        host, raw_port = value.rsplit(":", 1)
    else:
        host, raw_port = value, str(default_port)
    if not host or not _DIGITS.fullmatch(raw_port):
        return None
    port = int(raw_port)
    if not 1 <= port <= 65535:
        return None
    return host, port


_NO_BODY = ("none", 0)
_CHUNKED = ("chunked", 0)
_UNTIL_CLOSE = ("close", 0)


def _content_length(head: _Head) -> int | None:
    """The one ``Content-Length`` value, None when absent; several distinct values are malformed."""
    values = {item.decode("latin-1") for item in head.values(b"content-length")}
    if not values:
        return None
    if len(values) != 1:
        raise _BadMessage("conflicting Content-Length values")
    value = values.pop()
    if not _DIGITS.fullmatch(value):
        raise _BadMessage("malformed Content-Length")
    return int(value)


def _request_framing(head: _Head) -> tuple[str, int]:
    codings = [item.lower() for item in head.values(b"transfer-encoding")]
    if codings or head.has(b"transfer-encoding"):
        if head.has(b"content-length"):
            raise _BadMessage("both Transfer-Encoding and Content-Length")
        if not codings or codings[-1] != b"chunked" or b"chunked" in codings[:-1]:
            raise _BadMessage("a request body must end in chunked coding")
        return _CHUNKED
    length = _content_length(head)
    if not length:
        return _NO_BODY
    return ("length", length)


def _response_framing(method: str, status: int, head: _Head) -> tuple[str, int]:
    if method == "HEAD" or status in (204, 304):
        return _NO_BODY
    codings = [item.lower() for item in head.values(b"transfer-encoding")]
    if codings or head.has(b"transfer-encoding"):
        if head.has(b"content-length"):
            raise _BadMessage("both Transfer-Encoding and Content-Length")
        if codings and codings[-1] == b"chunked":
            return _CHUNKED
        return _UNTIL_CLOSE
    length = _content_length(head)
    if length is None:
        return _UNTIL_CLOSE
    return ("length", length)


def _status_of(head: _Head) -> int:
    matched = _STATUS.fullmatch(head.start)
    if matched is None:
        raise _BadMessage("malformed status line")
    return int(matched.group(1))


def _copy_exact(source: _Stream, target: _Sink, count: int) -> None:
    while count:
        piece = source.read_some(min(count, _RELAY_BYTES))
        if not piece:
            raise _Truncated("peer closed inside a body")
        target.sendall(piece)
        count -= len(piece)


def _copy_chunked(source: _Stream, target: _Sink) -> None:
    """Pass one chunked body on, framing included, and stop after its last line."""
    while True:
        line = _check_line(source.read_line(_CHUNK_LINE_LIMIT))
        size_token = line.split(b";", 1)[0].strip(b" \t")
        if not _HEX.fullmatch(size_token):
            raise _BadMessage("malformed chunk size")
        size = int(size_token, 16)
        target.sendall(line + b"\r\n")
        if size == 0:
            break
        _copy_exact(source, target, size)
        if source.read_line(0) != b"":
            raise _BadMessage("chunk data does not end in CRLF")
        target.sendall(b"\r\n")
    trailer_bytes = 0
    while True:
        line = source.read_line(_HEADER_LIMIT)
        trailer_bytes += len(line) + 2
        if trailer_bytes > _HEADER_LIMIT:
            raise _BadMessage("trailer is too large")
        if line == b"":
            target.sendall(b"\r\n")
            return
        _parse_field(line)
        target.sendall(line + b"\r\n")


def _copy_until_close(source: _Stream, target: _Sink) -> None:
    while True:
        piece = source.read_some(_RELAY_BYTES)
        if not piece:
            return
        target.sendall(piece)


def _copy_body(source: _Stream, target: _Sink, framing: tuple[str, int]) -> None:
    kind, length = framing
    if kind == "length":
        _copy_exact(source, target, length)
    elif kind == "chunked":
        _copy_chunked(source, target)
    elif kind == "close":
        _copy_until_close(source, target)


def _open_upstream(host: str, port: int) -> socket.socket | None:
    """A connection to the first checked address that answers; None when refused."""
    addresses = dial_addresses(host)
    if addresses is None:
        return None
    failure: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout=_HANDSHAKE_TIMEOUT_SECONDS)
        except OSError as exc:
            failure = exc
    raise failure if failure is not None else OSError("no address to dial")


def _wants_close(request: _Request) -> bool:
    """Whether the client will not send another request on this connection."""
    tokens = _connection_tokens(request.head)
    if b"close" in tokens:
        return True
    return request.version == b"HTTP/1.0" and b"keep-alive" not in tokens


def _upstream_head(request: _Request) -> bytes:
    """The request in origin form, for a single-use origin connection."""
    fields = [
        (name, value)
        for name, value in _without_hop_by_hop(request.head)
        if name.lower() != b"host"
    ]
    fields.insert(0, (b"Host", request.authority))
    fields.append((b"Connection", b"close"))
    start = request.method.encode("ascii") + b" " + request.path + b" " + request.version
    return _serialize(start, fields)


def _client_head(head: _Head, *, keep: bool) -> bytes:
    fields = _without_hop_by_hop(head)
    fields.append((b"Connection", b"keep-alive" if keep else b"close"))
    return _serialize(head.start, fields)


def _await_continue(origin: _Stream, client: socket.socket, interim_ok: bool) -> _Head | None:
    """Wait briefly for the origin's answer to ``Expect: 100-continue``.

    Interim responses are passed on. A final response is returned without
    the body having been sent; None means the body should follow.
    """
    while origin.has_data(_CONTINUE_WAIT_SECONDS):
        raw = origin.read_head()
        if raw is None:
            raise _Truncated("origin closed before answering")
        head = _parse_head(raw)
        status = _status_of(head)
        if status == 101 or status >= 200:
            return head
        if interim_ok:
            client.sendall(raw)
        if status == 100:
            return None
    return None


def _relay_response(
    origin: _Stream,
    client: socket.socket,
    request: _Request,
    head: _Head | None,
    *,
    keep: bool,
) -> bool:
    """Pass the origin's response on; return whether the client connection stays usable.

    Nothing has reached the client when this raises. Once the final head is
    sent, a failure closes the client connection instead.
    """
    interim_ok = request.version == b"HTTP/1.1"
    while True:
        if head is None:
            raw = origin.read_head()
            if raw is None:
                raise _Truncated("origin closed before answering")
            head = _parse_head(raw)
        status = _status_of(head)
        if status == 101:
            raise _BadMessage("the origin switched protocols")
        if status >= 200:
            break
        if interim_ok:
            client.sendall(_serialize(head.start, head.fields))
        head = None
    framing = _response_framing(request.method, status, head)
    keep = keep and framing != _UNTIL_CLOSE
    client.sendall(_client_head(head, keep=keep))
    try:
        _copy_body(origin, client, framing)
    except (OSError, _BadMessage):
        return False
    return keep


def _serve_plain(stream: _Stream, request: _Request, connections: _Connections) -> bool:
    """Forward one plaintext request; return whether another may follow on this connection.

    A malformed request raises :class:`_BadMessage`, a malformed response
    :class:`_BadGateway`.
    """
    client = stream.sock
    framing = _request_framing(request.head)
    keep = not _wants_close(request)
    upstream = _open_upstream(request.host, request.port)
    if upstream is None:
        client.sendall(_REJECTED)
        return False
    with connections.tracked(upstream):
        upstream.settimeout(_IDLE_TIMEOUT_SECONDS)
        client.settimeout(_IDLE_TIMEOUT_SECONDS)
        upstream.sendall(_upstream_head(request))
        origin = _Stream(upstream)
        early: _Head | None = None
        interim_ok = request.version == b"HTTP/1.1"
        if framing != _NO_BODY:
            if b"100-continue" in {item.lower() for item in request.head.values(b"expect")}:
                try:
                    early = _await_continue(origin, client, interim_ok)
                except _BadMessage as exc:
                    raise _BadGateway(str(exc)) from exc
            if early is None:
                upload = _Upload(stream, origin, interim_ok=interim_ok)
                try:
                    _copy_body(upload, upload, framing)
                except _OriginAnswered as answered:
                    early = answered.head
            if early is not None:
                # The body was not read to its end, so this connection cannot carry another request.
                keep = False
        try:
            kept = _relay_response(origin, client, request, early, keep=keep)
        except _BadMessage as exc:
            raise _BadGateway(str(exc)) from exc
        if early is not None and framing != _NO_BODY:
            _linger(client)
        return kept


def _linger(client: socket.socket) -> None:
    """Let the client read a response sent before its request body ended.

    Closing a socket that holds unread request bytes resets the connection,
    and the reset can discard the response still on its way. The write side
    is shut first; what the client still sends is dropped for at most
    ``_LINGER_SECONDS``.
    """
    try:
        client.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + _LINGER_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            client.settimeout(left)
            if not client.recv(_RELAY_BYTES):
                return
    except OSError:
        return


def _handle_client(client: socket.socket, connections: _Connections | None = None) -> None:
    connections = connections if connections is not None else _Connections()
    try:
        with connections.tracked(client):
            _serve_client(client, connections)
    except _Closing:
        return


def _serve_client(client: socket.socket, connections: _Connections) -> None:
    stream = _Stream(client)
    served = False
    try:
        while True:
            client.settimeout(_HANDSHAKE_TIMEOUT_SECONDS)
            try:
                raw = stream.read_head()
            except TimeoutError:
                # A kept connection with no further request is finished.
                # The first header still fails as a bad gateway.
                if served:
                    return
                raise
            if raw is None:
                return
            request = _parse_request(raw)
            if request.method == "CONNECT":
                _serve_tunnel(stream, request, connections)
                return
            if not _serve_plain(stream, request, connections):
                return
            served = True
    except _Closing:
        return
    except _BadMessage:
        _send_quietly(client, _BAD_REQUEST)
    except (OSError, ValueError, UnicodeError):
        _send_quietly(client, _BAD_GATEWAY)


def _send_quietly(client: socket.socket, payload: bytes) -> None:
    try:
        client.sendall(payload)
    except OSError:
        pass


def _serve_tunnel(stream: _Stream, request: _Request, connections: _Connections) -> None:
    remote = _open_upstream(request.host, request.port)
    if remote is None:
        stream.sock.sendall(_REJECTED)
        return
    with connections.tracked(remote):
        stream.sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        # Past the 200 the bytes belong to the tunnel: a failure only ends it.
        try:
            early = bytes(stream.buffer)
            stream.buffer.clear()
            _tunnel(stream.sock, remote, _IDLE_TIMEOUT_SECONDS, early)
        except OSError:
            return


def _tunnel(
    left: socket.socket, right: socket.socket, idle_seconds: float, early: bytes = b""
) -> None:
    """Relay both ways until each side has closed or nothing moves for ``idle_seconds``.

    The two directions move independently: bytes a peer is not reading yet
    wait in a buffer of at most ``_RELAY_BYTES`` while the other direction
    keeps flowing, and their source is not read until they are sent. A side
    that closes its write half passes that on once its bytes are delivered.
    ``early`` is what ``left`` already sent, still owed to ``right``.
    """
    peer = {left: right, right: left}
    # Bytes received from the other side, waiting to be sent to the key.
    pending = {left: bytearray(), right: bytearray(early)}
    reading = {left, right}
    left.setblocking(False)
    right.setblocking(False)
    with selectors.DefaultSelector() as selector:
        interest = {left: 0, right: 0}
        while reading or pending[left] or pending[right]:
            for sock in (left, right):
                wanted = selectors.EVENT_WRITE if pending[sock] else 0
                if sock in reading and not pending[peer[sock]]:
                    wanted |= selectors.EVENT_READ
                if wanted != interest[sock]:
                    if not interest[sock]:
                        selector.register(sock, wanted, sock)
                    elif not wanted:
                        selector.unregister(sock)
                    else:
                        selector.modify(sock, wanted, sock)
                    interest[sock] = wanted
            events = selector.select(idle_seconds)
            if not events:
                return
            for key, mask in events:
                ready: socket.socket = key.data
                if mask & selectors.EVENT_WRITE:
                    try:
                        sent = ready.send(pending[ready])
                    except BlockingIOError:
                        sent = 0
                    del pending[ready][:sent]
                    if not pending[ready] and peer[ready] not in reading:
                        _shut_write(ready)
                if mask & selectors.EVENT_READ:
                    try:
                        chunk = ready.recv(_RELAY_BYTES)
                    except BlockingIOError:
                        continue
                    if chunk:
                        pending[peer[ready]].extend(chunk)
                        continue
                    reading.discard(ready)
                    if not pending[peer[ready]]:
                        _shut_write(peer[ready])


def _shut_write(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
