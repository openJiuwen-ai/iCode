# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise queue actions through the main screen and its real event routing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Button, TextArea

from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    InvocationMessage,
    UserInjectCancel,
    UserInjectResult,
    UserInjectSteer,
    UserInterrupt,
    UserMessage,
)
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


@pytest.mark.parametrize("terminal", ["completed", "error"])
@pytest.mark.parametrize("outcome", ["consumed", "abandoned", "missing"])
async def test_terminal_queue_releases_composer_before_late_outcome(
    tmp_path: Path, terminal: str, outcome: str
) -> None:
    bus = EventBus()
    messages: list[UserMessage] = []

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        event.started_turn = event.injection_id is None

    await bus.subscribe(UserMessage, admit)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued text"
        bar.focus_input()
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        if terminal == "error":
            await bus.publish(Error(code="executor_error", message="turn failed"), raise_handler_errors=True)
        else:
            await bus.publish(
                InvocationMessage(
                    origin=InvocationOrigin(kind="turn", invocation_id="turn-1", session_id=None, parent=None),
                    text="done",
                    is_final=True,
                ),
                raise_handler_errors=True,
            )
        assert not bar.locked
        assert not main._state.run.agent_running
        assert bar.value == "queued text"
        bar.value = "  next draft  "
        if outcome != "missing":
            await bus.publish(
                UserInjectResult(
                    text=messages[0].text, injection_id=messages[0].injection_id, consumed=outcome == "consumed"
                ),
                raise_handler_errors=True,
            )
        assert bar.value == "  next draft  "
        assert not main._state.pending_injection.active
        assert bar.injection_phase == ""
        assert not bar.query_one("#cancel-btn", Button).display
        # An error offers Retry; switch to the ordinary new-message action under test.
        bar.retry_mode = False
        bar.focus_input()
        await pilot.press("enter")
        await wait_for(lambda: not main._state.submit.active, pilot=pilot)
        assert [message.text for message in messages] == ["queued text", "next draft"]


async def test_rejected_steer_after_terminal_cannot_restore_queue_buttons(tmp_path: Path) -> None:
    bus = EventBus()
    messages: list[UserMessage] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_steer(event: UserInjectSteer) -> None:
        entered.set()
        await release.wait()

    async def admit(event: UserMessage) -> None:
        messages.append(event)
        event.started_turn = event.injection_id is None

    await bus.subscribe(UserMessage, admit)
    await bus.subscribe(UserInjectSteer, hold_steer)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "queued text"
        bar.focus_input()
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        main._input_flow.steer_pending_injection()
        workers = list(main.workers)
        try:
            await wait_for(lambda: entered.is_set() or all(worker.is_finished for worker in workers), pilot=pilot)
            assert entered.is_set()
            assert any(not worker.is_finished for worker in workers)
            main._set_agent_running(False)
            bar.value = "next draft"
        finally:
            release.set()
            for worker in workers:
                await worker.wait()
        assert not bar.locked
        assert bar.injection_phase == ""
        assert not bar.query_one("#cancel-btn", Button).display
        assert str(bar.query_one("#send-btn", Button).label) == "Send"
        await click_when_settled(pilot, "#send-btn")
        await wait_for(lambda: not main._state.submit.active, pilot=pilot)
        assert [message.text for message in messages] == ["queued text", "next draft"]


@pytest.mark.parametrize("action", ["steer", "cancel", "escape", "interrupt"])
@pytest.mark.parametrize("submit", ["enter", "button"])
async def test_main_screen_queue_controls_keep_one_message_and_the_original_draft(
    tmp_path: Path, action: str, submit: str
) -> None:
    bus = EventBus()
    messages: list[UserMessage] = []
    controls: list[object] = []

    async def on_control(event: UserInjectSteer | UserInjectCancel | UserInterrupt) -> None:
        controls.append(event)
        if isinstance(event, UserInjectSteer):
            event.accepted = True

    await bus.subscribe(UserMessage, messages.append)
    await bus.subscribe(UserInjectSteer, on_control)
    await bus.subscribe(UserInjectCancel, on_control)
    await bus.subscribe(UserInterrupt, on_control)
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.value = "  keep the original draft  "
        bar.focus_input()
        assert not main._model_unconfigured()
        assert not bar.agent_loading
        assert bar.display
        if submit == "enter":
            await pilot.press("enter", "enter")
        else:
            await click_when_settled(pilot, "#running-send-btn")
            await pilot.press("enter")
        assert main._state.pending_injection.active, (type(app.screen), bar.value, bar.locked, messages)
        await wait_for(
            lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot, description="one queued message"
        )
        assert bar.query_one(TextArea).has_focus
        assert not bar.query_one("#running-send-btn", Button).display
        assert bar.value == "  keep the original draft  "
        assert str(bar.query_one("#send-btn", Button).label) == "Send Now"
        injection_id = messages[0].injection_id
        assert injection_id is not None

        if action in ("steer", "cancel"):
            await click_when_settled(pilot, "#send-btn" if action == "steer" else "#cancel-btn")
        else:
            await pilot.press("escape" if action == "escape" else "ctrl+b")
        await wait_for(lambda: len(controls) == 1, pilot=pilot, description="queue action reaches backend")
        expected = (
            UserInjectSteer if action == "steer" else UserInterrupt if action == "interrupt" else UserInjectCancel
        )
        assert isinstance(controls[0], expected)
        assert len(messages) == 1
        if action == "steer":
            assert bar.locked and bar.injection_phase == "steering"
            assert str(bar.query_one("#send-btn", Button).label) == "Interrupt"
            assert not bar.query_one("#cancel-btn", Button).display
            assert main.agent_running_state
            await bus.publish(
                UserInjectResult(text=messages[0].text, injection_id=injection_id, consumed=True),
                raise_handler_errors=True,
            )
            await wait_for(lambda: not bar.locked, pilot=pilot, description="delivered message releases composer")
            assert bar.value == ""
            assert main.agent_running_state
        else:
            await wait_for(lambda: not bar.locked, pilot=pilot, description="cancel or stop releases composer")
            assert bar.value == "  keep the original draft  "
            assert main.agent_running_state is (action != "interrupt")
            assert bar.query_one("#running-send-btn", Button).display is (action != "interrupt")
            # A late abandoned result cannot erase the user's next edit.
            bar.value = "new draft"
            await bus.publish(
                UserInjectResult(text=messages[0].text, injection_id=injection_id, consumed=False),
                raise_handler_errors=True,
            )
            assert bar.value == "new draft"


async def test_queue_and_steering_controls_only_refresh_the_input_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from textual.widgets import Static

    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter
    from tests.support.pilot_barrier import screen_is_settled

    bus = EventBus()
    settings, registry = make_mock_settings_and_registry()
    app = make_chrys_app(tmp_path, event_bus=bus, settings=settings, model_registry=registry)

    async def promote(event: UserInjectSteer) -> None:
        event.accepted = True

    await bus.subscribe(UserInjectSteer, promote)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        await panel.mount(*(Static(f"Transcript row {index}") for index in range(64)))
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        bar.focus_input()
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled populated screen")
        layouts: list[None] = []
        restyles: list[bool] = []
        recomposes: list[None] = []
        monkeypatch.setattr(main, "_refresh_layout", lambda *args, **kwargs: layouts.append(None))
        monkeypatch.setattr(main, "update_node_styles", lambda animate=True: restyles.append(animate))

        async def recompose() -> None:
            recomposes.append(None)

        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", recompose)
        await pilot.press("h")
        assert bar.query_one("#running-send-btn", Button).display
        await pilot.press("backspace")
        assert not bar.query_one("#running-send-btn", Button).display
        await pilot.press("h")
        await click_when_settled(pilot, "#running-send-btn")
        await wait_for(
            lambda: bar.injection_phase == "queued" and not main._state.submit.active,
            pilot=pilot,
            description="queued composer",
        )
        await click_when_settled(pilot, "#send-btn")
        await wait_for(lambda: bar.injection_phase == "steering", pilot=pilot, description="steering composer")
        assert layouts == []
        assert restyles == []
        assert recomposes == []
