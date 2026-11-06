# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""BackgroundTaskModal — one background command's details and its live output.

The modal paints first and reads the log after: the output tail is read in a
thread, at most its last 256 KB, then only what was added since, once a second.
The output follows new lines while it is scrolled to the bottom.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses
import os
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import on
from textual.containers import ScrollableContainer, VerticalGroup
from textual.css.query import NoMatches
from textual.widgets import Button, Log, Static, TabbedContent, TabPane

from chrys.app.tui.binding_display import CLOSE_BINDING, COPY_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_locale_controller, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog, NoticeDialog
from chrys.app.tui.screens.dialogs.tool_view import (
    COPIED_OUTPUT,
    COPIED_OUTPUT_TOO_LARGE,
    COPIED_OUTPUT_UNAVAILABLE,
    copy_and_notify,
)
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.widgets import DialogButtonRow, DialogButtonSpec
from chrys.app.tui.widgets.chat.tool_view_builders import build_code_view
from chrys.app.tui.widgets.sidebar.background_tasks import (
    one_line,
    task_duration,
    task_icon,
    task_outcome,
)
from chrys.app.tui.widgets.task_summary import TaskSummaryData
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.platform.files import surrogate_safe_text

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from textual.app import ComposeResult
    from textual.widget import Widget

    from chrys.foundation.i18n import MessageDef
    from chrys.foundation.models.background_tasks import BackgroundTaskView
    from chrys.service.tools.background.crash import LeftCommand

_TAIL_BYTES = 256 * 1024
"""How much of the log's end the modal shows at most."""
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")
_MAX_LINES = 10_000
_POLL_SECONDS = 1.0

_TITLE = msg("tui.background_task.title", fallback="Background task · {command}")
_INPUT = msg("tui.background_task.tab.input", fallback="Input")
_OUTPUT = msg("tui.background_task.tab.output", fallback="Output")
_RUNNING = msg("tui.background_task.running", fallback="running")
_REASON = msg("tui.background_task.field.reason", fallback="Reason")
_CWD = msg("tui.background_task.field.cwd", fallback="Folder")
_STARTED = msg("tui.background_task.field.started", fallback="In background since")
_WHY = msg("tui.background_task.field.trigger", fallback="Moved there because")
_PROCESSES = msg("tui.background_task.field.processes", fallback="Processes")
_LOG = msg("tui.background_task.field.log", fallback="Log")
_TRIGGER_TIMEOUT = msg("tui.background_task.trigger.timeout", fallback="it ran past its timeout")
_TRIGGER_IDLE = msg("tui.background_task.trigger.idle", fallback="it printed nothing and was nearly idle for a minute")
_TRIGGER_SHELL_EXITED = msg(
    "tui.background_task.trigger.shell_exited",
    fallback="its shell exited while processes it started kept running",
)
_TRIGGER_USER = msg("tui.background_task.trigger.user", fallback="you moved it there")
_TRIGGERS = {
    "timeout": _TRIGGER_TIMEOUT,
    "idle": _TRIGGER_IDLE,
    "shell_exited": _TRIGGER_SHELL_EXITED,
    "user": _TRIGGER_USER,
}
_STOP = msg("tui.background_task.button.stop", fallback="Stop")
_COPY_OUTPUT = msg("tui.background_task.button.copy_output", fallback="Copy output")
_COPY_LOG_PATH = msg("tui.background_task.button.copy_log_path", fallback="Copy log path")
_STOP_TITLE = msg("tui.background_task.stop.title", fallback="Stop this background task?")
_STOP_MESSAGE = msg(
    "tui.background_task.stop.message",
    fallback="This stops these tasks and every process they started.",
)
_COPIED_LOG_PATH = msg("tui.background_task.copy.log_path", fallback="Copied log path")
_COPIED_LOG_PATH_UNAVAILABLE = msg(
    "tui.background_task.copy.log_path_unavailable",
    fallback="Copied log path (terminal clipboard unavailable)",
)
_LOG_UNREADABLE = msg("tui.background_task.log_unreadable", fallback="The log can't be read.")
_STOP_ALL_TITLE = msg("tui.background_task.stop_all.title", fallback="Stop background tasks?")
_STOP_ALL_MESSAGE = msg(
    "tui.background_task.stop_all.message",
    fallback="This stops the background task:",
    plural_fallback="This stops {count} background tasks:",
)
_STOP_AND_CONTINUE = msg("tui.background_task.button.stop_and_continue", fallback="Stop and continue")
_STOP_AND_EXIT = msg("tui.background_task.button.stop_and_exit", fallback="Stop and exit")
_LEFT_BEHIND_TITLE = msg("tui.background_task.left_behind.title", fallback="Commands still running")
_LEFT_BEHIND_MESSAGE = msg(
    "tui.background_task.left_behind.message",
    fallback="{app} closed unexpectedly while these commands ran in the background. They are still running:",
)
_IGNORE = msg("tui.background_task.button.ignore", fallback="Ignore")
ROLLBACK_NOTE = msg(
    "tui.background_task.stop_all.rollback_note",
    fallback="Discarding turns may not restore what they changed while in the background.",
)


def confirm_stopping(
    tasks: Sequence[BackgroundTaskView], owner: Widget, *, note: MessageDef | None = None, exiting: bool = False
) -> NoticeDialog:
    """A confirmation, for *owner* to show, that names the running *tasks* an action stops, with *note* below them.

    *exiting* when the action quits, so its button says so.
    """
    now = time.time()
    return NoticeDialog(
        title=_STOP_ALL_TITLE.bind(),
        message=_STOP_ALL_MESSAGE.bind(count=len(tasks)),
        tasks=tuple(TaskSummaryData(task.command, task_duration(task, now), task.reason) for task in tasks),
        impact=(_STOP_MESSAGE.bind(),) + ((note.bind(),) if note is not None else ()),
        confirm_label=(_STOP_AND_EXIT if exiting else _STOP_AND_CONTINUE).bind(),
        confirm_variant="error",
        locale_controller=widget_locale_controller(owner),
    )


def confirm_stopping_left_behind(commands: Sequence[LeftCommand], owner: Widget) -> NoticeDialog:
    """A question, for *owner* to show, whether to stop the *commands* a launch that crashed left running."""
    return NoticeDialog(
        title=_LEFT_BEHIND_TITLE.bind(),
        message=_LEFT_BEHIND_MESSAGE.bind(app=APP_DISPLAY_NAME),
        tasks=tuple(TaskSummaryData(command.command, reason=command.cwd) for command in commands),
        impact=(_STOP_MESSAGE.bind(),),
        confirm_label=_STOP.bind(),
        cancel_label=_IGNORE.bind(),
        confirm_variant="error",
        locale_controller=widget_locale_controller(owner),
    )


class LogTail:
    """Reads what a log gained since the last read, at most its last ``_TAIL_BYTES``."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._offset = 0
        self._file: tuple[int, int] | None = None
        """The (device, inode) read last: a rotated log is a new file."""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._held_cr = ""
        """A CR the last read ended on, given with the next read, where an LF may complete it."""

    def read(self) -> tuple[str, bool]:
        """The text added since the last read, made safe to show, and whether it replaces what was read before; blocks."""
        text, restart = self.read_raw()
        return sanitize_source_text(text, tab_size=8), restart

    def read_raw(self) -> tuple[str, bool]:
        """``read``, with the text as the log has it: what a copy takes."""
        with open(self._path, "rb") as file:
            stat = os.fstat(file.fileno())
            size = stat.st_size
            rotated = self._file is not None and self._file != (stat.st_dev, stat.st_ino)
            self._file = (stat.st_dev, stat.st_ino)
            # A rotated log starts over; one that grew by more than the tail skips ahead.
            restart = rotated or size < self._offset or size - self._offset > _TAIL_BYTES
            start = max(0, size - _TAIL_BYTES) if restart else self._offset
            file.seek(start)
            data = file.read(size - start)
        self._offset = start + len(data)
        if restart:
            self._decoder.reset()
            self._held_cr = ""
            if start > 0:
                # Cut into a line: it starts after the first break, CR included (a hook's log keeps them).
                first = _LINE_BREAK.search(data)
                data = data[first.end() :] if first else b""
        # A read can end inside a character: the decoder keeps its first bytes for the next read.
        text = self._held_cr + self._decoder.decode(data)
        # Or between the CR and the LF of one break, which shown apart would end two lines.
        if text.endswith("\r"):
            text, self._held_cr = text[:-1], "\r"
        else:
            self._held_cr = ""
        return text, restart


def _read_log(path: str) -> str:
    with open(path, "rb") as file:
        return file.read().decode("utf-8", errors="replace")


class BackgroundTaskModal(BaseDialog[None]):
    """A background command's input and live output, with Stop and copy actions."""

    CSS_PATH = "background_task.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "close", CLOSE_BINDING, show=False),
        localized_binding("c", "copy_output", COPY_BINDING, show=False),
    ]

    def __init__(
        self,
        task: BackgroundTaskView,
        *,
        latest: Callable[[str], BackgroundTaskView | None],
        stop: Callable[[str], object],
    ) -> None:
        super().__init__()
        self._shown = task
        self._latest = latest
        """The task as the main screen knows it now; None once it left the list."""
        self._stop = stop
        self._tail = LogTail(task.log_path)
        self._reading = False

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        task = self._shown
        with VerticalGroup(id="background-task-container") as container:
            container.border_title = Text(render_str(localizer, _TITLE.bind(command=one_line(task.command))))
            with TabbedContent(id="background-task-tabs"):
                with (
                    TabPane(render_str(localizer, _INPUT.bind()), id="background-task-input"),
                    ScrollableContainer(classes="background-task-scroll"),
                ):
                    yield build_code_view("bash", task.command, dark=self.app.current_theme.dark)
                    yield Static(self._fields(), id="background-task-fields")
                with TabPane(render_str(localizer, _OUTPUT.bind()), id="background-task-output"):
                    yield Log(max_lines=_MAX_LINES, id="background-task-log")
            yield DialogButtonRow(
                DialogButtonSpec(Text(render_str(localizer, _STOP.bind())), id="background-task-stop", variant="error"),
                DialogButtonSpec(Text(render_str(localizer, _COPY_OUTPUT.bind())), id="background-task-copy-output"),
                DialogButtonSpec(Text(render_str(localizer, _COPY_LOG_PATH.bind())), id="background-task-copy-path"),
                id="background-task-buttons",
            )

    def on_mount(self) -> None:
        self._show_state()
        # Like ToolDetailModal: switch tabs once they mounted, never through ``initial=``.
        self.call_after_refresh(self._open_output)
        self.set_interval(_POLL_SECONDS, self._poll)

    def _open_output(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one(TabbedContent).active = "background-task-output"
        self._poll()

    def _fields(self) -> Table:
        localizer = widget_localizer(self)
        task = self._shown
        trigger = _TRIGGERS.get(task.trigger)
        rows: list[tuple[MessageRef, str]] = [
            (_REASON.bind(), one_line(task.reason)),
            (_CWD.bind(), surrogate_safe_text(task.cwd)),
            (
                _STARTED.bind(),
                datetime.fromtimestamp(task.started_at, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            ),
            (_WHY.bind(), render_str(localizer, trigger.bind()) if trigger is not None else task.trigger),
            (_PROCESSES.bind(), ", ".join(str(pid) for pid in task.pids)),
            (_LOG.bind(), surrogate_safe_text(task.log_path)),
        ]
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(overflow="fold")
        for label, value in rows:
            if value:
                grid.add_row(render_str(localizer, label), Text(value))
        return grid

    def _poll(self) -> None:
        latest = self._latest(self._shown.id)
        if latest is None and self._shown.running:
            # It left the list unseen, as more ended at once than the list keeps: it ended.
            latest = dataclasses.replace(self._shown, ended_at=time.time())
        if latest is not None and latest != self._shown:
            self._shown = latest
            with contextlib.suppress(NoMatches):
                # Its processes change as its shell exits before them.
                self.query_one("#background-task-fields", Static).update(self._fields())
        self._show_state()
        if not self._reading:
            self._reading = True
            self.run_worker(self._read_more(), exit_on_error=False)

    def _show_state(self) -> None:
        task = self._shown
        localizer = widget_localizer(self)
        label = _RUNNING.bind() if task.running and task.stopped_by is None else task_outcome(task)
        subtitle = Text.assemble(
            task_icon(task, running_style=Style(color="yellow")),
            f" {render_str(localizer, label)} · {task_duration(task, time.time())}",
        )
        with contextlib.suppress(NoMatches):
            self.query_one("#background-task-container").border_subtitle = subtitle
            stop = self.query_one("#background-task-stop", Button)
            stoppable = task.running and task.stopped_by is None
            if stop.disabled == stoppable:
                stop.disabled = not stoppable

    async def _read_more(self) -> None:
        try:
            text, restart = await asyncio.to_thread(self._tail.read)
        except OSError:
            return
        finally:
            self._reading = False
        with contextlib.suppress(NoMatches):
            log = self.query_one(Log)
            if restart:
                log.clear()
            if text:
                log.write(text, scroll_end=log.is_vertical_scroll_end)

    @on(Button.Pressed, "#background-task-stop")
    def _on_stop(self, event: Button.Pressed) -> None:
        event.stop()

        def stop_if_confirmed(confirmed: bool | None) -> None:
            if confirmed:
                self._stop(self._shown.id)

        self.app.push_screen(
            ConfirmDialog(
                title=_STOP_TITLE.bind(),
                message=_STOP_ALL_MESSAGE.bind(count=1),
                tasks=(
                    TaskSummaryData(self._shown.command, task_duration(self._shown, time.time()), self._shown.reason),
                ),
                impact=(_STOP_MESSAGE.bind(),),
                confirm_label=_STOP.bind(),
                confirm_variant="error",
                locale_controller=widget_locale_controller(self),
            ),
            stop_if_confirmed,
        )

    @on(Button.Pressed, "#background-task-copy-output")
    def _on_copy_output(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_copy_output()

    @on(Button.Pressed, "#background-task-copy-path")
    def _on_copy_path(self, event: Button.Pressed) -> None:
        event.stop()
        copy_and_notify(
            self,
            self._shown.log_path,
            copied=_COPIED_LOG_PATH,
            too_large=_COPIED_LOG_PATH_UNAVAILABLE,
            unavailable=_COPIED_LOG_PATH_UNAVAILABLE,
        )

    def action_copy_output(self) -> None:
        """Copy the whole log as it is now."""
        self.run_worker(self._copy_output(), exit_on_error=False)

    async def _copy_output(self) -> None:
        try:
            output = await asyncio.to_thread(_read_log, self._shown.log_path)
        except OSError:
            self.notify(render_str(widget_localizer(self), _LOG_UNREADABLE.bind()), severity="warning", markup=False)
            return
        copy_and_notify(
            self,
            output,
            copied=COPIED_OUTPUT,
            too_large=COPIED_OUTPUT_TOO_LARGE,
            unavailable=COPIED_OUTPUT_UNAVAILABLE,
        )

    def action_close(self) -> None:
        """Close the dialog."""
        self.dismiss(None)
