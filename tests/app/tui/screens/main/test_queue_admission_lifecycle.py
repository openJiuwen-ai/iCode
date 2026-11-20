# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep admission and draft ownership distinct when a queued message outlives its turn."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from textual import events
from textual.message import Message
from textual.widgets import TextArea

from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationMessage, UserInjectCancel, UserInjectResult, UserMessage
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for


async def test_edit_and_undo_before_changed_dispatch_cannot_clear_the_returned_draft(tmp_path: Path) -> None:
    from textual.widgets import TextArea

    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        bar = main.query_one(InputBar)
        bar.value = "original draft"
        await pilot.pause()
        submission = main._view_adapter.lock_input_with_text()
        bar.unlock_and_keep()
        area = bar.query_one(TextArea)
        # These user edit actions post Changed without waiting for the parent's
        # message pump. A returning admission worker can see the same text again.
        area.insert(" changed")
        area.action_undo()
        assert bar.draft() == submission.draft
        main._view_adapter.clear_submitted_input(submission)
        assert bar.value == "original draft"


async def _finish_old_turn(bus: EventBus) -> None:
    await bus.publish(
        InvocationMessage(
            origin=InvocationOrigin(kind="turn", invocation_id="old-turn", session_id=None, parent=None),
            text="done",
            is_final=True,
        ),
        raise_handler_errors=True,
    )


@pytest.mark.parametrize("edit", ["unchanged", "new", "changed_back"])
async def test_queued_admission_clears_only_the_unedited_sent_draft(tmp_path: Path, edit: str) -> None:
    bus = EventBus()
    entered = asyncio.Event()
    release = asyncio.Event()
    messages: list[UserMessage] = []

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        entered.set()
        await release.wait()
        event.started_turn = True

    await bus.subscribe(UserMessage, admit)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued original"
        bar.focus_input()
        await pilot.press("enter")
        workers = list(main.workers)
        try:
            await wait_for(lambda: entered.is_set() or all(worker.is_finished for worker in workers), pilot=pilot)
            assert entered.is_set()
            assert any(not worker.is_finished for worker in workers)
            await _finish_old_turn(bus)
            assert not bar.locked
            assert main._state.submit.active
            if edit != "unchanged":
                await pilot.press("ctrl+a", *"new draft")
            if edit == "changed_back":
                await pilot.press("ctrl+a", *"queued original")
        finally:
            release.set()
            for worker in workers:
                await worker.wait()
        assert main._state.run.agent_running
        assert [event.text for event in messages] == ["queued original"]
        assert bar.value == {"unchanged": "", "new": "new draft", "changed_back": "queued original"}[edit]
        assert not bar.locked
        assert not main._state.submit.active


async def test_escape_after_terminal_still_withdraws_the_inflight_queued_admission(tmp_path: Path) -> None:
    bus = EventBus()
    entered = asyncio.Event()
    release = asyncio.Event()
    cancelled: list[str] = []
    messages: list[UserMessage] = []

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        entered.set()
        await release.wait()
        event.started_turn = event.injection_id not in cancelled

    async def cancel(event: UserInjectCancel) -> None:
        cancelled.append(event.injection_id)

    await bus.subscribe(UserMessage, admit)
    await bus.subscribe(UserInjectCancel, cancel)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued original"
        bar.focus_input()
        await pilot.press("enter")
        workers = list(main.workers)
        try:
            await wait_for(lambda: entered.is_set() or all(worker.is_finished for worker in workers), pilot=pilot)
            assert entered.is_set()
            assert any(not worker.is_finished for worker in workers)
            await _finish_old_turn(bus)
            await pilot.press("escape")
            assert app.screen is main
            await wait_for(lambda: bool(cancelled), pilot=pilot, description="queued admission is withdrawn")
            assert cancelled == [messages[0].injection_id]
            assert bar.value == "queued original"
            assert not bar.locked
        finally:
            release.set()
            for worker in workers:
                await worker.wait()
        assert not main._state.run.agent_running
        assert not main._state.submit.active
        assert not messages[0].started_turn


async def test_terminal_before_queue_worker_starts_does_not_discard_the_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    messages: list[UserMessage] = []
    scheduled: list[Callable[[], Awaitable[object]]] = []

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        event.started_turn = True

    await bus.subscribe(UserMessage, admit)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued original"
        bar.focus_input()
        monkeypatch.setattr(main._input_flow, "_start_worker", scheduled.append)
        await pilot.press("enter")
        assert len(scheduled) == 1
        assert main._state.submit.active
        assert bar.locked
        await _finish_old_turn(bus)
        assert not bar.locked
        assert messages == []
        await asyncio.create_task(scheduled.pop()())
        assert [event.text for event in messages] == ["queued original"]
        assert main._state.run.agent_running
        assert not main._state.submit.active
        assert bar.value == ""


async def test_queue_worker_cannot_send_to_a_replacement_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    messages: list[UserMessage] = []
    scheduled: list[Callable[[], Awaitable[object]]] = []
    generation = [0]

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        event.started_turn = True

    await bus.subscribe(UserMessage, admit)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        monkeypatch.setattr(main._services, "session_generation", lambda: generation[0])
        monkeypatch.setattr(main._input_flow, "_start_worker", scheduled.append)
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "old session message"
        bar.focus_input()
        await pilot.press("enter")
        assert len(scheduled) == 1
        await _finish_old_turn(bus)
        generation[0] += 1
        bar.value = "new session draft"
        await asyncio.create_task(scheduled.pop()())
        assert messages == []
        assert bar.value == "new session draft"
        assert not bar.locked
        assert not main._state.submit.active
        assert not main._state.run.agent_running


@pytest.mark.parametrize("outcome", ["fresh_turn", "consumed"])
async def test_edit_before_submit_message_dispatch_survives_successful_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    bus = EventBus()
    messages: list[UserMessage] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        if outcome == "fresh_turn":
            await _finish_old_turn(bus)
            event.started_turn = True

    await bus.subscribe(UserMessage, admit)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)):
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued original"
        bar.focus_input()
        dispatch = main._dispatch_message

        async def hold_submit(message: Message) -> None:
            if isinstance(message, InputBar.UserSubmitted):
                entered.set()
                await release.wait()
            await dispatch(message)

        monkeypatch.setattr(main, "_dispatch_message", hold_submit)
        try:
            # The real submission message waits on the screen's queue while
            # the text area's independent message pump accepts the next key.
            bar.submit_input()
            await wait_for(entered.is_set, description="submit awaiting screen dispatch")
            assert messages == []
            assert not bar.locked
            area = bar.query_one(TextArea)
            assert area.post_message(events.Key("x", "x"))
            await wait_for(lambda: bar.value == "queued originalx", description="later key updates the draft")
        finally:
            release.set()
        await wait_for(
            lambda: len(messages) == 1 and not main._state.submit.active and main._state.run.agent_running,
            description="queued message admitted as a new turn",
        )
        for worker in list(main.workers):
            await worker.wait()
        assert messages[0].text == "queued original"
        if outcome == "consumed":
            await bus.publish(
                UserInjectResult(text=messages[0].text, injection_id=messages[0].injection_id, consumed=True),
                raise_handler_errors=True,
            )
        assert bar.value == "queued originalx"
        assert not bar.locked
