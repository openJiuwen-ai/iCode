# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP prompts whose images are prepared before the turn starts."""

from __future__ import annotations

import asyncio
import base64
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest
from acp import RequestError
from acp.helpers import image_block, text_block

from chrys.app.acp import server as server_module
from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import TurnStarted
from chrys.foundation.text.images import compress_image_data
from chrys.orchestration.session_host import Cancelled, EndTurn
from tests.app.acp._server_fakes import _FakeClient, _FakeHost, _FakeManager
from tests.support.images import image_bytes, noisy_image_bytes
from tests.support.waiting import wait_for

_CLIENT_MESSAGE_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_IMAGE = base64.b64encode(image_bytes("PNG")).decode("ascii")


@dataclass
class _CancellableHost(_FakeHost):
    """A host that, like the real one, drops a cancel sent before it admits the turn."""

    log: list[str] = field(default_factory=list)
    _cancel_requested: bool = False

    async def iter_turn_events(self, _message: Any):
        self.log.append("admitted")
        self._cancel_requested = False
        yield TurnStarted(turn_id="1" * 32, turn_number=1, session_id="s1")
        self.last_turn_outcome = Cancelled() if self._cancel_requested else EndTurn()

    async def cancel_current_turn(self) -> None:
        self.log.append("cancel")
        self._cancel_requested = True


class _CancellingManager(_FakeManager):
    """Cancels through the host as the real manager does."""

    async def cancel(self, session_id: str) -> None:
        await super().cancel(session_id)
        await self._session.host.cancel_current_turn()


@dataclass
class _HeldPreparation:
    """Holds ``convert_prompt_blocks`` in its worker thread until released."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        convert = server_module.convert_prompt_blocks

        def held(blocks: Any, *, vision_enabled: bool, max_image_bytes: int) -> Any:
            self.started.set()
            # Bounded: on the event loop, a regression would block the release forever.
            self.release.wait(timeout=10)
            return convert(blocks, vision_enabled=vision_enabled, max_image_bytes=max_image_bytes)

        monkeypatch.setattr(server_module, "convert_prompt_blocks", held)


def _server(manager: _FakeManager) -> ChrysAcpServer:
    server = ChrysAcpServer(manager, initial_vision=True)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())
    return server


@pytest.mark.anyio
async def test_prompt_prepares_images_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_thread = threading.get_ident()
    threads: list[int] = []
    convert = server_module.convert_prompt_blocks

    def recording(blocks: Any, *, vision_enabled: bool, max_image_bytes: int) -> Any:
        threads.append(threading.get_ident())
        return convert(blocks, vision_enabled=vision_enabled, max_image_bytes=max_image_bytes)

    monkeypatch.setattr(server_module, "convert_prompt_blocks", recording)
    host = _CancellableHost(event_bus=EventBus(), vision_enabled=True)
    server = _server(_CancellingManager(host))

    response = await server.prompt(
        [text_block("look"), image_block(_IMAGE, "image/png")], session_id="s1", message_id=_CLIENT_MESSAGE_ID
    )

    assert response.stop_reason == "end_turn"
    assert len(threads) == 1
    assert threads[0] != loop_thread


@pytest.mark.anyio
async def test_cancel_sent_while_images_are_prepared_starts_no_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    preparation = _HeldPreparation()
    preparation.install(monkeypatch)
    host = _CancellableHost(event_bus=EventBus(), vision_enabled=True)
    server = _server(_CancellingManager(host))

    prompt = asyncio.create_task(
        server.prompt([image_block(_IMAGE, "image/png")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)
    )
    try:
        await wait_for(preparation.started.is_set, description="image preparation started")
        assert not prompt.done()
        await server.cancel("s1")
    finally:
        preparation.release.set()
    response = await prompt

    assert response.stop_reason == "cancelled"
    # Not even to wait in admission, as for a prompt hook that asks first.
    assert host.log == ["cancel"]
    assert response.field_meta is not None
    assert "turnId" not in response.field_meta["chrys"]


@pytest.mark.anyio
async def test_prompt_with_images_and_no_cancel_runs_its_turn_uncancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    preparation = _HeldPreparation()
    preparation.install(monkeypatch)
    preparation.release.set()
    host = _CancellableHost(event_bus=EventBus(), vision_enabled=True)
    server = _server(_CancellingManager(host))

    response = await server.prompt([image_block(_IMAGE, "image/png")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    assert response.stop_reason == "end_turn"
    assert host.log == ["admitted"]


@pytest.mark.anyio
async def test_session_closed_while_images_are_prepared_starts_no_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    preparation = _HeldPreparation()
    preparation.install(monkeypatch)
    host = _CancellableHost(event_bus=EventBus(), vision_enabled=True)
    manager = _CancellingManager(host)
    server = _server(manager)

    prompt = asyncio.create_task(
        server.prompt([image_block(_IMAGE, "image/png")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)
    )
    try:
        await wait_for(preparation.started.is_set, description="image preparation started")
        await manager.begin_close("s1")
    finally:
        preparation.release.set()

    with pytest.raises(RequestError):
        await prompt
    assert "admitted" not in host.log


@pytest.mark.anyio
async def test_prompt_images_are_compressed_to_the_session_image_limit() -> None:
    limit = 64 * 1024
    source = noisy_image_bytes("PNG")
    assert len(compress_image_data(source)) > limit
    received: list[Any] = []

    @dataclass
    class _ReceivingHost(_FakeHost):
        async def iter_turn_events(self, message: Any):
            received.append(message)
            yield TurnStarted(turn_id="1" * 32, turn_number=1, session_id="s1")
            self.last_turn_outcome = EndTurn()

    host = _ReceivingHost(event_bus=EventBus(), vision_enabled=True, image_send_limit_bytes=limit)
    server = _server(_CancellingManager(host))

    response = await server.prompt(
        [image_block(base64.b64encode(source).decode("ascii"), "image/png")],
        session_id="s1",
        message_id=_CLIENT_MESSAGE_ID,
    )

    assert response.stop_reason == "end_turn"
    (message,) = received
    image = message.prepared_contents[1]
    assert image.media_type == "image/jpeg"
    assert len(base64.b64decode(image.uri.split(",", 1)[1])) <= limit
