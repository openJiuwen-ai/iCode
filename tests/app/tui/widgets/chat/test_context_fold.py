# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Context compression summaries as they appear and respond in the transcript."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.events import Click, MouseDown, MouseMove, MouseUp
from textual.widgets import Static, TextArea

from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.messages import AgentMessage, MessageCopyButton
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.state.folds import FoldItem
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import ChatPanelApp, click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from rich.console import RenderableType
    from textual.pilot import Pilot
    from textual.screen import Screen

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

_SUMMARY = "User: [red]literal[/red]\nAssistant: **plain text**"


@pytest.fixture(autouse=True)
def no_native_branch_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


@pytest.mark.parametrize("replayed", [False, True], ids=["live", "replayed"])
async def test_context_fold_first_frame_is_one_collapsed_row(replayed: bool) -> None:
    async with ChatPanelApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        if replayed:
            block = CompressedBlock("ctx", summary_text=_SUMMARY, turn_range=(3, 3))
            await panel.replay_history([], folds=[FoldItem("ctx", _SUMMARY, block=block)])
            await panel.wait_replay_complete()
        else:
            await panel.add_context_fold("ctx", _SUMMARY, 5, (3, 3))

        fold = panel.query_one(ContextFoldWidget)
        title, body = fold.query(Static)
        assert fold.title_text == "▸ Compressed (Turn 3)"
        assert str(title.render()).strip() == "═══ ▸ Compressed (Turn 3) ═══"
        assert not body.display
        await wait_for(lambda: fold.region.height > 0, pilot=pilot, description="the compression row is laid out")
        assert fold.region.height == title.region.height == 1
        assert body not in pilot.app.screen._compositor.visible_widgets


async def test_clicking_the_whole_title_toggles_without_moving_composer_focus(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        panel = app.screen.query_one(ChatPanel)
        composer = app.screen.query_one(InputBar).query_one(TextArea)
        composer.focus()
        await panel.add_context_fold("ctx", _SUMMARY, 24)
        fold = panel.query_one(ContextFoldWidget)
        title, body = fold.query(Static)
        await wait_for(lambda: title.region.width > 40, pilot=pilot, description="the title fills the transcript")

        await click_when_settled(pilot, title, offset=(40, 0))
        assert panel.query_one(ContextFoldWidget).title_text == "▾ Compressed (24 messages)"
        assert body.display
        assert app.screen.focused is composer

        await click_when_settled(pilot, title)
        assert panel.query_one(ContextFoldWidget).title_text == "▸ Compressed (24 messages)"
        assert not body.display
        assert app.screen.focused is composer
        await wait_for(lambda: fold.region.height == 1, pilot=pilot, description="the collapsed body gives up its rows")


async def test_keyboard_fold_toggles_all_summaries_and_tools_without_moving_focus(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 40)) as pilot:
        panel = app.screen.query_one(ChatPanel)
        await panel.add_tool_start("c1", "read_file", "filesystem.read", "a.py")
        await panel.add_tool_result("c1", "read_file", "ok")
        for index in range(3):
            await panel.add_context_fold(f"ctx-{index}", f"Summary {index}")
        folds = list(panel.query(ContextFoldWidget))
        group = panel.query_one(ToolGroup)
        assert group.collapsed and all(not fold.query_one("#summary").display for fold in folds)
        composer = app.screen.query_one(InputBar).query_one(TextArea)
        composer.focus()

        for expanded, arrow in ((True, "▾"), (False, "▸")):
            await pilot.press("slash", "f", "o", "l", "d", "enter")
            await wait_for(
                lambda: screen_is_settled(app, app.screen), pilot=pilot, description="keyboard fold laid out"
            )
            assert group.collapsed is not expanded
            for index, fold in enumerate(folds):
                title, body = fold.query(Static)
                assert body.display is expanded
                assert str(title.render()).strip() == f"═══ {arrow} Compressed ═══"
                assert (body in app.screen._compositor.visible_widgets) is expanded
                rows = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                assert (f"Summary {index}" in rows) is expanded
            assert app.screen.focused is composer


async def test_fold_closes_the_only_summary_opened_by_click() -> None:
    async with ChatPanelApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_context_fold("first", "First summary")
        await panel.add_context_fold("second", "Second summary")
        first, second = panel.query(ContextFoldWidget)
        await click_when_settled(pilot, first.query(Static).first())
        assert first.query_one("#summary").display and not second.query_one("#summary").display

        assert panel.toggle_fold_all() is True
        for fold in (first, second):
            assert not fold.query_one("#summary").display
            assert fold.title_text == "▸ Compressed"


@pytest.mark.parametrize("summary", ["", " \n "], ids=["empty", "blank"])
async def test_fold_ignores_markers_without_summaries(summary: str) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        assert panel.toggle_fold_all() is False
        await panel.add_context_fold("empty", summary)
        empty = panel.query_one(ContextFoldWidget)
        for _ in (0, 1):
            assert panel.toggle_fold_all() is False
            assert empty.title_text == "Compressed" and not empty.query_one("#summary").display

        await panel.add_context_fold("readable", "Readable summary")
        readable = panel.query(ContextFoldWidget).last()
        assert panel.toggle_fold_all() is False
        assert readable.query_one("#summary").display
        assert panel.toggle_fold_all() is True
        assert not readable.query_one("#summary").display
        assert empty.title_text == "Compressed" and not empty.query_one("#summary").display


async def test_keyboard_fold_keeps_the_view_when_a_long_summary_opens(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        panel = app.screen.query_one(ChatPanel)
        await panel.add_system("Earlier\n" * 60)
        await panel.add_context_fold("ctx", "Summary\n" * 95)
        fold = panel.query_one(ContextFoldWidget)
        composer = app.screen.query_one(InputBar).query_one(TextArea)
        composer.focus()
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot, description="history laid out")
        panel.pause_following()
        # The marker starts outside the view, where a click's reveal would scroll to it.
        panel.scroll_to(y=10, animate=False, immediate=True)
        await pilot.pause()
        before = panel.scroll_y
        assert before > 0 and fold.region.y >= panel.content_region.bottom

        for expanded in (True, False):
            await pilot.press("slash", "f", "o", "l", "d", "enter")
            await wait_for(
                lambda: screen_is_settled(app, app.screen), pilot=pilot, description="keyboard fold laid out"
            )
            assert fold.query_one("#summary").display is expanded
            assert panel.scroll_y == before
            assert app.screen.focused is composer


async def test_keyboard_fold_shrinks_the_anchored_view_in_its_first_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    app = ChatPanelApp()
    async with app.run_test(size=(100, 32)) as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_system("\n".join(f"Earlier {index}" for index in range(40)))
        await panel.add_context_fold("ctx", "\n".join(f"Summary {index}" for index in range(80)))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot, description="history laid out")
        panel.jump_to_bottom()
        assert panel.toggle_fold_all() is False
        await wait_for(
            lambda: screen_is_settled(app, app.screen) and panel.scroll_y == panel.max_scroll_y,
            pilot=pilot,
            description="expanded summary and bottom anchor laid out",
        )

        frames: list[bool] = []
        display = app._display

        def capture_frame(screen: Screen, renderable: RenderableType | None) -> None:
            if renderable is not None:
                region = panel.content_region
                rows = screen._compositor.render_strips()[region.y : region.bottom]
                frames.append(any("Compressed" in strip.text for strip in rows))
            display(screen, renderable)

        monkeypatch.setattr(app, "_display", capture_frame)
        assert panel.toggle_fold_all() is True
        await wait_for(
            lambda: bool(frames) and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="the folded marker was painted",
        )
        assert all(frames)


@pytest.mark.parametrize("earlier_lines", [20, 60], ids=["short-history", "scrolling-history"])
@pytest.mark.parametrize("running", [False, True], ids=["idle", "running"])
async def test_opening_a_long_summary_at_the_bottom_keeps_its_title_and_first_line_in_view(
    earlier_lines: int, running: bool
) -> None:
    async with ChatPanelApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Earlier conversation")
        await panel.add_system("\n".join(f"Earlier line {index}" for index in range(earlier_lines)))
        await panel.add_context_fold("ctx", "\n".join(f"Summary line {index}" for index in range(95)))
        panel.agent_running = running
        title, body = panel.query_one(ContextFoldWidget).query(Static)
        await wait_for(
            lambda: screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot, description="the marker is laid out"
        )
        panel.jump_to_bottom()
        await pilot.pause()
        assert panel.scroll_y == panel.max_scroll_y
        assert panel.is_anchored and not panel.is_anchor_released()
        assert panel.content_region.bottom - 5 <= title.region.y < panel.content_region.bottom
        before = panel.scroll_y
        # Leave room for the bottom-jump button that appears over the last row.
        reveal_rows = max(0, title.region.y + 3 - panel.content_region.bottom)

        await click_when_settled(pilot, title)
        await wait_for(
            lambda: body.display and screen_is_settled(pilot.app, pilot.app.screen),
            pilot=pilot,
            description="the expanded summary is laid out",
        )
        assert body.content_region.height > panel.content_region.height
        assert panel.scroll_y == before + reveal_rows
        assert panel.content_region.y <= title.region.y < panel.content_region.bottom
        assert panel.content_region.y <= body.content_region.y < panel.content_region.bottom
        rows = "\n".join(strip.text for strip in pilot.app.screen._compositor.render_strips())
        assert "Compressed" in rows
        assert "Summary line 0" in rows


async def _drag(pilot: Pilot, start: tuple[int, int], end: tuple[int, int]) -> None:
    # Go through the app so releasing on the same widget also produces a Click.
    for event, (x, y) in ((MouseDown, start), (MouseMove, end), (MouseUp, end)):
        pilot.app.post_message(event(None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y))
        await pilot.pause()


async def test_dragging_across_the_title_does_not_toggle_it() -> None:
    async with ChatPanelApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_context_fold("ctx", _SUMMARY)
        title, body = panel.query_one(ContextFoldWidget).query(Static)
        await wait_for(lambda: title.region.width > 0, pilot=pilot, description="the compression title is laid out")

        x, y = title.region.offset
        await _drag(pilot, (x, y), (x + 10, y))
        assert panel.query_one(ContextFoldWidget).title_text == "▸ Compressed"
        assert not body.display

        await click_when_settled(pilot, title)
        assert body.display
        await _drag(pilot, (x, y), (x + 10, y))
        assert panel.query_one(ContextFoldWidget).title_text == "▾ Compressed"
        assert body.display


@pytest.mark.parametrize("hovered", [False, True], ids=["untracked-pointer", "hovered-pointer"])
@pytest.mark.parametrize("vertical", [False, True], ids=["along-title", "off-title"])
async def test_dragging_out_and_back_to_the_same_title_cell_does_not_toggle_it(
    tmp_path: Path, hovered: bool, vertical: bool
) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        panel = app.screen.query_one(ChatPanel)
        await panel.add_system("Previous output")
        await panel.add_context_fold("ctx", _SUMMARY)
        title, body = panel.query_one(ContextFoldWidget).query(Static)
        await wait_for(lambda: title.region.width > 0, pilot=pilot, description="the compression title is laid out")
        x, y = title.region.offset
        outside = (x, y - 2) if vertical else (x + 12, y)
        if hovered:
            await pilot.hover(offset=(x, y))
        for event, (column, row) in ((MouseDown, (x, y)), (MouseMove, outside), (MouseMove, (x, y)), (MouseUp, (x, y))):
            pilot.app.post_message(
                event(None, column, row, 0, 0, 1, False, False, False, screen_x=column, screen_y=row)
            )
            await pilot.pause()
        assert not body.display
        await click_when_settled(pilot, title)
        assert body.display, "a fresh click still opens the summary"


async def test_a_small_mouse_movement_inside_copy_still_copies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_chrys_app(tmp_path)
    copied: list[str] = []

    def capture_response(message: AgentMessage) -> None:
        copied.append(message.text)

    monkeypatch.setattr(AgentMessage, "copy_agent_response", capture_response)
    async with app.run_test(size=(100, 32)) as pilot:
        panel = app.screen.query_one(ChatPanel)
        await panel.add_user_message("Question")
        await panel.add_agent_message("Answer")
        button = panel.query_one(AgentMessage).query_one(MessageCopyButton)
        await wait_for(
            lambda: screen_is_settled(app, app.screen) and button.region.width >= 2,
            pilot=pilot,
            description="copy button laid out",
        )
        x, y = button.region.offset
        await _drag(pilot, (x, y), (x + 1, y))
        assert copied == ["Answer"]


@pytest.mark.parametrize("summary_lines", [1, 95], ids=["short-summary", "long-summary"])
async def test_opening_a_summary_in_the_middle_keeps_the_reading_position(summary_lines: int) -> None:
    async with ChatPanelApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_system("\n".join(["Earlier"] * 60))
        await panel.add_context_fold("ctx", "\n".join(f"Summary line {index}" for index in range(summary_lines)))
        await panel.add_system("\n".join(["Later"] * 60))
        fold = panel.query_one(ContextFoldWidget)
        title, body = fold.query(Static)
        await wait_for(
            lambda: screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot, description="history laid out"
        )
        panel.pause_following()
        panel.scroll_to(y=fold.virtual_region.y - 15, animate=False, immediate=True)
        await pilot.pause()
        before_scroll, before_row = panel.scroll_y, title.region.y
        assert panel.content_region.y < before_row < panel.content_region.bottom - 2

        await click_when_settled(pilot, title)
        await wait_for(
            lambda: screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot, description="summary laid out"
        )
        assert body.display
        assert panel.scroll_y == before_scroll
        assert title.region.y == before_row
        assert panel.content_region.y <= body.content_region.y < panel.content_region.bottom


@pytest.mark.parametrize("column", [0, 2], ids=["left-edge", "first-character"])
async def test_clicking_the_open_summary_leaves_it_open(column: int) -> None:
    async with ChatPanelApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_context_fold("ctx", _SUMMARY)
        title, body = panel.query_one(ContextFoldWidget).query(Static)

        await click_when_settled(pilot, title)
        await click_when_settled(pilot, body, offset=(column, 1))
        assert panel.query_one(ContextFoldWidget).title_text == "▾ Compressed"
        assert body.display


async def test_title_and_summary_are_centered() -> None:
    async with ChatPanelApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "read_file", "filesystem_read", "a.py")
        await panel.add_tool_result("c1", "read_file", "ok", 10)
        await panel.add_context_fold("ctx", _SUMMARY, turn_range=(3, 3))
        process_row = panel.query_one(LiveProcessRow)
        title, body = panel.query_one(ContextFoldWidget).query(Static)
        await wait_for(lambda: title.region.width > 0, pilot=pilot, description="the compression title is laid out")

        # The marker spans the transcript like the rows above it and draws its label in the middle.
        assert title.content_region.x == process_row.content_region.x
        row = title.render_line(0).text
        assert row.strip() == "═══ ▸ Compressed (Turn 3) ═══"
        assert abs(len(row) - len(row.rstrip()) - (len(row) - len(row.lstrip()))) <= 1
        await click_when_settled(pilot, title)
        # The opened summary reads as quiet background: centered, dim and italic like the always-shown summary before.
        for line in (0, 1):
            text = body.render_line(line).text
            assert text.strip() == _SUMMARY.splitlines()[line]
            left, right = len(text) - len(text.lstrip()), len(text) - len(text.rstrip())
            assert left > 10 and abs(left - right) <= 1
        segments = [segment for segment in body.render_line(0) if segment.text.strip()]
        assert segments and all(segment.style and segment.style.dim and segment.style.italic for segment in segments)


@pytest.mark.parametrize("summary", ["", " \n "], ids=["empty", "blank"])
async def test_marker_without_a_summary_has_no_arrow_and_ignores_clicks(summary: str) -> None:
    clicks: list[Click] = []

    class ObserveClicksApp(ChatPanelApp):
        def on_click(self, event: Click) -> None:
            clicks.append(event)

    async with ObserveClicksApp().run_test(size=(100, 32)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_context_fold("ctx", summary, 5, (3, 3))
        fold = panel.query_one(ContextFoldWidget)
        title, body = fold.query(Static)

        assert fold.title_text == "Compressed (Turn 3)"
        await click_when_settled(pilot, title)
        assert fold.title_text == "Compressed (Turn 3)"
        assert not body.display
        assert fold.region.height == 1
        assert any(event.screen_offset in title.region for event in clicks), "the plain marker lets clicks bubble"
        assert title.styles.pointer != "pointer"


async def test_drag_across_summaries_copies_raw_bodies_without_headers(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        panel = app.screen.query_one(ChatPanel)
        await panel.add_context_fold("first", _SUMMARY, turn_range=(1, 1))
        await panel.add_context_fold("middle", "hidden summary", turn_range=(2, 2))
        await panel.add_context_fold("last", "Last summary", turn_range=(3, 3))
        first, middle, last = panel.query(ContextFoldWidget)
        first_title, first_body = first.query(Static)
        last_title, last_body = last.query(Static)
        await click_when_settled(pilot, first_title)
        # Opening an already visible summary need not bring the later marker into view.
        last_title.scroll_visible(animate=False, immediate=True)
        await click_when_settled(pilot, last_title)
        await wait_for(lambda: last_body.region.height > 0, pilot=pilot, description="both summaries are laid out")

        assert last.region.y == middle.region.bottom + 1
        # Summaries are centered: drag from the left edge of the first summary row to the end of the last one.
        start = first_body.region.x, first_body.content_region.y
        end = last_body.region.right - 1, last_body.content_region.y
        await _drag(pilot, start, end)
        assert app.screen.get_selected_text() == f"{_SUMMARY}\nLast summary"
