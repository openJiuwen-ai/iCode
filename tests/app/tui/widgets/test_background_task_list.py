# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The background commands below the todos in the sidebar Tasks tab."""

from __future__ import annotations

import time

import pytest
from rich.cells import cell_len
from rich.color import Color
from rich.style import Style
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.pilot import Pilot
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.widgets.sidebar.background_tasks import BackgroundTaskList
from chrys.app.tui.widgets.sidebar.tasks import TasksPanel, TodoListState, section_heights
from chrys.foundation.models.background_tasks import BackgroundTaskView
from chrys.foundation.models.todos import TodoItem
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for


def _task(task_id: str, command: str = "npm run dev", **fields: object) -> BackgroundTaskView:
    base: dict[str, object] = {
        "id": task_id,
        "command": command,
        "reason": "",
        "cwd": "/repo",
        "trigger": "timeout",
        "turn": 1,
        "pids": (4242,),
        "log_path": "/logs/x.log",
        "started_at": time.time() - 5,
    }
    base.update(fields)
    return BackgroundTaskView(**base)  # type: ignore[arg-type]


def _ended(task_id: str, command: str = "make build", exit_code: int = 0) -> BackgroundTaskView:
    return _task(task_id, command, ended_at=time.time(), exit_code=exit_code)


class _TasksApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.opened: list[str] = []

    def compose(self) -> ComposeResult:
        yield TasksPanel()

    def on_background_task_list_opened(self, event: BackgroundTaskList.Opened) -> None:
        self.opened.append(event.task_id)


def _rows(app: App) -> list[str]:
    rows = app.query_one("#background-tasks-rows", Static)
    width, height = rows.size
    return [strip.text.rstrip() for strip in rows.render_lines(Region(0, 0, width, height)) if strip.text.strip()]


def _title(app: App) -> str | None:
    return app.query_one(BackgroundTaskList).border_title


def _shown(app: App, selector: str) -> bool:
    return app.query_one(selector).display


def _border(widget: Widget, *, bottom: bool = False) -> Strip:
    width, height = widget.outer_size
    return widget.render_lines(Region(0, height - 1 if bottom else 0, width, 1))[0]


def _link(app: App) -> range:
    """The columns of the Clear ended link on the list's bottom border, as drawn: underlined."""
    columns: list[int] = []
    x = 0
    for segment in _border(app.query_one(BackgroundTaskList), bottom=True):
        width = cell_len(segment.text)
        if segment.style is not None and segment.style.underline:
            columns.extend(range(x, x + width))
        x += width
    return range(columns[0], columns[-1] + 1) if columns else range(0)


def _clear_shown(app: App) -> bool:
    return bool(_link(app))


async def _click_clear(pilot: Pilot) -> None:
    background = pilot.app.query_one(BackgroundTaskList)
    await click_when_settled(pilot, background, offset=(_link(pilot.app).start, background.outer_size.height - 1))


def _todo_title(app: App) -> str | None:
    return app.query_one("#tasks-todos").border_title


def _heights(app: App) -> tuple[int, int]:
    return app.query_one("#tasks-todos").region.height, app.query_one(BackgroundTaskList).region.height


def _cells(app: App, row: int) -> list[tuple[str, Style]]:
    rows = app.query_one("#background-tasks-rows", Static)
    strip = rows.render_lines(Region(0, row, rows.size.width, 1))[0]
    return [(segment.text.strip(), segment.style or Style()) for segment in strip if segment.text.strip()]


def _background(app: App, row: int) -> Color | None:
    """The background behind a row's command."""
    return _cells(app, row)[1][1].bgcolor


@pytest.mark.asyncio
async def test_background_commands_show_in_their_own_half_below_the_todos() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        await wait_for(lambda: _heights(pilot.app) == (12, 12), pilot=pilot, description="two even halves")
        assert _shown(pilot.app, "#tasks-empty")
        assert _title(pilot.app) == "Background Tasks"
        assert str(pilot.app.query_one("#background-tasks-empty", Static).render()) == "No background tasks"
        assert not _shown(pilot.app, "#background-tasks-box")

        panel.background_tasks = (_task("a", "python -m http.server [bold]8000[/]"), _ended("b"))
        await wait_for(lambda: len(_rows(pilot.app)) == 2, pilot=pilot, description="two background rows")

        assert not _shown(pilot.app, "#background-tasks-empty")
        assert _shown(pilot.app, "#tasks-empty")
        assert _title(pilot.app) == "Background Tasks (1)"
        running, ended = _rows(pilot.app)
        assert running.startswith("● python -m http.server [bold]8000[/]")
        assert running.endswith("running")
        assert ended.startswith("✓ make build")
        assert ended.endswith("exit 0")
        assert _clear_shown(pilot.app)
        assert _heights(pilot.app) == (12, 12)

        panel.todo_state = TodoListState(items=(TodoItem(content="ship it"),))
        await wait_for(lambda: _shown(pilot.app, "#tasks-list"), pilot=pilot, description="todos shown")
        assert not _shown(pilot.app, "#tasks-empty")

        panel.background_tasks = ()
        await wait_for(
            lambda: _shown(pilot.app, "#background-tasks-empty"), pilot=pilot, description="empty state back"
        )
        assert not _shown(pilot.app, "#background-tasks-box")


@pytest.mark.asyncio
@pytest.mark.parametrize(("shell_exit_code", "outcome"), [(None, "running"), (0, "exit 0"), (3, "exit 3")])
async def test_a_running_command_shows_its_time_and_outcome_in_the_running_color(shell_exit_code, outcome) -> None:
    async with _TasksApp().run_test(size=(40, 24)) as pilot:
        pilot.app.query_one(TasksPanel).background_tasks = (_task("a", shell_exit_code=shell_exit_code), _ended("b"))
        await wait_for(lambda: len(_rows(pilot.app)) == 2, pilot=pilot, description="two background rows")

        icon, _command, duration, status = _cells(pilot.app, 0)
        assert (icon[0], status[0]) == ("●", outcome)
        assert "pid" not in _rows(pilot.app)[0]
        assert "shell" not in _rows(pilot.app)[0]
        assert icon[1].color is not None
        assert duration[1].color == status[1].color == icon[1].color
        # An ended one's are dimmed, as Textual paints dim: a faded color.
        _icon, command, ended_duration, exit_code = _cells(pilot.app, 1)
        assert exit_code[0] == "exit 0"
        assert ended_duration[1].color == exit_code[1].color
        assert exit_code[1].color not in (icon[1].color, command[1].color)


@pytest.mark.asyncio
async def test_the_title_and_clear_ended_show_whole_on_the_borders_of_the_narrowest_sidebar() -> None:
    # 40 columns: what the sidebar leaves inside its border at its minimum width.
    async with _TasksApp().run_test(size=(40, 24)) as pilot:
        background = pilot.app.query_one(BackgroundTaskList)
        pilot.app.query_one(TasksPanel).background_tasks = (*(_task(str(n)) for n in range(32)), _ended("e"))
        await wait_for(lambda: _clear_shown(pilot.app), pilot=pilot, description="clear ended shown")

        assert _border(background).text.startswith("╭─ Background Tasks (32) ─")
        assert _border(background, bottom=True).text.endswith("─ Clear ended ─╯")


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 12])  # 12: too narrow for the whole link, which Textual cuts short
async def test_only_the_clear_ended_link_on_the_bottom_border_points_and_clears(width: int) -> None:
    async with _TasksApp().run_test(size=(width, 24)) as pilot:
        app = pilot.app
        background = app.query_one(BackgroundTaskList)
        app.query_one(TasksPanel).background_tasks = (_task("a"), _ended("b"))
        await wait_for(lambda: _clear_shown(app), pilot=pilot, description="the link shown")
        link, bottom = _link(app), background.outer_size.height - 1

        # The blank either side of the link counts as the link; the border past it does not.
        for inside, beside in ((link.start - 1, link.start - 2), (link.stop, link.stop + 1)):
            await pilot.hover(background, offset=(inside, bottom))
            await wait_for(lambda: background.styles.pointer == "pointer", pilot=pilot, description="it points")
            await pilot.hover(background, offset=(beside, bottom))
            await wait_for(lambda: background.styles.pointer == "default", pilot=pilot, description="beside it")
            await click_when_settled(pilot, background, offset=(beside, bottom))
            assert len(background.tasks) == 2
        # Off the border, into the rows or out of the list, it no longer points either.
        for elsewhere in ("#background-tasks-rows", "#tasks-todos"):
            await pilot.hover(background, offset=(link.start, bottom))
            await wait_for(lambda: background.styles.pointer == "pointer", pilot=pilot, description="it points")
            await pilot.hover(elsewhere, offset=(2, 1))
            await wait_for(lambda: background.styles.pointer == "default", pilot=pilot, description="moved off it")

        await click_when_settled(pilot, background, offset=(link.stop - 1, bottom))
        await wait_for(lambda: len(background.tasks) == 1, pilot=pilot, description="the ended row cleared")
        await wait_for(lambda: not _clear_shown(app), pilot=pilot, description="the link gone")

        # Gone while the rows keep their count, and with them the layout, it no longer points where it was.
        panel = app.query_one(TasksPanel)
        panel.background_tasks = (_task("a"), _ended("c"))
        await wait_for(lambda: _clear_shown(app), pilot=pilot, description="the link back")
        await pilot.hover(background, offset=(_link(app).start, bottom))
        await wait_for(lambda: background.styles.pointer == "pointer", pilot=pilot, description="it points")
        panel.background_tasks = (_task("a"), _task("d"))
        await wait_for(lambda: not _clear_shown(app), pilot=pilot, description="the link gone again")
        assert background.styles.pointer == "default"


@pytest.mark.parametrize(
    ("height", "todo_rows", "background_rows", "heights"),
    [
        (40, 1, 1, (None, None)),  # both empty: half each
        (40, 30, 30, (None, None)),  # both overflow their half
        (40, 18, 1, (None, None)),  # the todos still fit in their half
        (40, 19, 1, (None, 10)),  # the background shrinks, but not below a quarter
        (40, 19, 9, (None, 11)),  # ...or to what it shows
        (40, 30, 18, (None, 20)),
        (40, 30, 19, (None, None)),
        (40, 5, 30, (10, None)),
    ],
)
def test_a_section_shrinks_to_its_content_only_when_the_other_overflows(
    height: int, todo_rows: int, background_rows: int, heights: tuple[int | None, int | None]
) -> None:
    assert section_heights(height, todo_rows, background_rows) == heights


def _sized_todos(*lines: int) -> TodoListState:
    """Todos whose rows are exactly *lines* columns wide, marker included."""
    return TodoListState(
        items=tuple(TodoItem(content=f"{index:02d}" + "x" * (width - 4)) for index, width in enumerate(lines))
    )


@pytest.mark.asyncio
async def test_a_section_that_overflows_takes_what_the_other_does_not_show() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        # 58 columns fit the panel but not the 57 inside the box: each todo takes two rows.
        panel.todo_state = _sized_todos(*[58] * 6)
        await wait_for(lambda: _heights(pilot.app) == (18, 6), pilot=pilot, description="the commands keep a quarter")

        panel.background_tasks = tuple(_task(f"t{index}") for index in range(5))
        await wait_for(lambda: _heights(pilot.app) == (17, 7), pilot=pilot, description="the commands fit their rows")

        panel.background_tasks = tuple(_task(f"t{index}") for index in range(11))
        await wait_for(lambda: _heights(pilot.app) == (12, 12), pilot=pilot, description="both full: half each")

        panel.todo_state = TodoListState(items=())
        await wait_for(lambda: _heights(pilot.app) == (6, 18), pilot=pilot, description="the todos keep a quarter")


@pytest.mark.asyncio
async def test_todos_measure_at_the_width_their_scrollbar_leaves() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        panel.todo_state = _sized_todos(*[10] * 20)
        todo_list = pilot.app.query_one("#tasks-list", VerticalScroll)
        await wait_for(lambda: todo_list.scrollbar_gutter.width > 0, pilot=pilot, description="the todos scroll")

        # Ten rows fill the half exactly, but the last todo wraps beside the scrollbar it still has.
        panel.todo_state = _sized_todos(*[10] * 9, 57)
        await wait_for(lambda: _todo_title(pilot.app) == "Todos (0/10)", pilot=pilot, description="the new todos")
        background = pilot.app.query_one(BackgroundTaskList)
        assert str(background.styles.height) == "6"


@pytest.mark.asyncio
async def test_todos_that_arrive_while_the_tab_is_hidden_split_it_once_shown() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        await wait_for(lambda: _heights(pilot.app) == (12, 12), pilot=pilot, description="half each")
        panel.display = False
        await wait_for(lambda: not panel.region, pilot=pilot, description="hidden")
        panel.todo_state = _sized_todos(*[10] * 20)

        panel.display = True
        await wait_for(lambda: _heights(pilot.app) == (18, 6), pilot=pilot, description="split once shown")


@pytest.mark.asyncio
async def test_clear_ended_hides_those_commands_but_not_later_ones() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        panel.background_tasks = (_task("a"), _ended("b"))
        await wait_for(lambda: len(_rows(pilot.app)) == 2, pilot=pilot, description="two background rows")

        await _click_clear(pilot)
        await wait_for(lambda: len(_rows(pilot.app)) == 1, pilot=pilot, description="the ended row cleared")
        assert not _clear_shown(pilot.app)

        # The same ended command stays cleared; one that ends later shows.
        panel.background_tasks = (_ended("a", "npm run dev", exit_code=1), _ended("b"))
        await wait_for(
            lambda: _rows(pilot.app)[0].startswith("✗ npm run dev"), pilot=pilot, description="the new ended row"
        )
        assert len(_rows(pilot.app)) == 1

        # Nothing left to show: the box says so.
        await _click_clear(pilot)
        await wait_for(lambda: _shown(pilot.app, "#background-tasks-empty"), pilot=pilot, description="the empty state")


@pytest.mark.asyncio
async def test_clicking_a_row_opens_that_command() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        app = pilot.app
        assert isinstance(app, _TasksApp)
        app.query_one(TasksPanel).background_tasks = (_task("a"), _ended("b"))
        await wait_for(lambda: len(_rows(app)) == 2, pilot=pilot, description="two background rows")

        focused = app.focused
        await click_when_settled(pilot, "#background-tasks-rows", offset=(2, 1))
        await wait_for(lambda: app.opened == ["b"], pilot=pilot, description="the second row opened")
        # The keyboard stays where it was.
        assert app.focused is focused


@pytest.mark.asyncio
async def test_the_row_under_the_mouse_is_highlighted_until_it_moves_away() -> None:
    async with _TasksApp().run_test(size=(60, 16)) as pilot:
        app = pilot.app
        app.query_one(TasksPanel).background_tasks = tuple(_task(f"t{index}") for index in range(16))
        box = app.query_one("#background-tasks-box", VerticalScroll)
        await wait_for(lambda: box.max_scroll_y > 0, pilot=pilot, description="the list overflows")
        plain = _background(app, 1)
        hover = app.query_one(BackgroundTaskList).get_component_rich_style("background-tasks--hover").bgcolor
        assert hover != plain

        await pilot.hover("#background-tasks-rows", offset=(2, 7))
        await wait_for(lambda: _background(app, 7) == hover, pilot=pilot, description="the row highlighted")
        assert _background(app, 6) == _background(app, 8) == plain
        # Todos that overflow shrink the list, moving its rows under the mouse, which stays on them.
        app.query_one(TasksPanel).todo_state = _sized_todos(*[10] * 20)
        await wait_for(lambda: _heights(app) == (8, 8), pilot=pilot, description="the list shrunk")
        await wait_for(lambda: _background(app, 7) == plain, pilot=pilot, description="no highlight once moved")

        await pilot.hover("#background-tasks-rows", offset=(2, 1))
        await wait_for(lambda: _background(app, 1) == hover, pilot=pilot, description="the row highlighted again")
        # Scrolled, another row is under the mouse.
        box.scroll_end(animate=False)
        await wait_for(lambda: _background(app, 1) == plain, pilot=pilot, description="no highlight once scrolled")

        await pilot.hover("#background-tasks-rows", offset=(2, 15))
        await wait_for(lambda: _background(app, 15) == hover, pilot=pilot, description="the last row highlighted")
        await pilot.hover("#tasks-list")
        await wait_for(lambda: _background(app, 15) == plain, pilot=pilot, description="no highlight once left")


@pytest.mark.asyncio
async def test_the_highlight_goes_when_a_taller_tab_moves_the_rows_under_the_mouse() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        app = pilot.app
        panel = app.query_one(TasksPanel)
        panel.todo_state = _sized_todos(*[10] * 20)
        panel.background_tasks = (_task("a"), _task("b"))
        await wait_for(lambda: _heights(app) == (18, 6), pilot=pilot, description="the list fits its rows")
        plain = _background(app, 1)
        hover = app.query_one(BackgroundTaskList).get_component_rich_style("background-tasks--hover").bgcolor
        await pilot.hover("#background-tasks-rows", offset=(2, 1))
        await wait_for(lambda: _background(app, 1) == hover, pilot=pilot, description="the row highlighted")

        # The list keeps its size and moves down, its first row under the mouse.
        await resize_when_settled(pilot, 60, 25)
        await wait_for(lambda: _heights(app) == (19, 6), pilot=pilot, description="the todos took the new row")
        await wait_for(lambda: _background(app, 1) == plain, pilot=pilot, description="no highlight once moved")


@pytest.mark.asyncio
async def test_a_long_list_in_a_short_tab_scrolls_to_its_last_row() -> None:
    async with _TasksApp().run_test(size=(60, 16)) as pilot:
        app = pilot.app
        assert isinstance(app, _TasksApp)
        app.query_one(TasksPanel).background_tasks = tuple(_task(f"t{index}") for index in range(16))
        box = app.query_one("#background-tasks-box", VerticalScroll)
        await wait_for(lambda: box.max_scroll_y > 0, pilot=pilot, description="the list overflows")

        box.scroll_end(animate=False)
        # The rows scrolled up under the top border open nothing from there.
        await click_when_settled(pilot, app.query_one(BackgroundTaskList), offset=(4, 0))
        await click_when_settled(pilot, "#background-tasks-rows", offset=(2, 15))
        await wait_for(lambda: app.opened == ["t15"], pilot=pilot, description="the last row opened")


@pytest.mark.asyncio
async def test_duration_ticks_only_while_a_command_runs_and_the_list_is_shown() -> None:
    async with _TasksApp().run_test(size=(60, 24)) as pilot:
        panel = pilot.app.query_one(TasksPanel)
        background = pilot.app.query_one(BackgroundTaskList)
        panel.background_tasks = (_task("a"),)
        await wait_for(lambda: len(_rows(pilot.app)) == 1, pilot=pilot, description="one background row")
        timer = background._timer
        assert timer is not None
        await wait_for(timer._active.is_set, pilot=pilot, description="the timer runs")

        panel.display = False
        await wait_for(lambda: not timer._active.is_set(), pilot=pilot, description="hidden: the timer parks")

        panel.display = True
        await wait_for(timer._active.is_set, pilot=pilot, description="shown again: the timer runs")

        panel.background_tasks = (_ended("a"),)
        await wait_for(lambda: not timer._active.is_set(), pilot=pilot, description="ended: the timer parks")
