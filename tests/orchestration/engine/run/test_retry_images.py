# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image retention when a live run hands off to a queued retry."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest
from PIL import Image

from chrys.foundation.events.types import Error, SessionRestore, UserInterrupt, UserMessage, UserRetry, Warning
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine.run.test_retry_integration import _RESTORE_EVENT_TYPES, started_retry_engine
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.kernel import AgentResponse


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize("interrupt", [False, True], ids=["completed", "interrupted"])
@pytest.mark.parametrize("note", ["", "Read the document"], ids=["plain", "with-note"])
async def test_queued_retry_preserves_original_image(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, stream: bool, interrupt: bool, note: str
) -> None:
    settings, registry = make_mock_settings_and_registry(stream=stream)
    model = registry.get(settings.model_profile)
    assert model is not None
    model.vision = True
    store = JsonFileStateStore(tmp_path)
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        settings=settings,
        model_registry=registry,
        state_store=store,
        client_factory=lambda: MockChatClient(
            responses=[MockResponse(text="Original response"), MockResponse(text="Retry response")]
        ),
        event_types=(*_RESTORE_EVENT_TYPES, Warning),
    )
    engine, bus = started.engine, started.bus
    response_landed = asyncio.Event()
    release_response = asyncio.Event()
    succeeded = TurnBindings.succeeded

    async def pause_first_response(self: TurnBindings, result: AgentResponse) -> None:
        if not response_landed.is_set():
            response_landed.set()
            await release_response.wait()
        await succeeded(self, result)

    monkeypatch.setattr(TurnBindings, "succeeded", create_autospec(succeeded, side_effect=pause_first_response))
    image_path = tmp_path / "document.png"
    Image.new("RGB", (1, 1), color="white").save(image_path)
    opener = UserMessage(text=f"Describe @{image_path}")
    try:
        async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
            await bus.publish(opener, raise_handler_errors=True)
            await response_landed.wait()
            assert engine.current.loaded.bindings.state.running
            assert engine.history_messages[-1].text == "Original response"
            if interrupt:
                await bus.publish(UserInterrupt(), raise_handler_errors=True)
            await bus.publish(UserRetry(text=note), raise_handler_errors=True)
            assert engine.state == EngineState.PENDING_RETRY
            assert engine.turns.turn_state.lease.pending_retry.owner_admission_id is not None
            # Replay must use accepted bytes, even if the original file is gone.
            image_path.unlink()
            release_response.set()
            await engine.wait_for_run_task()
    finally:
        release_response.set()
        async with asyncio.timeout(DEFAULT_WAIT_TIMEOUT):
            await engine.wait_for_run_task()

    assert not [event for event in started.events if isinstance(event, Error | Warning)]
    client = started.clients[0]
    assert client.call_count == 2
    original_images = [
        content.uri
        for message in client.call_history[0][0]
        for content in message.contents
        if content.media_type == "image/png"
    ]
    assert len(original_images) == 1
    assert [
        content.uri
        for message in client.call_history[1][0]
        for content in message.contents
        if content.media_type == "image/png"
    ] == original_images
    assert engine.state == EngineState.IDLE
    assert engine.session.turn_number == 1
    users = [message for message in engine.history_messages if message.role == "user"]
    assert [message.text for message in users] == [opener.text, *([note] if note else [])]
    assert not users[0].additional_properties.get(HistoryMarkerKind.INJECTED_KEY)
    assert [content.uri for content in users[0].contents if content.media_type == "image/png"] == original_images
    if note:
        assert users[1].additional_properties[HistoryMarkerKind.INJECTED_KEY]
    answers = [
        message.text
        for message in engine.history_messages
        if message.role == "assistant" and HistoryMarkerKind.KEY not in message.additional_properties
    ]
    # A Continue, with a note or without, drops the answer the first pass showed.
    assert answers == ["Retry response"]
    session_id = engine.session_id
    assert session_id is not None
    saved = await store.load_session_raw(session_id)
    assert saved is not None
    assert [
        content["uri"]
        for message in saved
        for content in message["contents"]
        if content.get("media_type") == "image/png"
    ] == original_images
    await bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
    assert [
        content.uri
        for message in engine.history_messages
        for content in message.contents
        if content.media_type == "image/png"
    ] == original_images
