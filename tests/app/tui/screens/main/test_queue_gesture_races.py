# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Raw button gestures and pending history reads retain their original draft ownership."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from textual import events
from textual.message import Message
from textual.widgets import Button

from chrys.app.tui.widgets.chrome import input_bar as input_module
from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea, _InputBarButton
from chrys.foundation.events.types import UserInjectCancel, UserMessage
from tests.app.tui.screens.main.test_queue_button_races import _app_with_recorded_controls
from tests.support.waiting import wait_for


def _post_activation(button: Button, gesture: str) -> None:
    event = (
        events.Key("enter", None) if gesture == "enter" else events.Click(button, 1, 0, 0, 0, 1, False, False, False)
    )
    assert button.post_message(event)


@pytest.mark.parametrize("gesture", ["click", "enter"])
@pytest.mark.parametrize("action", ["steer", "cancel"])
@pytest.mark.parametrize("replace_queue", [False, True])
async def test_queued_raw_gesture_cannot_act_after_cancellation(
    tmp_path: Path, gesture: str, action: str, replace_queue: bool
) -> None:
    app, _bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "original guidance"
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        button = bar.query_one("#send-btn" if action == "steer" else "#cancel-btn", Button)
        _post_activation(button, gesture)
        main.action_escape()
        assert not bar.locked
        if replace_queue:
            bar.value = "replacement guidance"
            main._input_flow.submit_user_text(bar.value, bar.draft())
        await pilot.pause()
        for worker in list(main.workers):
            await worker.wait()
        assert len(controls) == 1 and isinstance(controls[0], UserInjectCancel)
        assert main._state.run.agent_running
        assert bar.locked is replace_queue
        assert bar.value == ("replacement guidance" if replace_queue else "original guidance")
        # A new gesture still executes the currently displayed action.
        _post_activation(button if replace_queue else bar.query_one("#send-btn", Button), gesture)
        await wait_for(lambda: len(controls) == 2, pilot=pilot)


@pytest.mark.parametrize("gesture", ["click", "enter"])
@pytest.mark.parametrize("button_id", ["send-btn", "running-send-btn"])
async def test_running_raw_gesture_cannot_submit_after_turn_end(tmp_path: Path, gesture: str, button_id: str) -> None:
    app, _bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "old guidance"
        await pilot.pause()
        _post_activation(bar.query_one(f"#{button_id}", Button), gesture)
        main._set_agent_running(False)
        bar.value = "new unsent draft"
        await pilot.pause()
        assert messages == [] and controls == []
        assert bar.value == "new unsent draft"
        _post_activation(bar.query_one("#send-btn", Button), gesture)
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        assert messages[0].text == "new unsent draft"


async def test_running_send_cannot_retarget_a_later_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "guidance for old run"
        await pilot.pause()
        button = bar.query_one("#running-send-btn", Button)
        dispatch = button._dispatch_message
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held_press(message: Message) -> None:
            if isinstance(message, _InputBarButton.ActionPressed):
                entered.set()
                await release.wait()
            await dispatch(message)

        monkeypatch.setattr(button, "_dispatch_message", held_press)
        try:
            button.press()
            await wait_for(entered.is_set)
            main._set_agent_running(False)
            bar.value = "open new turn"
            bar.submit_input()
            await wait_for(lambda: len(messages) == 1 and not main._state.submit.active)
            assert main._state.run.agent_running
            # Identical text must not make the old action valid in a later turn.
            bar.value = "guidance for old run"
        finally:
            release.set()
        await pilot.pause()
        assert [event.text for event in messages] == ["open new turn"]
        assert controls == [] and not bar.locked
        assert bar.value == "guidance for old run"
        button.press()
        await wait_for(lambda: len(messages) == 2 and bar.locked, pilot=pilot)


async def test_forwarded_interrupt_cannot_stop_a_later_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _bus, _messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        forward = bar.post_message
        captured: list[InputBar.InterruptRequested] = []

        def hold_interrupt(message: Message) -> bool:
            if isinstance(message, InputBar.InterruptRequested):
                captured.append(message)
                return True
            return forward(message)

        monkeypatch.setattr(bar, "post_message", hold_interrupt)
        bar.query_one("#send-btn", Button).press()
        await wait_for(lambda: len(captured) == 1, pilot=pilot)
        main._set_agent_running(False)
        main._set_agent_running(True)
        forward(captured[0])
        await pilot.pause()
        for worker in list(main.workers):
            await worker.wait()
        assert controls == [] and main._state.run.agent_running
        monkeypatch.setattr(bar, "post_message", forward)
        bar.query_one("#send-btn", Button).press()
        await wait_for(lambda: len(controls) == 1, pilot=pilot)


@pytest.mark.parametrize("outcome", ["queued", "cancelled", "new_turn"])
async def test_running_send_revokes_an_inflight_history_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    app, bus, messages, _controls = await _app_with_recorded_controls(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def held_history(*, max_entries: int, instance_id: str) -> list[str]:
        entered.set()
        if not release.wait(5):
            raise RuntimeError("history read was not released")
        return [] if outcome == "new_turn" else ["older history"]

    monkeypatch.setattr(input_module, "load_history", held_history)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None

        async def admit_new_turn(event: UserMessage) -> None:
            main._set_agent_running(False)
            event.started_turn = True

        if outcome == "new_turn":
            await bus.subscribe(UserMessage, admit_new_turn)
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "current guidance"
        await pilot.pause()
        assert area._history.entries == []
        up = asyncio.create_task(pilot.press("up"))
        try:
            await wait_for(lambda: entered.is_set() or up.done())
            if up.done():
                await up
            assert entered.is_set() and not up.done()
            bar.query_one("#running-send-btn", Button).press()
            await wait_for(lambda: len(messages) == 1 and not main._state.submit.active)
            assert messages[0].text == "current guidance"
            if outcome == "cancelled":
                assert main._input_flow.cancel_pending_injection()
            elif outcome == "new_turn":
                await wait_for(lambda: bar.value == "" and area._history.entries == ["current guidance"])
        finally:
            release.set()
            await up
        assert bar.locked is (outcome == "queued")
        assert bar.value == ("" if outcome == "new_turn" else "current guidance")
        assert area._history.index == -1
        if outcome == "queued":
            assert main._input_flow.cancel_pending_injection()
        # Invalidating the old read does not disable subsequent history navigation.
        await pilot.press("up")
        assert bar.value == ("current guidance" if outcome == "new_turn" else "older history")
