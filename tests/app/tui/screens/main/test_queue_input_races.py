# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Input and interrupt events cannot act on a draft or run that replaced their owner."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual import events
from textual.message import Message
from textual.widgets import Button

from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.foundation.events.types import InvocationMessage, UserInterrupt
from chrys.foundation.models.invocations import InvocationOrigin
from tests.app.tui.screens.main.test_queue_button_races import _app_with_recorded_controls
from tests.support.waiting import wait_for


@pytest.mark.parametrize("key", ["enter", "x", "backspace", "ctrl+j", "ctrl+z", "paste"])
async def test_input_arriving_while_queued_cannot_edit_or_send_after_cancel(tmp_path: Path, key: str) -> None:
    app, _bus, messages, _controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "old guidance"
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        assert bar.locked
        event = events.Paste("late paste") if key == "paste" else events.Key(key, "x" if key == "x" else None)
        area.post_message(event)
        assert main._input_flow.cancel_pending_injection()
        bar.value = "new draft"
        await pilot.pause()
        assert [message.text for message in messages] == ["old guidance"]
        assert bar.value == "new draft" and not bar.locked
        await pilot.press("x", "enter")
        await wait_for(lambda: len(messages) == 2 and bar.locked, pilot=pilot)
        assert messages[1].text == "new draftx"


@pytest.mark.parametrize("end_turn", [False, True])
async def test_submitted_enter_cannot_retarget_later_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end_turn: bool
) -> None:
    app, _bus, messages, _controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        bar.value = "old guidance"
        await pilot.pause()
        dispatch = bar._dispatch_message
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_submit(message: Message) -> None:
            if isinstance(message, _ChatTextArea.Submitted):
                entered.set()
                await release.wait()
            await dispatch(message)

        monkeypatch.setattr(bar, "_dispatch_message", held_submit)
        try:
            area.post_message(events.Key("enter", None))
            await wait_for(entered.is_set)
            area.post_message(events.Key("x", "x"))
            await wait_for(lambda: bar.value == "old guidancex")
            if end_turn:
                main._set_agent_running(False)
        finally:
            release.set()
        await pilot.pause()
        assert messages == []
        assert bar.value == "old guidancex"
        await pilot.press("enter")
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        assert messages[0].text == "old guidancex"


async def test_enter_accepts_characters_already_waiting_in_its_input_pump(tmp_path: Path) -> None:
    app, _bus, messages, _controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        area = main.query_one(_ChatTextArea)
        area.post_message(events.Key("a", "a"))
        area.post_message(events.Key("b", "b"))
        area.post_message(events.Key("enter", None))
        await wait_for(lambda: len(messages) == 1 and not main._state.submit.active, pilot=pilot)
        assert messages[0].text == "ab"


@pytest.mark.parametrize("boundary", ["publish", "render"])
async def test_late_interrupt_completion_cannot_clear_a_later_turn_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    app, bus, messages, _controls = await _app_with_recorded_controls(tmp_path)
    async with app.run_test(size=(100, 30)):
        main = app._main_screen
        assert main is not None
        main._set_agent_running(True)
        bar = main.query_one(InputBar)
        entered, release = asyncio.Event(), asyncio.Event()
        render = main._view_adapter.render_interrupted

        async def held_render() -> None:
            await render()
            entered.set()
            await release.wait()

        async def held_publish(event: UserInterrupt) -> None:
            entered.set()
            await release.wait()

        if boundary == "render":
            monkeypatch.setattr(main._view_adapter, "render_interrupted", held_render)
        else:
            await bus.subscribe(UserInterrupt, held_publish)
        bar.query_one("#send-btn", Button).press()
        await wait_for(entered.is_set)
        interrupt_workers = list(main.workers)
        try:
            await bus.publish(
                InvocationMessage(
                    origin=InvocationOrigin(kind="turn", invocation_id="old-turn", session_id=None, parent=None),
                    text="done",
                    is_final=True,
                ),
                raise_handler_errors=True,
            )
            assert not main._state.run.agent_running
            bar.value = "new task"
            bar.submit_input()
            await wait_for(lambda: len(messages) == 1 and not main._state.submit.active)
            assert main._state.run.agent_running
            bar.value = "new guidance"
            bar.submit_input()
            await wait_for(lambda: len(messages) == 2 and not main._state.submit.active and bar.locked)
            queued_id = main._state.pending_injection.injection_id
        finally:
            release.set()
            for worker in interrupt_workers:
                await worker.wait()
        assert main._state.run.agent_running and not bar.retry_mode
        assert bar.locked and bar.value == "new guidance"
        assert main._state.pending_injection.matches(queued_id)
