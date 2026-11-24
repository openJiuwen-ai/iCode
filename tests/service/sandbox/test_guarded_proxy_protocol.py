# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The guarded proxy streams HTTP/1.1 correctly and reclaims its connections.

Each origin is a local listener; the proxy's TCP dial is rewritten to it, so
the suite never leaves the machine. Covered: chunked and ``100-continue``
uploads, interim responses, close-delimited and truncated responses,
request framing that could smuggle a second request, origin-form targets
and hop-by-hop fields, trying each resolved address, the IPv6 transition
ranges, streaming without holding a whole body, the idle limit on tunnels,
upstream waits and the in-sandbox bridge, and :meth:`GuardedProxy.close`
ending open connections, also from the runtime's stop and restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import jiuwenbox.supervisor.guarded_proxy as guarded_proxy
import jiuwenbox.supervisor.sandbox_daemon as sandbox_daemon
import pytest

from tests.support.waiting import wait_for

_PUBLIC_HOST = "8.8.8.8"
_READ_SECONDS = 15.0
_IDLE_SECONDS = 0.5
_REAL_CREATE_CONNECTION = socket.create_connection

needs_unix_sockets = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="the proxy listens on a filesystem socket",
)


class _Origin:
    """A local origin that serves each accepted connection with ``serve``."""

    def __init__(self, serve: Callable[[socket.socket], None], *, connections: int = 1) -> None:
        self._serve = serve
        self._connections = connections
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = int(self.listener.getsockname()[1])
        self.release = threading.Event()
        self.thread = threading.Thread(target=self._run, name="guarded-proxy-test-origin", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        for _ in range(self._connections):
            try:
                conn, _address = self.listener.accept()
            except OSError:
                return
            with conn, contextlib.suppress(OSError):
                conn.settimeout(_READ_SECONDS)
                self._serve(conn)

    def close(self) -> None:
        self.release.set()
        self.listener.close()
        self.thread.join(5)


def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    """One head and whatever followed it in the same reads."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError(f"closed inside a head: {bytes(data)!r}")
        data.extend(chunk)
    head, _separator, rest = bytes(data).partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _read_exact(sock: socket.socket, rest: bytes, count: int) -> bytes:
    data = bytearray(rest)
    while len(data) < count:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _read_chunked_request(sock: socket.socket, rest: bytes) -> bytes:
    data = bytearray(rest)
    while not data.endswith(b"0\r\n\r\n"):
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed inside a chunked body")
        data.extend(chunk)
    return bytes(data)


def _read_until_closed(sock: socket.socket, timeout: float = _READ_SECONDS) -> tuple[bytes, bool]:
    """Everything until the peer closes, and whether it closed before ``timeout``."""
    sock.settimeout(timeout)
    data = bytearray()
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return bytes(data), True
            data.extend(chunk)
    except TimeoutError:
        return bytes(data), False
    except ConnectionResetError:
        return bytes(data), True


class _Proxy:
    """One proxy connection served by ``_handle_client`` on a thread."""

    def __init__(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.client.connect(listener.getsockname())
            accepted, _address = listener.accept()
        finally:
            listener.close()
        self.client.settimeout(_READ_SECONDS)
        self.worker = threading.Thread(
            target=guarded_proxy._handle_client,
            args=(accepted,),
            name="guarded-proxy-protocol-test",
            daemon=True,
        )
        self.worker.start()

    def close(self) -> None:
        self.client.close()
        self.worker.join(5)


@pytest.fixture
def proxy() -> Iterator[_Proxy]:
    served = _Proxy()
    yield served
    served.close()


@pytest.fixture
def origins() -> Iterator[list[_Origin]]:
    started: list[_Origin] = []
    yield started
    for origin in started:
        origin.close()


def _start(origins: list[_Origin], serve: Callable[[socket.socket], None], **kwargs: int) -> _Origin:
    origin = _Origin(serve, **kwargs)
    origins.append(origin)
    return origin


def _dial_to(monkeypatch: pytest.MonkeyPatch, port: int) -> list[tuple[str, int]]:
    dialed: list[tuple[str, int]] = []

    def dial(address: tuple[str, int], *args: object, **kwargs: object) -> socket.socket:
        dialed.append(address)
        return _REAL_CREATE_CONNECTION(("127.0.0.1", port), *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(guarded_proxy.socket, "create_connection", dial)
    return dialed


def _respond(body: bytes, *, extra: bytes = b"") -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode("ascii") + b"\r\n" + extra + b"\r\n" + body


def _final_response(sock: socket.socket) -> tuple[bytes, bytes]:
    head, rest = _read_head(sock)
    length = 0
    for line in head.split(b"\r\n"):
        name, _separator, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    return head, _read_exact(sock, rest, length)


def test_chunked_upload_ends_at_its_last_chunk(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    seen: list[bytes] = []

    def serve(conn: socket.socket) -> None:
        head, rest = _read_head(conn)
        seen.append(head + _read_chunked_request(conn, rest))
        conn.sendall(_respond(b"ok"))

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    proxy.client.sendall(
        f"POST http://{_PUBLIC_HOST}:{origin.port}/up HTTP/1.1\r\n".encode("ascii")
        + b"Host: ignored\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n"
    )

    head, body = _final_response(proxy.client)

    assert head.startswith(b"HTTP/1.1 200 "), head
    assert body == b"ok"
    assert seen and seen[0].endswith(b"\r\n\r\n5\r\nhello\r\n0\r\n\r\n")


def test_origin_receives_origin_form_without_hop_by_hop_fields(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    seen: list[bytes] = []

    def serve(conn: socket.socket) -> None:
        head, _rest = _read_head(conn)
        seen.append(head)
        # The origin's own ``Connection: close`` concerns only its connection.
        conn.sendall(_respond(b"ok", extra=b"Connection: close\r\n"))

    origin = _start(origins, serve, connections=2)
    _dial_to(monkeypatch, origin.port)
    authority = f"{_PUBLIC_HOST}:{origin.port}".encode("ascii")
    request = (
        b"GET http://" + authority + b"/path?q=1#fragment HTTP/1.1\r\n"
        b"Host: elsewhere.example\r\n"
        b"Proxy-Authorization: Basic c2VjcmV0\r\n"
        b"Connection: X-Secret\r\n"
        b"X-Secret: hidden\r\n"
        b"X-Kept: yes\r\n\r\n"
    )

    proxy.client.sendall(request)
    first_head, first_body = _final_response(proxy.client)
    proxy.client.sendall(request)
    _second_head, second_body = _final_response(proxy.client)

    assert first_body == second_body == b"ok"
    assert b"connection: close" not in first_head.lower()
    upstream = seen[0].split(b"\r\n")
    assert upstream[0] == b"GET /path?q=1 HTTP/1.1"
    fields = [line.lower() for line in upstream[1:] if line]
    assert b"host: " + authority.lower() in fields
    assert b"x-kept: yes" in fields
    assert b"connection: close" in fields
    assert not any(line.startswith((b"proxy-authorization", b"x-secret")) for line in fields)
    assert len(seen) == 2


def test_expect_continue_upload_waits_for_the_origin(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    def serve(conn: socket.socket) -> None:
        _head, rest = _read_head(conn)
        conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        if _read_exact(conn, rest, 5) == b"hello":
            conn.sendall(_respond(b"stored"))

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    proxy.client.sendall(
        f"PUT http://{_PUBLIC_HOST}:{origin.port}/f HTTP/1.1\r\n".encode("ascii")
        + b"Host: x\r\nContent-Length: 5\r\nExpect: 100-continue\r\n\r\n"
    )

    interim, rest = _read_head(proxy.client)
    assert interim.startswith(b"HTTP/1.1 100 "), interim
    assert rest == b""
    proxy.client.sendall(b"hello")
    head, body = _final_response(proxy.client)

    assert head.startswith(b"HTTP/1.1 200 "), head
    assert body == b"stored"


def test_interim_response_is_followed_by_the_final_one(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    def serve(conn: socket.socket) -> None:
        _read_head(conn)
        conn.sendall(b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n" + _respond(b"final"))

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    proxy.client.sendall(f"GET http://{_PUBLIC_HOST}:{origin.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))

    data = bytearray()
    while not data.endswith(b"final"):
        chunk = proxy.client.recv(65536)
        if not chunk:
            break
        data.extend(chunk)
    interim, _separator, final = bytes(data).partition(b"\r\n\r\n")

    assert interim.startswith(b"HTTP/1.1 103 "), data
    assert b"rel=preload" in interim
    assert final.startswith(b"HTTP/1.1 200 "), data
    assert final.endswith(b"\r\n\r\nfinal")


def test_close_delimited_response_ends_when_the_origin_closes(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    def serve(conn: socket.socket) -> None:
        _read_head(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\n\r\nuntil-close")

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    proxy.client.sendall(f"GET http://{_PUBLIC_HOST}:{origin.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))

    started = time.monotonic()
    data, closed = _read_until_closed(proxy.client)
    elapsed = time.monotonic() - started

    assert closed, data
    assert data.endswith(b"\r\n\r\nuntil-close")
    assert b"connection: close" in data.lower()
    # Well under the ten-second handshake timeout a close-delimited body once waited for.
    assert elapsed < guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS / 2


def test_truncated_response_closes_the_client_connection(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    def serve(conn: socket.socket) -> None:
        _read_head(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nonly-ten-b")

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    proxy.client.sendall(f"GET http://{_PUBLIC_HOST}:{origin.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))

    data, closed = _read_until_closed(proxy.client, timeout=guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS / 2)

    assert closed, data
    assert data.endswith(b"\r\n\r\nonly-ten-b")


@pytest.mark.parametrize(
    "fields",
    [
        b"Content-Length: 5\r\nTransfer-Encoding: chunked\r\n",
        b"Content-Length: 5\r\nContent-Length: 6\r\n",
        b"Content-Length: 5, 6\r\n",
        b"Content-Length: +5\r\n",
        b"Transfer-Encoding: chunked\r\nTransfer-Encoding: identity\r\n",
        b"Transfer-Encoding: chunked, chunked\r\n",
        b"Transfer-Encoding: gzip\r\n",
        b"Transfer-Encoding:\r\n chunked\r\n",
        b"Transfer-Encoding : chunked\r\n",
        b"X-Bare\nTransfer-Encoding: chunked\r\n",
    ],
    ids=[
        "length-and-chunked",
        "two-lengths",
        "listed-lengths",
        "signed-length",
        "chunked-not-last-across-lines",
        "chunked-twice",
        "not-chunked",
        "folded",
        "space-before-colon",
        "bare-line-feed",
    ],
)
def test_ambiguous_request_framing_is_refused(
    fields: bytes,
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
) -> None:
    dialed = _dial_to(monkeypatch, 9)
    proxy.client.sendall(
        b"POST http://" + _PUBLIC_HOST.encode("ascii") + b"/ HTTP/1.1\r\nHost: x\r\n" + fields + b"\r\nhello"
    )

    data, closed = _read_until_closed(proxy.client)

    assert data.startswith(b"HTTP/1.1 400 "), data
    assert closed
    assert dialed == []


def test_origin_form_request_is_refused(monkeypatch: pytest.MonkeyPatch, proxy: _Proxy) -> None:
    dialed = _dial_to(monkeypatch, 9)
    proxy.client.sendall(b"GET /local HTTP/1.1\r\nHost: 8.8.8.8\r\n\r\n")

    data, closed = _read_until_closed(proxy.client)

    assert data.startswith(b"HTTP/1.1 400 "), data
    assert closed
    assert dialed == []


def test_each_resolved_address_is_tried_in_turn(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    def serve(conn: socket.socket) -> None:
        _read_head(conn)
        conn.sendall(_respond(b"second"))

    origin = _start(origins, serve)
    dialed: list[tuple[str, int]] = []

    def dial(address: tuple[str, int], *args: object, **kwargs: object) -> socket.socket:
        dialed.append(address)
        if address[0] == "8.8.8.8":
            raise ConnectionRefusedError("first address refuses")
        return _REAL_CREATE_CONNECTION(("127.0.0.1", origin.port), *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(guarded_proxy.socket, "create_connection", dial)
    monkeypatch.setattr(guarded_proxy, "_resolve", lambda host: ["8.8.8.8", "1.1.1.1"])
    proxy.client.sendall(f"GET http://pkg.example:{origin.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))

    head, body = _final_response(proxy.client)

    assert head.startswith(b"HTTP/1.1 200 "), head
    assert body == b"second"
    assert dialed == [("8.8.8.8", origin.port), ("1.1.1.1", origin.port)]


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::a00:1",
        "2002:a9fe:a9fe::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "::a9fe:a9fe",
        "192.88.99.1",
    ],
    ids=["nat64", "nat64-local", "6to4", "teredo", "ipv4-compatible", "6to4-relay"],
)
def test_ipv6_transition_addresses_are_blocked(address: str) -> None:
    assert guarded_proxy.address_is_blocked(address) is True
    assert guarded_proxy.host_is_blocked(address) is True


def test_mixed_dns_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guarded_proxy, "_resolve", lambda host: ["8.8.8.8", "64:ff9b::a9fe:a9fe"])

    assert guarded_proxy.dial_addresses("pkg.example") is None


def test_body_is_streamed_before_the_origin_finishes(
    monkeypatch: pytest.MonkeyPatch,
    proxy: _Proxy,
    origins: list[_Origin],
) -> None:
    first = b"a" * guarded_proxy._RELAY_BYTES
    rest = b"b" * guarded_proxy._RELAY_BYTES

    def serve(conn: socket.socket) -> None:
        _read_head(conn)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(first) + len(rest)).encode("ascii") + b"\r\n\r\n")
        conn.sendall(first)
        if origin.release.wait(_READ_SECONDS):
            conn.sendall(rest)

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    proxy.client.sendall(f"GET http://{_PUBLIC_HOST}:{origin.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))

    _head, early = _read_head(proxy.client)
    early = _read_exact(proxy.client, early, len(first))
    origin.release.set()
    body = _read_exact(proxy.client, early, len(first) + len(rest))

    assert early == first
    assert body == first + rest


async def test_idle_tunnel_is_reclaimed(monkeypatch: pytest.MonkeyPatch, origins: list[_Origin]) -> None:
    monkeypatch.setattr(guarded_proxy, "_IDLE_TIMEOUT_SECONDS", _IDLE_SECONDS)

    def serve(conn: socket.socket) -> None:
        origin.release.wait(_READ_SECONDS)

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    served = _Proxy()
    try:
        served.client.sendall(f"CONNECT {_PUBLIC_HOST}:{origin.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))
        head, _rest = await asyncio.to_thread(_read_head, served.client)
        assert head.startswith(b"HTTP/1.1 200 "), head
        await wait_for(lambda: not served.worker.is_alive(), description="idle tunnel ended")
        _data, closed = await asyncio.to_thread(_read_until_closed, served.client)
    finally:
        served.close()

    assert closed


async def test_moving_tunnel_outlives_the_idle_limit(monkeypatch: pytest.MonkeyPatch, origins: list[_Origin]) -> None:
    monkeypatch.setattr(guarded_proxy, "_IDLE_TIMEOUT_SECONDS", _IDLE_SECONDS)

    def serve(conn: socket.socket) -> None:
        while chunk := conn.recv(65536):
            conn.sendall(chunk)

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    served = _Proxy()
    pieces = [f"piece-{index};".encode("ascii") for index in range(6)]

    def exchange() -> bytes:
        served.client.sendall(f"CONNECT {_PUBLIC_HOST}:{origin.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))
        _head, rest = _read_head(served.client)
        echoed = bytearray(rest)
        for piece in pieces:
            # Paces the traffic so the whole exchange outlasts the idle limit.
            time.sleep(_IDLE_SECONDS / 2)
            served.client.sendall(piece)
            echoed.extend(_read_exact(served.client, b"", len(piece)))
        return bytes(echoed)

    try:
        started = time.monotonic()
        echoed = await asyncio.to_thread(exchange)
        elapsed = time.monotonic() - started
    finally:
        served.close()

    assert echoed == b"".join(pieces)
    assert elapsed > _IDLE_SECONDS * 2


def test_silent_origin_is_reclaimed(monkeypatch: pytest.MonkeyPatch, origins: list[_Origin], proxy: _Proxy) -> None:
    monkeypatch.setattr(guarded_proxy, "_IDLE_TIMEOUT_SECONDS", _IDLE_SECONDS)

    def serve(conn: socket.socket) -> None:
        _read_head(conn)
        origin.release.wait(_READ_SECONDS)

    origin = _start(origins, serve)
    _dial_to(monkeypatch, origin.port)
    proxy.client.sendall(f"GET http://{_PUBLIC_HOST}:{origin.port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))

    data, closed = _read_until_closed(proxy.client, timeout=guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS / 2)

    assert data.startswith(b"HTTP/1.1 502 "), data
    assert closed


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # Unix socket paths are short; pytest's tmp_path can exceed the limit on macOS.
    directory = Path(tempfile.mkdtemp(prefix="gpp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _open_tunnel(path: Path, port: int) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(_READ_SECONDS)
    client.connect(str(path))
    client.sendall(f"CONNECT {_PUBLIC_HOST}:{port} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))
    head, _rest = _read_head(client)
    if not head.startswith(b"HTTP/1.1 200 "):
        client.close()
        raise AssertionError(head)
    return client


def _held_origin(origins: list[_Origin], *, connections: int = 1) -> _Origin:
    def serve(conn: socket.socket) -> None:
        origin.release.wait(_READ_SECONDS)

    origin = _start(origins, serve, connections=connections)
    return origin


@needs_unix_sockets
async def test_close_ends_open_connections(
    monkeypatch: pytest.MonkeyPatch,
    origins: list[_Origin],
    short_dir: Path,
) -> None:
    origin = _held_origin(origins)
    _dial_to(monkeypatch, origin.port)
    proxy = guarded_proxy.GuardedProxy()
    path = short_dir / "p.sock"
    proxy.serve(path)
    client: socket.socket | None = None
    try:
        client = await asyncio.to_thread(_open_tunnel, path, origin.port)
        proxy.close()
        _data, closed = await asyncio.to_thread(
            _read_until_closed, client, guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS / 2
        )
    finally:
        proxy.close()
        if client is not None:
            client.close()

    assert closed
    assert not path.exists()


@needs_unix_sockets
@pytest.mark.skipif(not hasattr(os, "getuid"), reason="the sandbox runtime is POSIX-only")
async def test_runtime_stop_and_restart_close_the_previous_proxy(
    monkeypatch: pytest.MonkeyPatch,
    origins: list[_Origin],
    short_dir: Path,
) -> None:
    from jiuwenbox.server.runtime.process import ProcessRuntime

    origin = _held_origin(origins, connections=2)
    _dial_to(monkeypatch, origin.port)
    runtime = ProcessRuntime()
    runtime._control_dirs["sb-proxy"] = short_dir
    path = short_dir / "guarded-proxy.sock"
    clients: list[socket.socket] = []
    try:
        runtime._start_guarded_proxy("sb-proxy")
        clients.append(await asyncio.to_thread(_open_tunnel, path, origin.port))
        runtime._start_guarded_proxy("sb-proxy")
        _data, replaced_closed = await asyncio.to_thread(
            _read_until_closed, clients[0], guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS / 2
        )
        clients.append(await asyncio.to_thread(_open_tunnel, path, origin.port))
        await runtime.stop("sb-proxy")
        _data, stopped_closed = await asyncio.to_thread(
            _read_until_closed, clients[1], guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS / 2
        )
    finally:
        runtime._close_guarded_proxy("sb-proxy")
        for client in clients:
            client.close()

    assert replaced_closed
    assert stopped_closed
    assert "sb-proxy" not in runtime._guarded_proxies


@needs_unix_sockets
async def test_sandbox_bridge_reclaims_an_idle_connection(
    monkeypatch: pytest.MonkeyPatch,
    short_dir: Path,
) -> None:
    release = threading.Event()
    path = short_dir / "b.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def hold() -> None:
        try:
            conn, _address = listener.accept()
        except OSError:
            return
        with conn:
            release.wait(_READ_SECONDS)

    holder = threading.Thread(target=hold, name="guarded-bridge-test-proxy", daemon=True)
    holder.start()
    monkeypatch.setattr(sandbox_daemon, "_GUARDED_PROXY_SOCKET", str(path))
    monkeypatch.setattr(sandbox_daemon, "_GUARDED_BRIDGE_IDLE_SECONDS", _IDLE_SECONDS)
    pair_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    pair_listener.bind(("127.0.0.1", 0))
    pair_listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(pair_listener.getsockname())
        accepted, _address = pair_listener.accept()
        pair_listener.close()
        worker = threading.Thread(
            target=sandbox_daemon._bridge_guarded_client,
            args=(accepted,),
            name="guarded-bridge-idle-test",
            daemon=True,
        )
        worker.start()
        await wait_for(lambda: not worker.is_alive(), description="idle bridge ended")
        _data, closed = await asyncio.to_thread(
            _read_until_closed, client, guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS / 2
        )
    finally:
        release.set()
        client.close()
        pair_listener.close()
        listener.close()
        holder.join(5)

    assert closed
