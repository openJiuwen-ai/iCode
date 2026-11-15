# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise queue actions through the main screen and its real event routing."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, TextArea

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
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


@pytest.mark.parametrize("action", ["steer", "cancel", "escape", "interrupt"])
async def test_main_screen_queue_controls_keep_one_message_and_the_original_draft(tmp_path: Path, action: str) -> None:
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
        await pilot.press("enter", "enter")
        assert main._state.pending_injection.active, (type(app.screen), bar.value, bar.locked, messages)
        await wait_for(
            lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot, description="one queued message"
        )
        assert bar.query_one(TextArea).has_focus
        assert bar.value == "  keep the original draft  "
        assert str(bar.query_one("#send-btn", Button).label) == "Steer"
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
        bar.value = "additional guidance"
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
        await pilot.press("enter")
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
