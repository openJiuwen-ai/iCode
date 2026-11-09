# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Discard links keep copy in place and preview only mounted, affected content."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from rich.cells import cell_len
from textual.color import Color
from textual.widgets import Button

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    InterruptedMessage,
    MessageCopyButton,
    MessageHeaderRow,
    MessageRollbackButton,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.foundation.config.settings import Settings
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import WidgetApp, click_when_settled
from tests.support.waiting import wait_for


@pytest.fixture(autouse=True)
def no_native_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )


@pytest.mark.parametrize("width", [32, 80])
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_undo_link_is_complete_at_the_right_and_copy_keeps_its_position(width: int, locale: str) -> None:
    message = UserMessage("Question", timestamp="- 10:41 AM", turn_number=1, rollback_available=True)
    from chrys.app.tui.i18n import LocaleController

    app = WidgetApp(lambda: message)
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(width, 12)) as pilot:
        header = message.query_one(MessageHeaderRow)
        copy = message.query_one(MessageCopyButton)
        link = message.query_one(MessageRollbackButton)
        assert link.render().plain == ("↺ Discard this turn and later" if locale == "en" else "↺ 撤回本轮及之后")
        assert link.region.right == header.content_region.right - 1
        assert link.content_region.right == header.content_region.right - 1
        assert link.content_region.width == cell_len(link.render().plain)
        message.set_rollback_available(False)
        await pilot.pause()
        copy_x = copy.region.x
        message.set_rollback_available(True)
        await pilot.pause()
        assert copy.region.x == copy_x
        assert link.content_region.width == cell_len(link.render().plain)
        # The right margin is outside the clickable label.
        disabled = Mock(wraps=message.set_rollback_disabled)
        original = message.set_rollback_disabled
        message.set_rollback_disabled = disabled
        try:
            await click_when_settled(pilot, header, offset=(header.content_size.width - 1, 0))
            disabled.assert_not_called()
        finally:
            message.set_rollback_disabled = original


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("compressed", [False, True])
async def test_hover_marks_the_suffix_restores_gray_and_never_relayouts_main_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    compressed: bool,
) -> None:
    app = make_chrys_app(tmp_path, settings=Settings(locale=locale))
    async with app.run_test(size=(100, 40)) as pilot:
        main = app.screen
        assert isinstance(main, MainScreen)
        main._set_has_messages(True)
        panel = main.query_one(ChatPanel)
        panel.set_rollback_targets(set(range(21)))
        users: list[UserMessage] = []
        answers: list[AgentMessage] = []
        for turn in range(16, 20):
            user = UserMessage(f"Question {turn}", turn_number=turn, rollback_available=True, compressed=compressed)
            answer = AgentMessage(f"Answer {turn}", is_compressed=compressed)
            users.append(user)
            answers.append(answer)
            await panel.mount(user, answer)
        # Include a live turn after the earlier mounted messages.
        await panel.add_user_message("Last question")
        panel.apply_turn_started(20)
        users.append(panel.query(UserMessage).last())
        await panel.add_agent_message("Last answer")
        answers.append(panel.query(AgentMessage).last())
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="populated main screen settled")
        original = {widget: widget.styles.border_left for widget in [*users, *answers]}
        layouts = Mock(wraps=main._refresh_layout)
        restyles = Mock(wraps=main.update_node_styles)
        footer = AsyncMock(wraps=main.query_one(ChrysFooter).recompose)
        arrange = Mock(wraps=main._compositor._arrange_root)
        monkeypatch.setattr(main._compositor, "_arrange_root", arrange)
        monkeypatch.setattr(main, "_refresh_layout", layouts)
        monkeypatch.setattr(main, "update_node_styles", restyles)
        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", footer)
        link = users[1].query_one(MessageRollbackButton)
        await pilot.hover(link)
        await pilot.pause()
        error = Color.parse(app.get_css_variables()["error"])
        assert link.tooltip is None
        assert users[0].styles.border_left == original[users[0]]
        assert answers[0].styles.border_left == original[answers[0]]
        assert all(widget.styles.border_left[1] == error for widget in [*users[1:], *answers[1:]])
        await pilot.hover(users[0].query_one(MessageCopyButton))
        await pilot.pause()
        assert all(widget.styles.border_left == previous for widget, previous in original.items())
        await pilot.hover(users[-1].query_one(MessageRollbackButton))
        await pilot.pause()
        assert users[-1].query_one(MessageRollbackButton).tooltip is None
        assert users[-1].styles.border_left[1] == error
        assert answers[-1].styles.border_left[1] == error
        assert all(widget.styles.border_left == original[widget] for widget in [*users[:-1], *answers[:-1]])
        await pilot.hover(users[0].query_one(MessageCopyButton))
        await pilot.pause()
        arrange.assert_not_called()
        layouts.assert_not_called()
        restyles.assert_not_called()
        footer.assert_not_called()
        # Positive control: force a real layout so the spy cannot pass on an unused path.
        main.refresh(layout=True)
        await pilot.pause()
        layouts.assert_called()
        arrange.assert_called()


@pytest.mark.parametrize(
    ("locale", "action", "discard"), [("en", "Continue", "Discard This Turn"), ("zh-Hans", "继续", "撤回本轮")]
)
async def test_interruption_uses_discard_beside_continue(locale: str, action: str, discard: str) -> None:
    from chrys.app.tui.i18n import LocaleController

    card = InterruptedMessage("Stopped", action_label=action, offers_discard=True)
    app = WidgetApp(lambda: card)
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test():
        assert [str(button.label) for button in card.query(Button)] == [action, discard]


@pytest.mark.parametrize("end", ["leave", "clear", "unmount"])
async def test_hover_preview_includes_new_replies_and_tool_groups_and_restores_them(end: str) -> None:
    panel = ChatPanel()
    app = WidgetApp(lambda: panel)
    async with app.run_test(size=(100, 40)) as pilot:
        await panel.add_user_message("Earlier")
        panel.apply_turn_started(1)
        await panel.add_agent_message("Earlier answer")
        earlier = panel.query_one(UserMessage)
        panel.set_rollback_targets({0, 1})
        await panel.add_user_message("Current")
        panel.apply_turn_started(2)
        current = panel.query(UserMessage).last()
        await pilot.hover(current.query_one(MessageRollbackButton))
        error = Color.parse(app.get_css_variables()["error"])
        assert current.styles.border_left[1] == error
        await panel.add_answer_delta("new", "New streamed answer")
        panel.finish_answer_segment("new", completed=True)
        answer = panel.query(AgentMessage).last()
        await panel.add_tool_start("read", "read_file", "file.read", args={"path": "example.py"})
        group = panel.query_one(ToolGroup)
        await wait_for(
            lambda: answer.styles.border_left[1] == error and group.styles.border_left[1] == error,
            pilot=pilot,
            description="hover preview includes newly mounted answer and tools",
        )
        assert earlier.styles.border_left[1] != error
        if end == "leave":
            await pilot.hover(earlier.query_one(MessageCopyButton))
        elif end == "clear":
            await panel.clear()
        else:
            await panel.remove()
        await wait_for(
            lambda: all(widget.styles.border_left[1] != error for widget in (current, answer, group)),
            pilot=pilot,
            description="preview colors restored when hover or transcript ends",
        )
        assert panel._rollback_preview_start is None
        assert not panel._rollback_preview_borders


async def test_removing_a_previewed_widget_releases_its_record_without_ending_the_hover() -> None:
    panel = ChatPanel()
    async with WidgetApp(lambda: panel).run_test(size=(100, 30)) as pilot:
        await panel.add_user_message("Question")
        panel.apply_turn_started(1)
        await panel.add_answer_delta("withdrawn", "Answer to remove")
        answer = panel.query_one(AgentMessage)
        user = panel.query_one(UserMessage)
        original = answer.styles.border_left
        await pilot.hover(user.query_one(MessageRollbackButton))
        assert answer in panel._rollback_preview_borders
        await panel.remove_transcript_widget(answer)
        await wait_for(
            lambda: answer not in panel._rollback_preview_borders,
            pilot=pilot,
            description="unmounted preview entry released",
        )
        assert answer.styles.border_left == original
        assert panel._rollback_preview_start is user
        await pilot.hover(user.query_one(MessageCopyButton))
        assert not panel._rollback_preview_borders
