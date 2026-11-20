# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Display-only, cell-bounded command previews shared by stop confirmations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import split_graphemes
from rich.text import Text
from textual.containers import HorizontalGroup, VerticalGroup
from textual.widgets import Static

from chrys.app.tui.util.formatting import grapheme_clusters
from chrys.app.tui.util.source_text import mark_hidden_format
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import sanitize_terminal_block

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult

    from chrys.foundation.i18n import MessageRef


_DURATION = msg("tui.task_summary.duration", fallback="Running for {duration}")


def display_command(value: str) -> str:
    """Expand tabs at terminal cell stops, leaving the original command untouched."""
    value = mark_hidden_format(sanitize_terminal_block(value))
    lines = []
    for line in value.split("\n"):
        parts = line.split("\t")
        expanded = parts[0]
        column = split_graphemes(expanded)[1]
        for part in parts[1:]:
            spaces = 4 - column % 4
            expanded += " " * spaces + part
            column += spaces + split_graphemes(part)[1]
        lines.append(expanded)
    return "\n".join(lines)


def _take_cells(value: str, width: int, *, tail: bool = False) -> str:
    graphemes = grapheme_clusters(value)
    selected = []
    used = 0
    for cluster, cells in reversed(graphemes) if tail else graphemes:
        if used + cells > width:
            break
        selected.append(cluster)
        used += cells
    return "".join(reversed(selected) if tail else selected)


def preview_text(value: str, width: int) -> tuple[str, bool]:
    """Wrap at grapheme boundaries into at most two lines; never interpret shell syntax."""
    width = max(2, width)
    value = display_command(value)
    lines = []
    for line in value.split("\n"):
        current = ""
        used = 0
        for cluster, cells in grapheme_clusters(line):
            if used + cells > width:
                lines.append(current)
                current, used = "", 0
            current += cluster
            used += cells
        lines.append(current)
    if len(lines) <= 2:
        if len(lines) == 2 and "\n" not in value:
            # Prefer a whitespace boundary when both complete lines still fit.
            for start, _, _ in reversed(split_graphemes(lines[0])[0]):
                if value[start] == " " and split_graphemes(value[start + 1 :])[1] <= width:
                    return value[: start + 1] + "\n" + value[start + 1 :], False
        return "\n".join(lines), False
    flat = value.replace("\n", " ")
    return _take_cells(flat, width - 1) + "…\n" + _take_cells(flat, width, tail=True), True


@dataclass(frozen=True, slots=True)
class TaskSummaryData:
    command: str
    duration: str = ""
    reason: str = ""


class TaskSummary(VerticalGroup):
    """A command, separate runtime and reason, with access to its original text."""

    DEFAULT_CSS = """
    TaskSummary { height: auto; margin-bottom: 1; }
    TaskSummary Static { height: auto; text-align: left; }
    TaskSummary .task-command { text-wrap: nowrap; }
    TaskSummary .task-info { height: 1; }
    TaskSummary .task-reason { width: 1fr; text-wrap: nowrap; }
    TaskSummary .task-meta { width: auto; text-align: right; text-wrap: nowrap; }
    TaskSummary .task-meta, TaskSummary .task-reason { color: $text-muted; }
    """

    def __init__(self, data: TaskSummaryData, render: Callable[[MessageRef], str]) -> None:
        super().__init__()
        self.data = data
        self._render_label = render

    def compose(self) -> ComposeResult:
        yield Static(Text(), classes="task-command")
        with HorizontalGroup(classes="task-info"):
            yield Static(Text(), classes="task-reason")
            yield Static(Text(), classes="task-meta")

    def refresh_preview(self, width: int) -> None:
        command, truncated = preview_text(self.data.command, width)
        preview = self.query_one(".task-command", Static)
        preview.update(Text(command, no_wrap=True, overflow="crop"))
        preview.tooltip = Text(display_command(self.data.command)) if truncated else None
        meta = self.query_one(".task-meta", Static)
        duration = self._render_label(_DURATION.bind(duration=self.data.duration)) if self.data.duration else ""
        reason_text = display_command(self.data.reason).replace("\n", " ")
        reason_width = split_graphemes(reason_text)[1]
        duration_width = split_graphemes(duration)[1]
        meta.display = bool(duration) and reason_width + duration_width + (2 if reason_text else 0) <= width
        meta.styles.width = duration_width
        meta.update(Text(duration, no_wrap=True, overflow="crop"))
        reason = self.query_one(".task-reason", Static)
        if reason_width > width:
            reason_text = _take_cells(reason_text, width - 1) + "…"
        reason.update(Text(reason_text, no_wrap=True, overflow="crop"))
        self.query_one(".task-info").display = bool(reason_text or duration)
