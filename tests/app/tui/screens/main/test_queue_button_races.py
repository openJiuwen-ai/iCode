# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Queue buttons keep the action and message ownership present at the user's press."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Button

from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    UserInjectCancel,
    UserInjectResult,
    UserInjectSteer,
    UserInterrupt,
    UserMessage,
)
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from chrys.app.tui.app import ChrysApp

type QueueControl = UserInjectCancel | UserInjectSteer | UserInterrupt


async def _app_with_recorded_controls(
    tmp_path: Path,
) -> tuple[ChrysApp, EventBus, list[UserMessage], list[QueueControl]]:
    bus = EventBus()
    messages: list[UserMessage] = []
    controls: list[QueueControl] = []

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        event.started_turn = event.injection_id is None

    async def control(event: QueueControl) -> None:
        controls.append(event)
        if isinstance(event, UserInjectSteer):
            event.accepted = True

    await bus.subscribe(UserMessage, admit)
    await bus.subscribe(UserInjectSteer, control)
    await bus.subscribe(UserInjectCancel, control)
    await bus.subscribe(UserInterrupt, control)
    settings, registry = make_mock_settings_and_registry()
    return make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry), bus, messages, controls


async def test_steer_press_during_delivery_does_not_interrupt_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued guidance"
        bar.focus_input()
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        ready = asyncio.Event()
        release = asyncio.Event()
        original = main._view_adapter.add_user_injection

        async def delayed_bubble(text: str, *, created_at: datetime, item_id: str | None = None) -> None:
            await original(text, created_at=created_at, item_id=item_id)
            ready.set()
            await release.wait()

        monkeypatch.setattr(main._view_adapter, "add_user_injection", delayed_bubble)
        delivery = asyncio.create_task(
            bus.publish(
                UserInjectResult(text=messages[0].text, injection_id=messages[0].injection_id, consumed=True),
                raise_handler_errors=True,
            )
        )
        try:
            await wait_for(lambda: ready.is_set() or delivery.done(), pilot=pilot)
            if delivery.done():
                await delivery
            assert ready.is_set() and not delivery.done()
            send = bar.query_one("#send-btn", Button)
            assert str(send.label) == "Send Now"
            # Button.press is the pointer/keyboard activation path. Its Pressed
            # message is queued while the consumed result releases the draft.
            send.press()
            release.set()
            await delivery
            await pilot.pause()
            assert controls == []
            assert main._state.run.agent_running
            assert bar.value == ""
            assert not bar.locked
            # The now-visible Interrupt still has its ordinary meaning.
            send.press()
            await wait_for(lambda: len(controls) == 1, pilot=pilot)
            assert isinstance(controls[0], UserInterrupt)
        finally:
            release.set()
            await delivery


@pytest.mark.parametrize("action", ["steer", "cancel"])
@pytest.mark.parametrize("hop", ["button", "screen"])
async def test_old_queue_action_cannot_target_a_replacement_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, hop: str
) -> None:
    app, bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "first guidance"
        bar.focus_input()
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        button = bar.query_one("#send-btn" if action == "steer" else "#cancel-btn", Button)
        held: list[InputBar.SteerRequested | InputBar.CancelQueueRequested] = []
        original_post = bar.post_message

        if hop == "screen":
            from textual.message import Message

            def hold_action(message: Message) -> bool:
                if isinstance(message, InputBar.SteerRequested | InputBar.CancelQueueRequested):
                    held.append(message)
                    return True
                return original_post(message)

            monkeypatch.setattr(bar, "post_message", hold_action)
        button.press()
        if hop == "screen":
            await wait_for(lambda: len(held) == 1, pilot=pilot)
            monkeypatch.setattr(bar, "post_message", original_post)

        # An abandoned result has no mount await: the old click is still queued
        # while the draft is released and the next injection reserves the bar.
        await bus.publish(
            UserInjectResult(text=messages[0].text, injection_id=messages[0].injection_id, consumed=False),
            raise_handler_errors=True,
        )
        bar.value = "replacement guidance"
        main._input_flow.submit_user_text(bar.value, bar.draft())
        replacement_id = main._state.pending_injection.injection_id
        assert replacement_id is not None and replacement_id != messages[0].injection_id
        if held:
            original_post(held[0])
        await wait_for(lambda: len(messages) == 2 and not main._state.submit.active, pilot=pilot)
        await pilot.pause()
        assert controls == []
        assert main._state.pending_injection.injection_id == replacement_id
        assert bar.value == "replacement guidance"
        assert bar.injection_phase == "queued"

        # A fresh press targets the replacement, proving that it remains usable.
        button.press()
        await wait_for(lambda: len(controls) == 1, pilot=pilot)
        expected = UserInjectSteer if action == "steer" else UserInjectCancel
        assert isinstance(controls[0], expected)
        assert controls[0].injection_id == replacement_id


async def test_two_queued_steer_presses_cannot_turn_into_interrupt(tmp_path: Path) -> None:
    app, _bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued guidance"
        bar.focus_input()
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        send = bar.query_one("#send-btn", Button)
        send.press()
        send.press()
        await wait_for(lambda: len(controls) == 1, pilot=pilot)
        await pilot.pause()
        assert len(controls) == 1 and isinstance(controls[0], UserInjectSteer)
        assert bar.injection_phase == "steering"
        assert main._state.run.agent_running
        send.press()
        await wait_for(lambda: len(controls) == 2, pilot=pilot)
        assert isinstance(controls[1], UserInterrupt)


@pytest.mark.parametrize("queued", [False, True], ids=["interrupt", "steer"])
async def test_old_running_action_cannot_submit_a_draft_after_turn_end(tmp_path: Path, queued: bool) -> None:
    app, _bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        if queued:
            bar.value = "queued guidance"
            bar.focus_input()
            await pilot.press("enter")
            await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        sent_before = len(messages)
        send = bar.query_one("#send-btn", Button)
        send.press()
        main._set_agent_running(False)
        bar.value = "new unsent draft"
        await pilot.pause()
        assert controls == []
        assert len(messages) == sent_before
        assert bar.value == "new unsent draft"
        send.press()
        await wait_for(lambda: len(messages) == sent_before + 1, pilot=pilot)
        assert messages[-1].text == "new unsent draft"


async def test_repeated_running_send_presses_queue_one_message(tmp_path: Path) -> None:
    app, _bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "additional guidance"
        await pilot.pause()
        send = bar.query_one("#running-send-btn", Button)
        assert send.display
        send.press()
        send.press()
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        await pilot.pause()
        assert len(messages) == 1 and controls == []
        assert bar.locked and bar.injection_phase == "queued"
        assert not send.display
        assert bar.value == "additional guidance"


async def test_late_running_send_press_does_not_start_a_new_turn(tmp_path: Path) -> None:
    app, _bus, messages, controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "guidance for the running turn"
        await pilot.pause()
        bar.query_one("#running-send-btn", Button).press()
        main._set_agent_running(False)
        bar.value = "new draft"
        await pilot.pause()
        assert messages == [] and controls == []
        assert bar.value == "new draft"
        assert not bar.query_one("#running-send-btn", Button).display
        bar.query_one("#send-btn", Button).press()
        await wait_for(lambda: len(messages) == 1, pilot=pilot)
        assert messages[0].text == "new draft"
