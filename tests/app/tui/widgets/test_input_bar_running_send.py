# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Running Send shares Enter's routing without disturbing Interrupt or input focus."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, TextArea

from chrys.app.tui.widgets.chrome.input_bar import InputBar, SubmitRequest, SubmitRoute
from tests.support.tui_helpers import click_when_settled


class _RunningSendApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.selections: list[bool] = []
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_input_bar_suggestion_select(self, event: InputBar.SuggestionSelect) -> None:
        self.selections.append(event.execute)

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event.text)


@pytest.mark.parametrize("width", [45, 80, 120])
async def test_running_send_visibility_geometry_and_focus(width: int) -> None:
    async with _RunningSendApp().run_test(size=(width, 12)) as pilot:
        bar = pilot.app.query_one(InputBar)
        area = bar.query_one(TextArea)
        send = bar.query_one("#running-send-btn", Button)
        interrupt = bar.query_one("#send-btn", Button)
        bar.value = "draft"
        assert not send.display
        bar.agent_running = True
        await pilot.pause()
        assert send.display and not send.disabled
        assert area.has_focus
        assert str(send.label) == "Send"
        assert str(interrupt.label) == "Interrupt"
        assert not send.has_class("-stop") and interrupt.has_class("-stop")
        group = bar.query_one("#btn-group")
        assert send.region.x == group.content_region.x
        assert interrupt.region.x == send.region.right + 1
        assert interrupt.region.right == group.content_region.right
        assert group.region.right < bar.region.right
        assert area.region.width >= 4
        # The cached focus chain must update when a previously hidden button appears.
        chain = pilot.app.screen.focus_chain
        assert chain.index(send) + 1 == chain.index(interrupt)
        await pilot.press("tab")
        assert send.has_focus
        await pilot.press("tab")
        assert interrupt.has_focus
        send.focus()
        await pilot.pause()
        assert send.has_focus
        bar.value = " \n\t "
        await pilot.pause()
        assert not send.display
        assert area.has_focus
        assert interrupt.region.x == group.content_region.x
        assert send not in pilot.app.screen.focus_chain
        assert interrupt in pilot.app.screen.focus_chain
        await pilot.press("tab")
        assert interrupt.has_focus
        bar.focus_input()
        bar.value = "draft"
        bar.lock_with_text()
        await pilot.pause()
        assert not send.display
        assert str(interrupt.label) == "Send Now"
        cancel = bar.query_one("#cancel-btn", Button)
        assert cancel.display and cancel in pilot.app.screen.focus_chain
        bar.injection_phase = "steering"
        await pilot.pause()
        assert not send.display
        assert str(interrupt.label) == "Interrupt"
        assert cancel not in pilot.app.screen.focus_chain
        bar.unlock_and_keep()
        await pilot.pause()
        assert send.display and area.has_focus
        bar.agent_running = False
        await pilot.pause()
        assert not send.display
        assert str(interrupt.label) == "Send"


@pytest.mark.parametrize("guard", ["agent_loading", "retry_pending"])
async def test_running_send_respects_enter_submission_guards(guard: str) -> None:
    async with _RunningSendApp().run_test() as pilot:
        bar = pilot.app.query_one(InputBar)
        bar.agent_running = True
        bar.value = "draft"
        setattr(bar, guard, True)
        await pilot.pause()
        send = bar.query_one("#running-send-btn", Button)
        assert not send.display and send.disabled
        send.press()
        await pilot.press("enter")
        assert pilot.app.submitted == []
        assert bar.value == "draft"


@pytest.mark.parametrize("gesture", ["enter", "button"])
@pytest.mark.parametrize("suggestions", [False, True])
async def test_running_send_matches_enter_routing(gesture: str, suggestions: bool) -> None:
    async with _RunningSendApp().run_test() as pilot:
        bar = pilot.app.query_one(InputBar)
        requests: list[SubmitRequest] = []

        def route(request: SubmitRequest) -> SubmitRoute:
            requests.append(request)
            return "taken"

        bar.submit_router = route
        bar.agent_running = True
        bar.value = "@src" if suggestions else "/help"
        bar.set_suggestions_active(suggestions, mode="files")
        if gesture == "enter":
            await pilot.press("enter")
        else:
            await click_when_settled(pilot, "#running-send-btn")
        if suggestions:
            assert requests == []
            assert pilot.app.selections == [True]
        else:
            assert len(requests) == 1
            assert requests[0].draft.text == "/help"
            assert requests[0].runs_commands and not requests[0].continue_turn
        assert pilot.app.submitted == []


@pytest.mark.parametrize("change", ["run", "draft"])
async def test_running_send_cannot_outlive_its_run_or_draft(change: str) -> None:
    async with _RunningSendApp().run_test() as pilot:
        bar = pilot.app.query_one(InputBar)
        bar.agent_running = True
        bar.value = "original draft"
        await pilot.pause()
        send = bar.query_one("#running-send-btn", Button)
        send.press()
        if change == "run":
            bar.agent_running = False
            bar.agent_running = True
        else:
            bar.value = "changed draft"
            bar.value = "original draft"
        await pilot.pause()
        assert pilot.app.submitted == []
        assert bar.value == "original draft"
        send.press()
        await pilot.pause()
        assert pilot.app.submitted == ["original draft"]
