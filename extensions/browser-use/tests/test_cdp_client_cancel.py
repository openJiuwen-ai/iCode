# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cancellation hygiene for the CDP client (W08 stresses it with wait_for)."""

from __future__ import annotations

import asyncio

import pytest

from icode_browser_use.cdp.client import CDPClient


class CancellingSocket:
    """A socket whose send is cancelled before the frame is written."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        raise asyncio.CancelledError

    async def close(self) -> None:
        return None

    async def recv(self) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_cancelled_send_does_not_leave_a_pending_entry() -> None:
    client = CDPClient(CancellingSocket())

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await client.send("Runtime.evaluate", {"expression": "1"})
        assert client._pending == {}

    asyncio.run(scenario())


def test_cdp_client_shutdown_is_cancel_safe_and_shared() -> None:
    """Wave 3 P1-16a: cancelling the first aclose() must not leak _ws or leave shutdown incomplete."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowCloseSocket(CancellingSocket):
        def __init__(self) -> None:
            super().__init__()
            self.pong_event = asyncio.Event()
            self.aclose_started = 0
            self.aclose_finished = 0

        async def aclose(self) -> None:
            self.aclose_started += 1
            entered.set()
            await release.wait()
            self.aclose_finished += 1

    async def scenario() -> None:
        sock = SlowCloseSocket()
        client = CDPClient(sock)  # type: ignore[arg-type]
        client._start()

        first = asyncio.create_task(client.aclose())
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        # A second caller joins the same shielded shutdown task rather than returning early.
        second = asyncio.create_task(client.aclose())
        await asyncio.sleep(0.02)
        assert not second.done()
        release.set()
        await asyncio.wait_for(second, timeout=2.0)
        assert sock.aclose_started == 1
        assert sock.aclose_finished == 1
        assert client.closed is True

    asyncio.run(scenario())


def test_ws_frame_too_large_extracts_id_for_flat_session_and_fragments() -> None:
    """Wave 3 P2-13: flat-session sessionId-first & fragmented oversized frames recover request_id."""
    from icode_browser_use.cdp import websocket as ws_mod
    from icode_browser_use.cdp.websocket import FrameTooLarge, OP_CONTINUATION, OP_TEXT, WSClient, _encode_frame

    class DummyWriter:
        def write(self, _data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def scenario() -> None:
        # 1) Single oversized frame with leading sessionId before id.
        reader1 = asyncio.StreamReader()
        payload1 = b'{"sessionId":"sess-9","id":42,"result":"' + (b"x" * 200) + b'"}'
        reader1.feed_data(_encode_frame(OP_TEXT, payload1, mask=False))
        reader1.feed_eof()
        ws1 = WSClient(reader1, DummyWriter())  # type: ignore[arg-type]
        orig_limit = ws_mod.MAX_MESSAGE_BYTES
        ws_mod.MAX_MESSAGE_BYTES = 128
        try:
            with pytest.raises(FrameTooLarge) as exc1:
                await ws1.recv()
            assert exc1.value.request_id == 42

            # 2) Fragmented frames: first fragment only carries sessionId, second carries id + overflow.
            reader2 = asyncio.StreamReader()
            f1 = _encode_frame(OP_TEXT, b'{"sessionId":"sess-1",', mask=False, fin=False)
            f2 = _encode_frame(OP_CONTINUATION, b'"id":77,"result":"' + (b"y" * 200) + b'"}', mask=False, fin=True)
            reader2.feed_data(f1 + f2)
            reader2.feed_eof()
            ws2 = WSClient(reader2, DummyWriter())  # type: ignore[arg-type]
            with pytest.raises(FrameTooLarge) as exc2:
                await ws2.recv()
            assert exc2.value.request_id == 77
        finally:
            ws_mod.MAX_MESSAGE_BYTES = orig_limit

    asyncio.run(scenario())


def test_ws_rejects_64bit_length_msb_and_aborts_on_limit_overrun(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave 3 P2-13: RFC6455 64-bit MSB-set length is rejected; LimitOverrunError aborts socket."""
    import struct

    from icode_browser_use.cdp import websocket as ws_mod
    from icode_browser_use.cdp.websocket import OP_TEXT, WSClient, _decode_frame
    from icode_browser_use.errors import Unavailable

    bad_header = bytes([0x80 | OP_TEXT, 127]) + struct.pack("!Q", 1 << 63)
    with pytest.raises(Unavailable, match="最高位"):
        _decode_frame(bad_header)

    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(bad_header)
        reader.feed_eof()
        aborted = {"count": 0}

        class FakeWriter:
            def write(self, _data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                aborted["count"] += 1

            async def wait_closed(self) -> None:
                pass

        ws = WSClient(reader, FakeWriter())  # type: ignore[arg-type]
        with pytest.raises(Unavailable, match="最高位"):
            await ws._read_frame()

        # LimitOverrunError during handshake must abort the socket and raise Unavailable.
        async def fake_open_connection(_host: str, _port: int):
            return asyncio.StreamReader(), FakeWriter()

        async def fake_read_http(_reader):
            raise asyncio.LimitOverrunError("header too long", 1024)

        monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
        monkeypatch.setattr(ws_mod, "_read_http_response", fake_read_http)
        aborted["count"] = 0
        with pytest.raises(Unavailable, match="握手失败"):
            await WSClient.connect("ws://127.0.0.1:9222/devtools/browser/test")
        assert aborted["count"] == 1

    asyncio.run(scenario())
