# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Esc-cancelling a queued mid-run injection from the input bar."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from chrys.app.tui.screens.main.input_flow import InputFlowController
from chrys.app.tui.screens.main.navigation import MainNavigationController
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UserInjectCancel, UserMessage
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message


class _FakeInputFlowView:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def set_terminal_title_for_user_message(self, text: str) -> None:
        self.calls.append(f"title:{text}")

    def clear_terminal_title_result(self) -> None:
        self.calls.append("clear_terminal_title_result")

    def lock_input_with_text(self) -> None:
        self.calls.append("lock_input_with_text")

    def unlock_input_keep_if_locked(self) -> None:
        self.calls.append("unlock_input_keep_if_locked")


def _make_controller(
    state: MainScreenState,
    bus: EventBus,
    view: _FakeInputFlowView,
    workers: list[Callable[[], Awaitable[object]]],
) -> InputFlowController:
    async def _noop_agent_message(_event: object) -> None:
        return None

    async def _noop_error(_event: object) -> None:
        return None

    return InputFlowController(
        state=state,
        services=MainScreenServices(bus=bus),
        view=view,  # type: ignore[arg-type]
        start_worker=workers.append,
        handle_agent_message=_noop_agent_message,
        handle_agent_content=_noop_agent_message,
        handle_error=_noop_error,
        set_agent_running=lambda _running: None,
        set_has_messages=lambda _has: None,
        clear_workspace_marker=lambda: None,
        clear_pending_questions=lambda: None,
        commit_profile_switch_marker=lambda: None,
        post_gc_message=lambda _message: None,
        debug=lambda _key, _msg: None,
        pending_switch=lambda _draft: None,
        model_left_out=lambda: None,
        show_error_notice=lambda _error: None,
    )


async def _collect(bus: EventBus, event_type: type) -> list[Any]:
    events: list[Any] = []

    async def _on_event(event: object) -> None:
        events.append(event)

    await bus.subscribe(event_type, _on_event)
    return events


@pytest.mark.asyncio
async def test_queue_injection_tracks_pending_id_and_publishes_it() -> None:
    state = MainScreenState()
    bus = EventBus()
    view = _FakeInputFlowView()
    workers: list[Callable[[], Awaitable[object]]] = []
    controller = _make_controller(state, bus, view, workers)
    messages = await _collect(bus, UserMessage)

    await controller.queue_injection("mid-run note")

    assert len(messages) == 1
    assert messages[0].text == "mid-run note"
    assert messages[0].injection_id is not None
    assert state.pending_injection.matches(messages[0].injection_id)
    assert state.pending_injection.text == "mid-run note"
    assert "lock_input_with_text" in view.calls


@pytest.mark.asyncio
async def test_cancel_pending_injection_unlocks_and_publishes_cancel() -> None:
    state = MainScreenState()
    bus = EventBus()
    view = _FakeInputFlowView()
    workers: list[Callable[[], Awaitable[object]]] = []
    controller = _make_controller(state, bus, view, workers)
    messages = await _collect(bus, UserMessage)
    cancels = await _collect(bus, UserInjectCancel)

    await controller.queue_injection("editable text")
    injection_id = messages[0].injection_id

    assert controller.cancel_pending_injection() is True
    for work in workers:
        await work()

    assert state.pending_injection.active is False
    assert view.calls[-1] == "unlock_input_keep_if_locked"
    assert [c.injection_id for c in cancels] == [injection_id]


@pytest.mark.asyncio
async def test_cancel_without_pending_injection_is_noop() -> None:
    state = MainScreenState()
    bus = EventBus()
    view = _FakeInputFlowView()
    workers: list[Callable[[], Awaitable[object]]] = []
    controller = _make_controller(state, bus, view, workers)
    cancels = await _collect(bus, UserInjectCancel)

    assert controller.cancel_pending_injection() is False
    for work in workers:
        await work()

    assert cancels == []
    assert view.calls == []


@pytest.mark.asyncio
async def test_blocked_injection_submit_clears_pending_tracking() -> None:
    """A hook-blocked injection returns to the input without pending tracking."""
    state = MainScreenState()
    bus = EventBus()
    view = _FakeInputFlowView()
    workers: list[Callable[[], Awaitable[object]]] = []
    controller = _make_controller(state, bus, view, workers)

    async def _block(_event: UserMessage) -> None:
        state.submit.block()

    await bus.subscribe(UserMessage, _block)

    await controller.queue_injection("blocked text")

    assert state.pending_injection.active is False


class _FakeNavigationView:
    def __init__(self) -> None:
        self.confirm_dialogs: list[str] = []
        self.chat_turns: list[str] = []
        self.trajectory_turns: list[str] = []

    def open_confirm_dialog(self, *, title: MessageRef | str, **_kwargs: object) -> None:
        self.confirm_dialogs.append(title if isinstance(title, str) else format_message(title))

    def scroll_chat_to_turn(self, turn_id: str) -> None:
        self.chat_turns.append(turn_id)

    def select_trajectory_turn(self, turn_id: str) -> None:
        self.trajectory_turns.append(turn_id)


def _make_navigation(
    view: _FakeNavigationView,
    *,
    agent_running: bool,
    cancel_result: bool,
    cancel_calls: list[bool],
    dismiss_result: bool = False,
    dismiss_calls: list[bool] | None = None,
    dashboard_visible: bool = False,
) -> MainNavigationController:
    def _cancel() -> bool:
        cancel_calls.append(True)
        return cancel_result

    def _dismiss() -> bool:
        if dismiss_calls is not None:
            dismiss_calls.append(True)
        return dismiss_result

    return MainNavigationController(
        services=MainScreenServices(bus=EventBus()),
        view=view,  # type: ignore[arg-type]
        is_agent_loading=lambda: False,
        is_agent_running=lambda: agent_running,
        is_submit_pending=lambda: False,
        is_workflow_mode=lambda: False,
        has_messages=lambda: True,
        is_dashboard_visible=lambda: dashboard_visible,
        set_interrupt_confirm_active=lambda _active: None,
        publish_interrupt=lambda: None,
        dismiss_suggestions=_dismiss,
        cancel_pending_injection=_cancel,
        delete_current_and_new=_unused_async,
        restore_session=_unused_async,
        flush_notifications=_unused_flush,
        start_worker=lambda work: asyncio.ensure_future(work()),
        debug=lambda _key, _msg: None,
    )


async def _unused_async(_session_id: str) -> None:
    return None


async def _unused_flush() -> None:
    return None


def test_escape_cancels_pending_injection_without_interrupt_confirm() -> None:
    view = _FakeNavigationView()
    cancel_calls: list[bool] = []
    navigation = _make_navigation(view, agent_running=True, cancel_result=True, cancel_calls=cancel_calls)

    navigation.escape()

    assert cancel_calls == [True]
    assert view.confirm_dialogs == []


def test_toc_turn_routes_to_the_foreground_surface() -> None:
    chat_view = _FakeNavigationView()
    dashboard_view = _FakeNavigationView()
    chat_navigation = _make_navigation(chat_view, agent_running=False, cancel_result=False, cancel_calls=[])
    dashboard_navigation = _make_navigation(
        dashboard_view,
        agent_running=False,
        cancel_result=False,
        cancel_calls=[],
        dashboard_visible=True,
    )

    chat_navigation.scroll_to_turn("turn-7")
    dashboard_navigation.scroll_to_turn("turn-7")

    assert chat_view.chat_turns == ["turn-7"]
    assert chat_view.trajectory_turns == []
    assert dashboard_view.chat_turns == []
    assert dashboard_view.trajectory_turns == ["turn-7"]


def test_escape_still_confirms_interrupt_when_nothing_pending() -> None:
    view = _FakeNavigationView()
    cancel_calls: list[bool] = []
    navigation = _make_navigation(view, agent_running=True, cancel_result=False, cancel_calls=cancel_calls)

    navigation.escape()

    assert cancel_calls == [True]
    assert view.confirm_dialogs == ["Interrupt"]


def test_escape_dismisses_suggestions_before_interrupt_confirm() -> None:
    """With the suggestion popup open, Esc collapses it — no interrupt prompt."""
    view = _FakeNavigationView()
    cancel_calls: list[bool] = []
    dismiss_calls: list[bool] = []
    navigation = _make_navigation(
        view,
        agent_running=True,
        cancel_result=True,
        cancel_calls=cancel_calls,
        dismiss_result=True,
        dismiss_calls=dismiss_calls,
    )

    navigation.escape()

    assert dismiss_calls == [True]
    assert cancel_calls == []
    assert view.confirm_dialogs == []


def test_escape_dismisses_suggestions_before_exit_confirm() -> None:
    """Idle agent: Esc collapses the suggestion popup instead of prompting exit."""
    view = _FakeNavigationView()
    cancel_calls: list[bool] = []
    dismiss_calls: list[bool] = []
    navigation = _make_navigation(
        view,
        agent_running=False,
        cancel_result=False,
        cancel_calls=cancel_calls,
        dismiss_result=True,
        dismiss_calls=dismiss_calls,
    )

    navigation.escape()

    assert dismiss_calls == [True]
    assert view.confirm_dialogs == []


def test_escape_falls_through_to_exit_confirm_without_suggestions() -> None:
    view = _FakeNavigationView()
    cancel_calls: list[bool] = []
    dismiss_calls: list[bool] = []
    navigation = _make_navigation(
        view,
        agent_running=False,
        cancel_result=False,
        cancel_calls=cancel_calls,
        dismiss_result=False,
        dismiss_calls=dismiss_calls,
    )

    navigation.escape()

    assert dismiss_calls == [True]
    assert view.confirm_dialogs == ["Exit"]
