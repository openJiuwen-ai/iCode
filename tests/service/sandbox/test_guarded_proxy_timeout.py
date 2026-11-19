# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The guarded proxy's handshake timeout stops at the relay.

A quiet client still receives a body that takes longer than the header and
upstream handshake deadline. A client that never finishes the header fails
on that same deadline. The origin is a local listener: the suite does not
open a public connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time

import jiuwenbox.supervisor.guarded_proxy as guarded_proxy
import pytest

_PIECE_COUNT = 14
_PIECE_GAP_SECONDS = 1.0
_SLOW_BODY = b"p" * 4096 + b"END-OF-SLOW-BODY"
# Past the handshake timeout and the relay's two-second join.
_RELAY_FLOOR_SECONDS = guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS + 2
_READ_BUDGET_SECONDS = 45.0
_HEADER_GRACE_SECONDS = 5.0
# A public address the allow-list accepts. The TCP dial is rewritten to the
# local origin so the test never leaves the machine.
_PUBLIC_HOST = "8.8.8.8"


def _nodelay(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


def _connected_pair() -> tuple[socket.socket, socket.socket]:
    """Return ``(client, accepted)`` for a loopback TCP connection."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(listener.getsockname())
        accepted, _address = listener.accept()
    except OSError:
        client.close()
        raise
    finally:
        listener.close()
    _nodelay(client)
    _nodelay(accepted)
    return client, accepted


def _split_body(body: bytes, count: int) -> list[bytes]:
    size = len(body) // count
    parts: list[bytes] = []
    start = 0
    for index in range(count):
        end = len(body) if index == count - 1 else start + size
        parts.append(body[start:end])
        start = end
    return parts


def _read_response(client: socket.socket, body_length: int) -> bytes:
    """Read one HTTP response whose body is ``body_length`` bytes."""
    client.settimeout(_READ_BUDGET_SECONDS)
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = client.recv(65536)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    header, _, first = bytes(data).partition(b"\r\n\r\n")
    body = bytearray(first)
    while len(body) < body_length:
        chunk = client.recv(65536)
        if not chunk:
            break
        body.extend(chunk)
    return header + b"\r\n\r\n" + bytes(body)


def _read_until_closed(client: socket.socket) -> bytes:
    client.settimeout(guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS + _HEADER_GRACE_SECONDS + 5)
    data = bytearray()
    try:
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
    except TimeoutError:
        pass
    return bytes(data)


async def test_quiet_client_receives_a_body_past_the_handshake_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that sends nothing further still gets a long response body."""
    parts = _split_body(_SLOW_BODY, _PIECE_COUNT)
    assert b"".join(parts) == _SLOW_BODY

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = writer.get_extra_info("socket")
        if isinstance(raw, socket.socket):
            _nodelay(raw)
        try:
            request = bytearray()
            while b"\r\n\r\n" not in request:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                request.extend(chunk)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(_SLOW_BODY)).encode("ascii") + b"\r\n\r\n")
            await writer.drain()
            for piece in parts:
                await asyncio.sleep(_PIECE_GAP_SECONDS)
                writer.write(piece)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    client: socket.socket | None = None
    worker: threading.Thread | None = None
    try:
        listening = server.sockets
        assert listening
        origin_port = int(listening[0].getsockname()[1])
        create_connection = socket.create_connection
        dialed: list[tuple[object, object]] = []

        def _dial_local(address: object, *args: object, **kwargs: object) -> socket.socket:
            dialed.append((address, kwargs.get("timeout")))
            remote = create_connection(("127.0.0.1", origin_port), *args, **kwargs)
            _nodelay(remote)
            return remote

        monkeypatch.setattr(guarded_proxy.socket, "create_connection", _dial_local)
        client, accepted = _connected_pair()
        client.sendall(
            (
                f"GET http://{_PUBLIC_HOST}:{origin_port}/slow HTTP/1.1\r\nHost: {_PUBLIC_HOST}:{origin_port}\r\n\r\n"
            ).encode("ascii")
        )
        worker = threading.Thread(
            target=guarded_proxy._handle_client,
            args=(accepted,),
            name="guarded-proxy-timeout-test",
            daemon=True,
        )
        started = time.monotonic()
        worker.start()
        response = await asyncio.to_thread(_read_response, client, len(_SLOW_BODY))
        elapsed = time.monotonic() - started
    finally:
        if client is not None:
            client.close()
        server.close()
        await server.wait_closed()
        if worker is not None:
            await asyncio.to_thread(worker.join, 5)

    assert dialed == [((_PUBLIC_HOST, origin_port), guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS)]
    assert response.startswith(b"HTTP/1.1 200 ")
    _header, separator, received = response.partition(b"\r\n\r\n")
    assert separator == b"\r\n\r\n"
    assert len(received) == len(_SLOW_BODY)
    assert received == _SLOW_BODY
    assert elapsed > _RELAY_FLOOR_SECONDS
    assert worker is not None
    assert not worker.is_alive()


async def test_unfinished_header_fails_within_the_handshake_timeout() -> None:
    """A client that never completes the header is cut off by the handshake timeout."""
    client, accepted = _connected_pair()
    worker: threading.Thread | None = None
    try:
        # No blank line, so the header read stays in ``recv`` until it times out.
        client.sendall(b"GET http://8.8.8.8/slow HTTP/1.1\r\nHost: 8.8.8.8\r\n")
        worker = threading.Thread(
            target=guarded_proxy._handle_client,
            args=(accepted,),
            name="guarded-proxy-header-timeout-test",
            daemon=True,
        )
        started = time.monotonic()
        worker.start()
        data = await asyncio.to_thread(_read_until_closed, client)
        elapsed = time.monotonic() - started
    finally:
        client.close()
        if worker is not None:
            await asyncio.to_thread(worker.join, 5)

    limit = guarded_proxy._HANDSHAKE_TIMEOUT_SECONDS
    assert limit - 1 <= elapsed < limit + _HEADER_GRACE_SECONDS, data
    assert data.startswith(b"HTTP/1.1 502 "), (elapsed, data)
    assert worker is not None
    assert not worker.is_alive()
