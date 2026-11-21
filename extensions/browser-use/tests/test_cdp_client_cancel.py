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
