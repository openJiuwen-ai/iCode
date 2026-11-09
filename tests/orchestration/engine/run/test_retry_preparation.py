# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retry admission while an interrupted fresh turn is still preparing."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest
from PIL import Image

from chrys.foundation.events.types import (
    Error,
    SessionRestore,
    SessionRestored,
    UserInterrupt,
    UserMessage,
    UserRetry,
    Warning,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Content
from chrys.orchestration.engine.run.retry import RetryCoordinator
from chrys.orchestration.engine.run.runner import TurnRunner
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine.run.test_retry_integration import _RESTORE_EVENT_TYPES, started_retry_engine
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("additional_text", ["", "https://example.com/document"], ids=["plain", "with-note"])
@pytest.mark.parametrize("with_image", [False, True], ids=["text", "image"])
@pytest.mark.parametrize("pause_point", ["task-not-started", "before-turn", "workspace-preparation"])
async def test_retry_during_first_turn_preparation_preserves_admitted_input(
    tmp_path: Path,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
    additional_text: str,
    with_image: bool,
    pause_point: str,
) -> None:
    """Stop and retry before the first model call must retain the accepted opener."""
    settings, model_registry = make_mock_settings_and_registry(stream=True)
    model = model_registry.get(settings.model_profile)
    assert model is not None
    model.vision = True
    state_store = JsonFileStateStore(tmp_path)
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: MockChatClient(responses=[MockResponse(text="Document reply")]),
        settings=settings,
        model_registry=model_registry,
        state_store=state_store,
        event_types=(*_RESTORE_EVENT_TYPES, Warning),
    )
    pause_before_turn = pause_point != "workspace-preparation"
    engine, bus, events = started.engine, started.bus, started.events
    assert engine.session.workspace is not None
    assert engine.session.workspace.primary_cwd == str(tmp_path)
    compute_notice = create_autospec(
        WorkspaceChangeTracker.compute_turn_notice, side_effect=WorkspaceChangeTracker.compute_turn_notice
    )
    capture_baseline = create_autospec(
        WorkspaceChangeTracker.capture_baseline, side_effect=WorkspaceChangeTracker.capture_baseline
    )
    monkeypatch.setattr(WorkspaceChangeTracker, "compute_turn_notice", compute_notice)
    monkeypatch.setattr(WorkspaceChangeTracker, "capture_baseline", capture_baseline)
    preparation_entered = asyncio.Event()
    release_preparation = asyncio.Event()
    retry_checked = asyncio.Event()
    original_pre_run = TurnRunner.pre_run
    original_notice = TurnRunner._compute_workspace_notice
    original_wait = RetryCoordinator._wait_for_existing_run_task

    async def paused_pre_run(
        self: TurnRunner,
        *,
        reset_batch_id: bool,
        is_retry: bool = False,
        has_opening_input: bool = True,
        user_message_id: str | None = None,
        note_message_id: str | None = None,
        preparation_scope_operation_id: str | None = None,
    ) -> None:
        if pause_before_turn and not is_retry:
            preparation_entered.set()
            await release_preparation.wait()
        await original_pre_run(
            self,
            reset_batch_id=reset_batch_id,
            is_retry=is_retry,
            has_opening_input=has_opening_input,
            user_message_id=user_message_id,
            note_message_id=note_message_id,
            preparation_scope_operation_id=preparation_scope_operation_id,
        )

    async def paused_notice(self: TurnRunner, *, is_retry: bool) -> None:
        if not pause_before_turn and not is_retry:
            preparation_entered.set()
            await release_preparation.wait()
        await original_notice(self, is_retry=is_retry)

    async def observed_wait(self: RetryCoordinator) -> None:
        retry_checked.set()
        await original_wait(self)

    async def observe_error(_event: Error) -> None:
        retry_checked.set()

    monkeypatch.setattr(TurnRunner, "pre_run", create_autospec(original_pre_run, side_effect=paused_pre_run))
    monkeypatch.setattr(
        TurnRunner, "_compute_workspace_notice", create_autospec(original_notice, side_effect=paused_notice)
    )
    monkeypatch.setattr(
        RetryCoordinator, "_wait_for_existing_run_task", create_autospec(original_wait, side_effect=observed_wait)
    )
    await bus.subscribe(Error, observe_error)
    retry_task: asyncio.Task[None] | None = None
    opener = UserMessage(text="Can you read this document?")
    expected_images: list[str] = []
    if with_image:
        image_path = tmp_path / "document.png"
        Image.new("RGB", (1, 1), color="white").save(image_path)
        opener.text += f" @{image_path}"
        expected_images = [Content.from_data(image_path.read_bytes(), "image/png").uri]
    try:
        async with asyncio.timeout(ENGINE_TURN_TIMEOUT):
            await bus.publish(opener)
            if pause_point == "task-not-started":
                assert not preparation_entered.is_set()
            else:
                await preparation_entered.wait()
            assert engine.history_messages == []
            assert started.clients[0].call_count == 0
            await bus.publish(UserInterrupt())
            retry_task = asyncio.create_task(bus.publish(UserRetry(text=additional_text)))
            await retry_checked.wait()
            assert not [event for event in events if isinstance(event, Error)]
            assert engine.turns.current_input.text == opener.text
            assert engine.turns.current_input.created_at == opener.timestamp
            assert not retry_task.done()
            release_preparation.set()
            await retry_task
            await engine.wait_for_run_task()
    finally:
        release_preparation.set()
        async with asyncio.timeout(DEFAULT_WAIT_TIMEOUT):
            try:
                if retry_task is not None:
                    await retry_task
                await engine.wait_for_run_task()
            finally:
                await bus.unsubscribe(Error, observe_error)

    assert not [event for event in events if isinstance(event, Error)]
    user_messages = [message for message in engine.history_messages if message.role == "user"]
    assert [message.text for message in user_messages] == [opener.text, *([additional_text] if additional_text else [])]
    assert [
        content.uri for content in user_messages[0].contents if content.media_type == "image/png"
    ] == expected_images
    assert not user_messages[0].additional_properties.get(HistoryMarkerKind.INJECTED_KEY)
    if additional_text:
        assert user_messages[1].additional_properties[HistoryMarkerKind.INJECTED_KEY] is True
    assert not [event for event in events if isinstance(event, Warning)]
    assert started.clients[0].call_count == 1
    request_messages, _ = started.clients[0].call_history[0]
    assert [
        content.uri
        for message in request_messages
        if message.role == "user"
        for content in message.contents
        if content.media_type == "image/png"
    ] == expected_images
    assert engine.session.turn_number == 1
    assert engine.turns.current_input.text == ""

    session_id = engine.session_id
    assert session_id is not None
    saved = await state_store.load_session_raw(session_id)
    assert saved is not None
    saved_users = [message for message in saved if message["role"] == "user"]
    assert len(saved_users) == len(user_messages)
    assert [
        content["uri"] for content in saved_users[0]["contents"] if content.get("media_type") == "image/png"
    ] == expected_images
    assert [message["additional_properties"].get(HistoryMarkerKind.INJECTED_KEY, False) for message in saved_users] == [
        False,
        *([True] if additional_text else []),
    ]
    await bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
    assert any(isinstance(event, SessionRestored) and event.session_id == session_id for event in events)
    restored_users = [message for message in engine.history_messages if message.role == "user"]
    assert [message.text for message in restored_users] == [message.text for message in user_messages]
    assert [
        content.uri for content in restored_users[0].contents if content.media_type == "image/png"
    ] == expected_images
    assert engine.turns.current_input.text == ""
    compute_notice.assert_not_called()
    capture_baseline.assert_not_called()
