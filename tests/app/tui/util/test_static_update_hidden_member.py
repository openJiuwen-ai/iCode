# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Content that changes while a process hides it lays nothing out, and shows at its new size when revealed.

A widget laid out once and then hidden with the process entry that holds it (``set_process_hidden``
or ``set_content_visible``) keeps changing: a running command's output grows. Each change used to
lay the screen out for a widget nobody can see. Now only the content changes while it is hidden,
and the entry measures it again in the same layout that reveals it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from chrys.app.tui.util.static_update import update_static_in_place
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_visibility import ProcessMember
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.tool_kinds import KIND_SHELL
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.asyncio


class _Entry(ProcessMember):
    """A transcript entry a process can hide, holding a running command's output."""

    DEFAULT_CSS = """
    _Entry { height: auto; }
    _Entry > Vertical { height: auto; }
    #out { height: auto; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("one", id="out")


class _EntryApp(App[None]):
    def compose(self) -> ComposeResult:
        yield _Entry()
        yield Static("ROW-BELOW", id="below")


def _hide_by_process(entry: _Entry, hidden: bool) -> None:
    entry.set_process_hidden(hidden)


def _hide_by_content(entry: _Entry, hidden: bool) -> None:
    entry.set_content_visible(not hidden)


_HIDERS = pytest.mark.parametrize("hide", [_hide_by_process, _hide_by_content], ids=["process", "content"])


def _frame_lines(app: App[None]) -> list[str]:
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


async def _settle(app: App[None]) -> None:
    await wait_for(lambda: screen_is_settled(app, app.screen), description="screen settled")


def _spy_layouts(static: Static, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    layouts: list[bool] = []
    repaint = static.refresh

    def recording(*args: Any, **kwargs: Any) -> Any:
        layouts.append(bool(kwargs.get("layout", False)))
        return repaint(*args, **kwargs)

    monkeypatch.setattr(static, "refresh", recording)
    return layouts


def _spy_screen_layout(app: App[None], monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    layout = app.screen._refresh_layout

    def recording(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return layout(*args, **kwargs)

    monkeypatch.setattr(app.screen, "_refresh_layout", recording)
    return calls


@_HIDERS
@pytest.mark.parametrize(
    ("initial", "updates", "expected"),
    [
        pytest.param("one", ["one\ntwo", "one\ntwo\nthree"], ["one", "two", "three"], id="growing"),
        pytest.param("one\ntwo\nthree", ["only"], ["only"], id="shrinking"),
    ],
)
async def test_output_resizes_while_hidden_without_laying_out_until_reveal(
    hide: Callable[[_Entry, bool], None],
    initial: str,
    updates: list[str],
    expected: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _EntryApp()
    async with app.run_test(size=(60, 12)):
        entry = app.query_one(_Entry)
        out = app.query_one("#out", Static)
        update_static_in_place(out, initial)
        await _settle(app)
        assert out.outer_size.height == len(initial.splitlines())
        hide(entry, True)
        await _settle(app)
        assert not entry.display

        layouts = _spy_layouts(out, monkeypatch)
        screen_layouts = _spy_screen_layout(app, monkeypatch)
        for text in updates:
            update_static_in_place(out, text)
        await _settle(app)
        assert not any(layouts), "hidden content asked for a layout"
        assert not screen_layouts, "hidden content laid the screen out"

        hide(entry, False)
        await _settle(app)
        assert out.outer_size.height == len(expected)
        assert _frame_lines(app)[: len(expected) + 1] == [*expected, "ROW-BELOW"]


async def test_hidden_by_both_it_waits_for_the_last_one_to_reveal_it(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _EntryApp()
    async with app.run_test(size=(60, 12)):
        entry = app.query_one(_Entry)
        out = app.query_one("#out", Static)
        await _settle(app)
        entry.set_process_hidden(True)
        entry.set_content_visible(False)
        await _settle(app)

        layouts = _spy_layouts(out, monkeypatch)
        update_static_in_place(out, "one\ntwo")
        await _settle(app)
        assert not any(layouts)
        entry.set_process_hidden(False)
        await _settle(app)
        assert not entry.display

        entry.set_content_visible(True)
        await _settle(app)
        assert out.outer_size.height == 2
        assert _frame_lines(app)[:3] == ["one", "two", "ROW-BELOW"]


async def test_a_shown_widget_still_lays_out_when_its_content_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _EntryApp()
    async with app.run_test(size=(60, 12)):
        out = app.query_one("#out", Static)
        await _settle(app)
        layouts = _spy_layouts(out, monkeypatch)
        update_static_in_place(out, "one\ntwo")
        await _settle(app)
        assert any(layouts)
        assert out.outer_size.height == 2


@pytest.mark.parametrize("status", ["completed", "failed", "interrupted"])
async def test_hidden_shell_terminal_result_measures_in_its_first_visible_frame(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = ChatPanelApp()
    async with app.run_test(size=(100, 35)) as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("first", "shell", KIND_SHELL, args={"command": "first"})
        await panel.add_tool_start("parallel", "shell", KIND_SHELL, args={"command": "parallel"})
        row = panel.query_one(LiveProcessRow)
        group = panel.query_one(ToolGroup)
        first = group.get_tool("first")
        assert first is not None
        tail = first.query_one("#exec-panel", Static)
        await click_when_settled(pilot, row)
        group.collapsed = False
        await _settle(app)
        initial_height = tail.outer_size.height
        assert initial_height > 0
        await click_when_settled(pilot, row)
        await _settle(app)
        assert group.process_hidden and group.collapsed

        layouts = _spy_layouts(tail, monkeypatch)
        frames: list[tuple[int, list[str]]] = []
        refresh_layout = app.screen._refresh_layout
        screen_layouts: list[object] = []

        def record_layout(*args: Any, **kwargs: Any) -> None:
            screen_layouts.append(args)
            refresh_layout(*args, **kwargs)
            if group.display and not group.collapsed:
                frames.append((tail.outer_size.height, _frame_lines(app)))

        monkeypatch.setattr(app.screen, "_refresh_layout", record_layout)
        result_lines = ["one", "two", "three", "four"]
        if status != "completed":
            result_lines[0] = "Error: one"
        await panel.add_tool_result("first", "shell", "\n".join(result_lines), canonical_status=status)
        await _settle(app)
        assert panel.is_tool_running("parallel")
        assert not any(layouts)
        assert not screen_layouts

        await click_when_settled(pilot, row)
        group.collapsed = False
        await _settle(app)
        assert frames
        first_height, lines = frames[0]
        assert first_height == len(result_lines) + tail.styles.gutter.height > initial_height
        assert all(any(text in line for line in lines) for text in result_lines)
