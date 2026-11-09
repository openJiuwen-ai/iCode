# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the sidebar TasksPanel todo checklist rendering."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.widgets import Static

from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.tasks import TasksPanel, TodoListState
from chrys.foundation.models.todos import TodoItem
from tests.support.waiting import wait_for


class TasksPanelApp(App):
    def compose(self) -> ComposeResult:
        yield TasksPanel()


class PreloadedTasksPanelApp(App):
    def compose(self) -> ComposeResult:
        panel = TasksPanel()
        panel.todo_state = TodoListState(items=(TodoItem(content="preloaded", status="completed"),))
        yield panel


class SidebarPanelApp(App):
    def compose(self) -> ComposeResult:
        yield SidebarPanel()


def _checklist(app: App) -> Static:
    return app.query_one("#tasks-checklist", Static)


def _line_texts(app: App) -> list[str]:
    """Checklist lines as rendered at the widget's real on-screen width."""
    static = _checklist(app)
    width, height = static.size
    strips = static.render_lines(Region(0, 0, width, height))
    return [strip.text.rstrip() for strip in strips if strip.text.strip()]


def _segment_styles(app: App, line: int) -> list[tuple[str, Style]]:
    """(text, style) pairs for one checklist line, ignoring pad whitespace."""
    static = _checklist(app)
    # Textual's run_test stdout proxy reports itself as a terminal; force
    # plain rendering so segment styles stay symbolic (no ANSI downgrade).
    console = Console(width=static.size.width or 40, force_terminal=False, _environ={})
    lines = list(Segment.split_lines(console.render(static.content)))
    return [(segment.text.rstrip(), segment.style) for segment in lines[line] if segment.text.strip()]


def _label(panel: TasksPanel) -> str | None:
    """The todo box's border title."""
    return panel.query_one("#tasks-todos").border_title


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["textual-dark", "ansi-dark", "ansi-light"])
async def test_tasks_panel_renders_status_markers_and_header_count(theme: str) -> None:
    async with TasksPanelApp().run_test(size=(60, 20)) as pilot:
        pilot.app.theme = theme
        await pilot.pause()
        panel = pilot.app.query_one(TasksPanel)
        panel.todo_state = TodoListState(
            items=(
                TodoItem(content="write tests", status="completed"),
                TodoItem(content="run suite", status="in_progress", active_form="Running suite"),
                TodoItem(content="ship it"),
            )
        )
        await pilot.pause()

        assert _line_texts(pilot.app) == ["✓ write tests", "▸ Running suite", "☐ ship it"]
        assert _segment_styles(pilot.app, 0) == [
            ("✓", Style.parse("green")),
            ("write tests", Style.parse("dim strike")),
        ]
        warning = pilot.app.theme_variables["warning"]
        expected_style = rich_style_from_textual_color(warning, bold=True)
        assert all(style == expected_style for _text, style in _segment_styles(pilot.app, 1))

        assert _label(panel) == "Todos (1/3)"
        assert pilot.app.query_one("#tasks-list", VerticalScroll).display
        assert not pilot.app.query_one("#tasks-empty", Static).display


@pytest.mark.asyncio
async def test_tasks_panel_treats_model_markup_as_literal_text() -> None:
    """Model-authored todo content full of Textual markup must render literally.

    ``_line_texts`` renders through Textual's real strip pipeline, so a
    regression from Rich ``Text`` back to raw strings raises ``MarkupError``
    here rather than crashing a live frame.
    """
    async with TasksPanelApp().run_test(size=(60, 20)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        panel.todo_state = TodoListState(
            items=(
                TodoItem(content="[bold] unclosed tag"),
                TodoItem(content="stray close [/]", status="in_progress", active_form="closing [/] now"),
                TodoItem(content="mismatched [red]x[/blue]", status="completed"),
                TodoItem(content="array a[5] and [@click=app.quit]boom[/]"),
            )
        )
        await pilot.pause()

        assert _line_texts(pilot.app) == [
            "☐ [bold] unclosed tag",
            "▸ closing [/] now",
            "✓ mismatched [red]x[/blue]",
            "☐ array a[5] and [@click=app.quit]boom[/]",
        ]


@pytest.mark.asyncio
async def test_tasks_panel_wraps_long_items_at_real_width_with_hanging_indent() -> None:
    long_item = (
        "Read orchestration: engine/run/* remaining modules (compaction, finalizer, "
        "turn_state, retry, active_injection, prompt_hooks, turn_hooks)"
    )
    async with TasksPanelApp().run_test(size=(46, 24)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        panel.todo_state = TodoListState(items=(TodoItem(content=long_item),))
        await pilot.pause()

        lines = _line_texts(pilot.app)
        assert len(lines) > 1
        assert lines[0].startswith("☐ Read orchestration:")
        # Continuation lines hang under the first content character, not the marker.
        for continuation in lines[1:]:
            assert continuation.startswith("  ")
            assert continuation[2] != " "
        # Wrapping happens at the real widget width (RichLog's min_width=78
        # used to wrap at 78 and let overflow cropping eat characters).
        static = _checklist(pilot.app)
        assert static.size.width < 78
        rebuilt = " ".join(line[2:].strip() for line in lines)
        assert rebuilt == long_item


@pytest.mark.asyncio
async def test_tasks_panel_applies_state_set_before_children_mount() -> None:
    async with PreloadedTasksPanelApp().run_test(size=(60, 20)) as pilot:
        # The preloaded state lands via on_mount -> call_after_refresh, then
        # needs a layout for the checklist to gain size: poll instead of
        # assuming one pause covers the whole chain.
        await wait_for(
            lambda: _line_texts(pilot.app) == ["✓ preloaded"],
            pilot=pilot,
            description="preloaded task renders as completed",
        )

        assert _line_texts(pilot.app) == ["✓ preloaded"]
        assert _label(pilot.app.query_one(TasksPanel)) == "Todos (1/1)"


@pytest.mark.asyncio
async def test_tasks_panel_in_progress_falls_back_to_content_without_active_form() -> None:
    async with TasksPanelApp().run_test(size=(60, 20)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        panel.todo_state = TodoListState(items=(TodoItem(content="raw content", status="in_progress"),))
        await pilot.pause()

        assert _line_texts(pilot.app) == ["▸ raw content"]
        assert _label(panel) == "Todos (0/1)"


@pytest.mark.asyncio
async def test_tasks_panel_empty_state_clears_previous_items() -> None:
    async with TasksPanelApp().run_test(size=(60, 20)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        panel.todo_state = TodoListState(items=(TodoItem(content="stale", status="completed"),))
        await pilot.pause()
        panel.todo_state = TodoListState()
        await pilot.pause()

        assert _checklist(pilot.app).content == ""
        assert not pilot.app.query_one("#tasks-list", VerticalScroll).display
        empty = pilot.app.query_one("#tasks-empty", Static)
        assert empty.display
        assert str(empty.render()) == "No todos yet"
        assert _label(panel) == "Todos"


@pytest.mark.asyncio
async def test_tasks_panel_initial_state_shows_centered_empty_placeholder() -> None:
    async with TasksPanelApp().run_test(size=(60, 20)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        assert _label(panel) == "Todos"
        assert not pilot.app.query_one("#tasks-list", VerticalScroll).display
        empty = pilot.app.query_one("#tasks-empty", Static)
        assert empty.display
        # Placeholder is centered inside its box, not stuck to the top-left.
        assert empty.styles.content_align == ("center", "middle")
        assert empty.region.height > 3


@pytest.mark.asyncio
async def test_tasks_panel_none_state_is_ignored() -> None:
    async with TasksPanelApp().run_test(size=(60, 20)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        panel.todo_state = TodoListState(items=(TodoItem(content="keep me"),))
        await pilot.pause()
        panel.todo_state = None
        await pilot.pause()

        assert _line_texts(pilot.app) == ["☐ keep me"]
        assert _label(panel) == "Todos (0/1)"


@pytest.mark.asyncio
async def test_sidebar_panel_routes_todo_state_to_tasks_tab() -> None:
    async with SidebarPanelApp().run_test(size=(120, 40)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        assert [pane.id for pane in sidebar.query("TabPane")] == [
            "tab-toc",
            "tab-tasks",
            "tab-context",
            "tab-hooks",
            "tab-debug",
            "tab-buddy",
        ]

        sidebar.focus_tab("tab-tasks")
        await pilot.pause()
        sidebar.todo_state = TodoListState(items=(TodoItem(content="routed", status="in_progress"),))
        await pilot.pause()

        assert _line_texts(pilot.app) == ["▸ routed"]
        assert _label(sidebar.tasks_panel) == "Todos (0/1)"
