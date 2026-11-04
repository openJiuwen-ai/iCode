# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TasksPanel — live session todo checklist, and the main agent's background commands below it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Size
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.todo_format import todo_checklist_renderable, todo_done_count
from chrys.app.tui.util.visibility import is_widget_shown
from chrys.app.tui.widgets.sidebar.background_tasks import BackgroundTaskList
from chrys.app.tui.widgets.sidebar.empty_state import SidebarEmptyStateLabel
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

_TASKS_TITLE = msg("tui.sidebar.tasks.title", fallback="Todos")
_TASKS_COUNTER = msg("tui.sidebar.tasks.counter", fallback="Todos ({done}/{total})")
_TASKS_EMPTY = msg("tui.sidebar.tasks.empty", fallback="No todos yet")

_SECTION_CHROME = 2
"""Rows a section spends around its content: the two borders of its box, the top one carrying its title."""

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.models.background_tasks import BackgroundTaskView
    from chrys.foundation.models.todos import TodoItem


def section_heights(height: int, todo_rows: int, background_rows: int) -> tuple[int | None, int | None]:
    """The rows the todo and background sections take of *height*; None takes what the other leaves.

    Half each, unless one section's content overflows its half while the
    other's fits in it: then the other shrinks to its content, but not below
    a quarter, and the first takes the rest.
    """
    half = height // 2 - _SECTION_CHROME

    def fitted(rows: int) -> int:
        return max(rows + _SECTION_CHROME, height // 4)

    if todo_rows > half >= background_rows:
        return None, fitted(background_rows)
    if background_rows > half >= todo_rows:
        return fitted(todo_rows), None
    return None, None


@dataclass(frozen=True, slots=True)
class TodoListState:
    """View state for the Tasks panel (full-list payload, not a diff)."""

    items: tuple[TodoItem, ...] = ()


class TasksPanel(Widget, can_focus=False):
    """Renders the session todo list with live status markers, then the background commands.

    The checklist lives in a Static (re-rendered at the real content width on
    every layout) rather than a RichLog: RichLog wraps writes at
    ``min_width=78`` and never re-wraps, which cropped long items in the
    narrow sidebar. Each section shows its empty state when it has nothing to
    list; ``section_heights`` splits the height between them.
    """

    todo_state: reactive[TodoListState | None] = reactive(None, always_update=True)
    background_tasks: reactive[tuple[BackgroundTaskView, ...]] = reactive(())
    _MAX_TODO_SYNC_RETRIES = 3
    COMPONENT_CLASSES: ClassVar[set[str]] = {"tasks--in-progress"}

    DEFAULT_CSS = """
    TasksPanel {
        height: 100%;
        width: 100%;
    }
    TasksPanel > #tasks-todos {
        height: 1fr;
        border: round $tui-border-primary-darken-3 $inner-border-opacity;
        border-title-color: $text-muted;
        border-title-style: bold;
    }
    TasksPanel > .tasks--in-progress {
        color: $warning;
    }
    TasksPanel #tasks-list {
        height: 1fr;
        scrollbar-size: 1 1;
        overflow-x: hidden;
        padding: 0 0 0 1;
        display: none;
    }
    TasksPanel #tasks-checklist {
        width: 100%;
        height: auto;
    }
    TasksPanel #tasks-empty {
        height: 1fr;
        color: $text-muted;
        content-align: center middle;
    }
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
        locale_controller: LocaleController | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self._locale_controller = locale_controller
        self._render_state: TodoListState | None = None
        self._sync_pending = False
        self._sync_retries = 0
        self._theme_style: Style | None = None
        self._cleared: set[str] = set()
        """Ended background commands the user cleared from the list."""
        self._heights: tuple[int | None, int | None] = (None, None)
        self._title = self._render_message(_TASKS_TITLE.bind())
        """The todo box's border title, as shown."""

    def compose(self) -> ComposeResult:
        todos = Vertical(id="tasks-todos")
        todos.border_title = Text(self._title)
        with todos:
            with VerticalScroll(id="tasks-list"):
                yield Static(id="tasks-checklist")
            yield SidebarEmptyStateLabel(Text(self._render_message(_TASKS_EMPTY.bind())), id="tasks-empty")
        yield BackgroundTaskList(locale_controller=self._locale_controller)

    def on_mount(self) -> None:
        self.app.theme_changed_signal.subscribe(self, self._theme_colors_changed)
        if self.todo_state is not None:
            self._render_state = self.todo_state
        self._request_todo_sync()
        self._show_background_tasks()

    def on_unmount(self) -> None:
        self.app.theme_changed_signal.unsubscribe(self)

    def on_resize(self) -> None:
        # Resized, it moves the background rows under the mouse, also where their list keeps its size.
        self.query_one(BackgroundTaskList).unhover()
        self._split()

    def _theme_colors_changed(self, _theme: object) -> None:
        if self._in_progress_style() != self._theme_style:
            self._request_todo_sync()

    def _in_progress_style(self) -> Style:
        # Resolve alpha against the live background just as CSS does. Keep only
        # foreground so a Rich row doesn't acquire a painted background rectangle.
        resolved = self.get_component_rich_style("tasks--in-progress", default=Style(color="yellow"))
        return Style(color=resolved.color, bold=True)

    def watch_background_tasks(self) -> None:
        self._show_background_tasks()

    def on_background_task_list_clear_ended(self, event: BackgroundTaskList.ClearEnded) -> None:
        event.stop()
        self._cleared.update(task.id for task in self.background_tasks if not task.running)
        self._show_background_tasks()

    def _show_background_tasks(self) -> None:
        if not self.is_mounted:
            return
        self._cleared.intersection_update(task.id for task in self.background_tasks)
        self.query_one(BackgroundTaskList).tasks = tuple(
            task for task in self.background_tasks if task.id not in self._cleared
        )
        self._split()

    def _split(self) -> None:
        """Share the height between the sections by what each has to show; a same split changes nothing."""
        # Hidden, its size costs a whole-screen arrange to look up: the Resize that shows it splits instead.
        if not is_widget_shown(self):
            return
        width, height = self.content_size
        todo_rows = 1
        if self._render_state is not None and self._render_state.items:
            # The checklist's height at the width its box gives it, as the next layout will find it.
            width -= self.query_one("#tasks-todos", Vertical).gutter.width
            width -= self.query_one("#tasks-list", VerticalScroll).gutter.width
            size = Size(width, height)
            todo_rows = self.query_one("#tasks-checklist", Static).get_content_height(size, size, width)
        background = self.query_one(BackgroundTaskList)
        heights = section_heights(height, todo_rows, max(len(background.tasks), 1))
        if heights != self._heights:
            self._heights = heights
            todo_height, background_height = heights
            self.query_one("#tasks-todos", Vertical).styles.height = "1fr" if todo_height is None else todo_height
            background.styles.height = "1fr" if background_height is None else background_height

    def watch_todo_state(self, state: TodoListState | None) -> None:
        """Apply routed todo state from the screen."""
        if state is None:
            return
        self._render_state = state
        self._sync_retries = 0
        self._sync_todo_state()

    def refresh_localization(self) -> None:
        """Retranslate the current title/counter and empty-state label."""
        if not self.is_mounted:
            return
        self.query_one(BackgroundTaskList).refresh_localization()
        self.query_one("#tasks-empty", Static).update(Text(self._render_message(_TASKS_EMPTY.bind())))
        if self._render_state is None:
            self._show_title(self.query_one("#tasks-todos", Vertical), _TASKS_TITLE.bind())
            return
        self._sync_todo_state()

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        if controller is None:
            return format_message(reference)
        return render_str(controller.localizer, reference)

    def _show_title(self, todos: Vertical, reference: MessageRef) -> None:
        # On the border, a new title repaints the box and lays out nothing.
        title = self._render_message(reference)
        if title != self._title:
            self._title = title
            todos.border_title = Text(title)

    def _request_todo_sync(self) -> None:
        if self._sync_pending:
            return
        self._sync_pending = self.call_after_refresh(self._sync_todo_state)

    def _retry_todo_sync(self) -> None:
        if self._sync_retries >= self._MAX_TODO_SYNC_RETRIES:
            return
        self._sync_retries += 1
        self._request_todo_sync()

    def _sync_todo_state(self) -> None:
        self._sync_pending = False
        self._theme_style = self._in_progress_style()
        state = self._render_state
        if state is None:
            return
        try:
            todos = self.query_one("#tasks-todos", Vertical)
            todo_list = self.query_one("#tasks-list", VerticalScroll)
            checklist = self.query_one("#tasks-checklist", Static)
            empty = self.query_one("#tasks-empty", Static)
        except NoMatches:
            self._retry_todo_sync()
            return
        items = state.items
        empty.update(Text(self._render_message(_TASKS_EMPTY.bind())))
        if not items:
            self._show_title(todos, _TASKS_TITLE.bind())
            checklist.update("")
        else:
            self._show_title(todos, _TASKS_COUNTER.bind(done=todo_done_count(items), total=len(items)))
            checklist.update(todo_checklist_renderable(items, in_progress_style=self._theme_style))
        todo_list.display = bool(items)
        empty.display = not items
        self._split()
        self._sync_retries = 0
