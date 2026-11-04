# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""BackgroundTaskList — the main agent's background shell commands, below the todos in the Tasks tab."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.chat.tool_call import fmt_duration
from chrys.app.tui.widgets.sidebar.empty_state import SidebarEmptyStateLabel
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Click, Leave, MouseEvent, MouseMove
    from textual.timer import Timer

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.models.background_tasks import BackgroundTaskView

_TITLE = msg("tui.sidebar.background.title", fallback="Background Tasks")
_TITLE_RUNNING = msg(
    "tui.sidebar.background.title_running",
    fallback="Background Tasks ({count})",
    plural_fallback="Background Tasks ({count})",
)
_EMPTY = msg("tui.sidebar.background.empty", fallback="No background tasks")
_CLEAR_ENDED = msg("tui.sidebar.background.clear_ended", fallback="Clear ended")
_RUNNING = msg("tui.sidebar.background.running", fallback="running")
_STOPPING = msg("tui.sidebar.background.stopping", fallback="stopping")
_EXIT = msg("tui.sidebar.background.exit", fallback="exit {code}")
_STOPPED = msg("tui.sidebar.background.stopped", fallback="stopped")
_ENDED = msg("tui.sidebar.background.ended", fallback="ended")

_ICON_STYLES: dict[str, str] = {"success": "green", "failure": "red", "stopped": "dim", "ended": "dim"}
_ICONS: dict[str, str] = {"running": "●", "success": "✓", "failure": "✗", "stopped": "■", "ended": "○"}


def task_state(task: BackgroundTaskView) -> str:
    """One of "running", "success", "failure", "stopped" or "ended" (it ended without an exit code)."""
    if task.running:
        return "running"
    if task.stopped_by is not None:
        return "stopped"
    if task.exit_code is None:
        return "ended"
    return "success" if task.exit_code == 0 else "failure"


def task_icon(task: BackgroundTaskView, *, running_style: Style) -> Text:
    """The state glyph of *task*, colored."""
    state = task_state(task)
    return Text(_ICONS[state], style=running_style if state == "running" else _ICON_STYLES[state])


def task_outcome(task: BackgroundTaskView) -> MessageRef:
    """The compact exit code or status shown beside the task's duration."""
    state = task_state(task)
    if state == "running":
        if task.stopped_by is not None:
            return _STOPPING.bind()
        if task.shell_exit_code is not None:
            return _EXIT.bind(code=str(task.shell_exit_code))
        return _RUNNING.bind()
    if state in ("success", "failure"):
        return _EXIT.bind(code=str(task.exit_code))
    return _STOPPED.bind() if state == "stopped" else _ENDED.bind()


def task_duration(task: BackgroundTaskView, now: float) -> str:
    """How long *task* has run in the background, up to now or its end."""
    end = task.ended_at if task.ended_at is not None else now
    return fmt_duration(max(0, int((end - task.started_at) * 1000)))


def one_line(text: str) -> str:
    """*text* on one line, safe to show."""
    return sanitize_legacy_scalar(" ".join(text.split()))


class BackgroundTaskList(Vertical, can_focus=False):
    """The background commands, one row each in a box titled with their count; a click on a row opens its details.

    A row's duration ticks once a second, only while a command runs and the list
    is composited. Same-sized repaints skip layout; a row added or removed costs one.
    The rows scroll inside their box; with none, the box says so. Once a command
    ended, the box's bottom border offers to clear the ended ones.
    """

    class Opened(Message):
        """The user clicked the row of a background command."""

        def __init__(self, task_id: str) -> None:
            super().__init__()
            self.task_id = task_id

    class ClearEnded(Message):
        """The user asked to hide the commands that ended."""

    tasks: reactive[tuple[BackgroundTaskView, ...]] = reactive((), init=False)
    COMPONENT_CLASSES: ClassVar[set[str]] = {"background-tasks--running", "background-tasks--hover"}

    DEFAULT_CSS = """
    BackgroundTaskList {
        height: 1fr;
        width: 100%;
        border: round $tui-border-primary-darken-3 $inner-border-opacity;
        border-title-color: $text-muted;
        border-title-style: bold;
        border-subtitle-align: right;
        border-subtitle-color: $text-muted;
    }
    BackgroundTaskList > .background-tasks--running {
        color: $warning;
    }
    BackgroundTaskList > .background-tasks--hover {
        background: $primary 30%;
    }
    BackgroundTaskList > #background-tasks-box {
        height: 1fr;
        scrollbar-size: 1 1;
        overflow-x: hidden;
        padding: 0 0 0 1;
        display: none;
    }
    BackgroundTaskList #background-tasks-rows {
        width: 100%;
        height: auto;
        pointer: pointer;
    }
    BackgroundTaskList > #background-tasks-empty {
        height: 1fr;
        color: $text-muted;
        content-align: center middle;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._composited = False
        self._timer: Timer | None = None
        self._painted_rows = -1
        self._hovered: int | None = None
        """The row under the mouse."""
        self._clear_text = ""
        """The Clear ended link, as the locale words it."""
        self._labels: tuple[str, str] | None = None
        """The border title and subtitle as shown; the subtitle is empty while nothing ended."""

    def compose(self) -> ComposeResult:
        # Not focusable: a click on a row must leave the keyboard where it was.
        with VerticalScroll(id="background-tasks-box", can_focus=False):
            yield Static(id="background-tasks-rows")
        yield SidebarEmptyStateLabel(id="background-tasks-empty")

    def on_mount(self) -> None:
        self._timer = self.set_interval(1, self._tick, pause=True)
        # Scrolled, another row is under the mouse: the next move finds it.
        self.watch(self.query_one("#background-tasks-box", VerticalScroll), "scroll_y", self.unhover, init=False)
        self._localize()
        self._paint()

    def on_show(self) -> None:
        self._composited = True
        self._paint()

    def on_hide(self) -> None:
        self._composited = False
        self._sync_timer()

    def on_resize(self) -> None:
        # At the bottom of the tab, resized it moved its rows under the mouse.
        self.unhover()

    def watch_tasks(self) -> None:
        if self.is_mounted:
            self._paint()

    def refresh_localization(self) -> None:
        """Retranslate the list."""
        if self.is_mounted:
            self._localize()
            self._paint()

    def _localize(self) -> None:
        self._clear_text = self._text(_CLEAR_ENDED.bind())
        self.query_one("#background-tasks-empty", Static).update(Text(self._text(_EMPTY.bind())))

    def _tick(self) -> None:
        # Under a dialog it keeps ticking, painting nothing, as the running tool cards do.
        if is_widget_shown_on_active_screen(self):
            self._paint()

    def _sync_timer(self) -> None:
        if self._timer is None:
            return
        if self._composited and any(task.running for task in self.tasks):
            self._timer.resume()
        else:
            self._timer.pause()

    def _paint(self) -> None:
        tasks = self.tasks
        running = sum(task.running for task in tasks)
        title = self._text(_TITLE_RUNNING.bind(count=running) if running else _TITLE.bind())
        labels = (title, self._clear_text if running < len(tasks) else "")
        if labels != self._labels:
            # On the border, they repaint the box and lay out nothing.
            self._labels = labels
            self.border_title = Text(title)
            self.border_subtitle = Text(labels[1], style="underline") if labels[1] else None
            if not labels[1]:
                self._point(on_clear=False)
        rows = self.query_one("#background-tasks-rows", Static)
        rows.update(self._rows(tasks), layout=len(tasks) != self._painted_rows)
        self._painted_rows = len(tasks)
        box = self.query_one("#background-tasks-box", VerticalScroll)
        if box.display != bool(tasks):
            box.display = bool(tasks)
            self.query_one("#background-tasks-empty", Static).display = not tasks
        self._sync_timer()

    def _rows(self, tasks: tuple[BackgroundTaskView, ...]) -> Table:
        resolved = self.get_component_rich_style("background-tasks--running", default=Style(color="yellow"))
        running_style = Style(color=resolved.color)
        hover = Style(bgcolor=self.get_component_rich_style("background-tasks--hover").bgcolor)
        now = time.time()
        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_column(justify="right", no_wrap=True)
        for row, task in enumerate(tasks):
            detail = running_style if task.running else "dim"
            grid.add_row(
                task_icon(task, running_style=running_style),
                Text(one_line(task.command), no_wrap=True, overflow="ellipsis"),
                Text(task_duration(task, now), style=detail),
                Text(self._text(task_outcome(task)), style=detail),
                style=hover if row == self._hovered else None,
            )
        return grid

    def _text(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def on_click(self, event: Click) -> None:
        if self._on_clear(event):
            event.stop()
            self.post_message(self.ClearEnded())
            return
        row = self._row_at(event)
        if row is not None:
            event.stop()
            self.post_message(self.Opened(self.tasks[row].id))

    def on_mouse_move(self, event: MouseMove) -> None:
        self._point(on_clear=self._on_clear(event))
        self._hover(self._row_at(event))

    def _on_clear(self, event: MouseEvent) -> bool:
        """Whether *event* is on the Clear ended link at the right of the bottom border."""
        clear = self._labels[1] if self._labels is not None else ""
        if not clear or event.widget is not self:
            return False
        width, height = self.outer_size
        # As Textual draws a right-aligned label: the text, cut to leave both corners, an edge
        # cell beside each and a blank either side of it, ends one edge cell before the corner.
        text = min(cell_len(clear), width - 6)
        if text <= 0:
            return False
        return event.y == height - 1 and width - 4 - text <= event.x < width - 2

    def _point(self, *, on_clear: bool) -> None:
        pointer = "pointer" if on_clear else "default"
        if self.styles.pointer != pointer:
            self.styles.pointer = pointer

    def _row_at(self, event: MouseEvent) -> int | None:
        rows = self.query_one("#background-tasks-rows", Static)
        # Scrolled, the rows reach past their box: only the mouse on them counts.
        offset = event.get_content_offset(rows) if event.widget is rows else None
        return offset.y if offset is not None and 0 <= offset.y < len(self.tasks) else None

    def on_leave(self, event: Leave) -> None:
        if event.node is self:
            # Off its own cells, to one of its widgets or out of the list: off the link.
            self._point(on_clear=False)
        elif event.node is self.query_one("#background-tasks-rows", Static):
            self._hover(None)

    def unhover(self) -> None:
        """Drop the highlight, as the rows moved under the mouse: its next move finds the row there."""
        self._hover(None)

    def _hover(self, row: int | None) -> None:
        if row != self._hovered:
            self._hovered = row
            self.query_one("#background-tasks-rows", Static).update(self._rows(self.tasks), layout=False)
