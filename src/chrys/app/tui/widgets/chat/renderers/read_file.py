# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Renderer for the ``read_file`` tool.

Shows a ``- read_file (Nms)`` label followed by a bordered panel with
the file path as border title and syntax-highlighted content inside.
"""

from __future__ import annotations

import os
import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.syntax import Syntax
from rich.text import Text
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.util.static_update import update_static_in_place
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.chat.tool_call import (
    TOOL_CARD_REJECTED,
    BaseToolCard,
    ToolCardHeader,
    fmt_duration,
    tool_result_render_status,
)
from chrys.foundation.i18n import msg
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ

if TYPE_CHECKING:
    from textual.app import ComposeResult

_READ_FILE_READING = msg("tui.tool_card.read_file.reading", fallback="reading")
_READ_FILE_LINE_COUNT = msg(
    "tui.tool_card.read_file.line_count",
    fallback="{count} line",
    plural_fallback="{count} lines",
)
_READ_FILE_LINE_RANGE = msg(
    "tui.tool_card.read_file.line_range",
    fallback=" ({first}-{last})",
)
_READ_FILE_ERROR_SUFFIX = msg("tui.tool_card.read_file.error_suffix", fallback="error")

_MAX_LINES = 5
"""Max lines of file content to show before truncating."""

_HEADER_RE = re.compile(r"^File:\s+(.+?)\s+\((\d+)\s+lines?,\s+(\d+)\s+chars?\)")
"""Matches the 'File: /path (N lines, M chars)' header."""

_LINE_RE = re.compile(r"^(\d+)\|(.*)$")
"""Matches numbered content lines like '13|def foo():'."""

_READ_NOTICE_PREFIXES = ("[Truncated", "[Long lines truncated", "[Showing lines ")
"""Footer notices emitted by read_file for truncation and pagination."""


_LEXER_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".fish": "fish",
    ".ps1": "powershell",
    ".sql": "sql",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".xml": "xml",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".md": "markdown",
    ".rst": "rst",
    ".tex": "latex",
    ".r": "r",
    ".lua": "lua",
    ".pl": "perl",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".dart": "dart",
    ".vim": "vim",
    ".dockerfile": "dockerfile",
    ".tf": "terraform",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".gql": "graphql",
}
"""Pygments lexer names by lowercased file extension."""


def _guess_lexer(path: str) -> str:
    """Guess Pygments lexer name from file extension."""
    return _LEXER_BY_EXTENSION.get(os.path.splitext(path)[1].lower(), "text")


def _parse_read_result(result: str) -> tuple[str, list[tuple[int, str]], bool, bool]:
    """Parse read_file result.

    Returns: (file_path, [(line_num, text)], truncated, is_error)
    """
    if result.startswith("Error:"):
        return "", [], False, True

    lines = result.split("\n")
    if not lines:
        return "", [], False, True

    # Parse header
    file_path = ""
    m = _HEADER_RE.match(lines[0])
    if m:
        file_path = m.group(1)
        lines = lines[1:]

    # Parse numbered content lines
    content_lines: list[tuple[int, str]] = []
    truncated = False
    for line in lines:
        lm = _LINE_RE.match(line)
        if lm:
            content_lines.append((int(lm.group(1)), lm.group(2)))
        elif line.startswith("[Truncated"):
            truncated = True

    return file_path, content_lines, truncated, False


class ReadFileToolCall(BaseToolCard):
    """Rich renderer for ``read_file`` tool calls.

    Layout:
    - ``- read_file (Nms)`` label (bold)
    - Indented bordered panel with file path as title
    - Syntax-highlighted content inside
    """

    DEFAULT_CSS = """
    ReadFileToolCall {
        height: auto;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    ReadFileToolCall #rf-label {
        height: auto;
    }
    ReadFileToolCall > #rf-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-primary 50%;
        border-title-color: $text-error;
        border-title-style: bold;
        border-subtitle-align: right;
        border-subtitle-color: $text-muted;
        padding: 0 1;
    }
    ReadFileToolCall.-success > #rf-panel {
        border: round $tui-border-success 30%;
        border-title-color: $success;
        border-subtitle-color: $success;

        border-title-style: not bold;
    }
    ReadFileToolCall.-error > #rf-panel {
        border: round $tui-border-error 50%;
        border-title-color: $text-error;
        border-subtitle-color: $text-error;

        border-title-style: not bold;
    }
    ReadFileToolCall.-rejected > #rf-panel {
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-subtitle-color: $warning;

        border-title-style: not bold;
    }
    """

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(call_id, tool_name, args_summary, args=args)
        self._spin_idx = 0
        self._timer: Timer | None = None
        self._file_path = self.args.get("path", "")

    def _label_text(self, duration_ms: int = 0) -> Text:
        t = Text()
        t.append("• ", style="bold")
        t.append(self.tool_name, style="bold")
        if duration_ms:
            t.append(f" ({fmt_duration(duration_ms)})", style="dim")
        if self._file_path:
            t.append(f" {self._file_path}", style="dim")
        return t

    def compose(self) -> ComposeResult:
        yield ToolCardHeader(self._label_text(), id="rf-label")
        # A single bordered Static is the whole result panel: spinner while
        # running, syntax-highlighted preview (or error text) once done.
        panel = Static(self._render_spinner(), id="rf-panel")
        panel.border_title = Text(os.path.basename(self._file_path)) if self._file_path else Text(self.tool_name)
        yield panel

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._spin)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _spin(self) -> None:
        if self.status == "running":
            self._spin_idx = (self._spin_idx + 1) % len(self._SPINNERS)
            # Only a shown card repaints (see ``ToolCall._spin``); the first tick after it shows paints it.
            if not is_widget_shown_on_active_screen(self):
                return
            with suppress(Exception):
                update_static_in_place(self.query_one("#rf-panel", Static), self._render_spinner())

    def _render_spinner(self) -> Text:
        t = Text()
        t.append(f"{self._SPINNERS[self._spin_idx]} ", style="yellow")
        t.append(render_str(widget_localizer(self), _READ_FILE_READING.bind()), style="yellow")
        return t

    def _tool_view_output_widgets(self) -> list[Widget]:
        """Show the full file content, syntax-highlighted, with no line cap."""
        file_path, content_lines, _truncated, is_error = _parse_read_result(self.result_text)
        if is_error or not content_lines:
            return super()._tool_view_output_widgets()
        from chrys.app.tui.widgets.syntax_theme import transparent_syntax

        code = sanitize_source_text("\n".join(text for _, text in content_lines))
        syntax = transparent_syntax(
            code,
            _guess_lexer(file_path or self._file_path),
            dark=self._view_dark(),
            line_numbers=True,
            start_line=content_lines[0][0],
        )
        widgets: list[Widget] = [Static(syntax, classes="tool-view-code")]
        # Preserve both truncation warnings and optional pagination hints.
        widgets.extend(
            Static(Text(line, style="dim"), classes="tool-view-text")
            for line in self.result_text.split("\n")
            if line.startswith(_READ_NOTICE_PREFIXES)
        )
        return widgets

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        """Mark as complete with syntax-highlighted output."""
        self.result_text = result
        self.duration_ms = duration_ms
        self.approval = kwargs.get("approval")
        metadata = kwargs.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.status = "complete"
        if self._timer is not None:
            self._timer.stop()

        file_path, content_lines, file_truncated, parsed_error = _parse_read_result(result)
        render_status = tool_result_render_status(
            result,
            self.approval,
            self.tool_kind or KIND_FILESYSTEM_READ,
            self.metadata,
            parsed_error=parsed_error,
            parsed_error_overrides_structured_success=True,
        )

        # Update label with duration
        with suppress(Exception):
            self.query_one("#rf-label", Static).update(self._label_text(duration_ms))

        # Update panel title and subtitle
        panel = self.query_one("#rf-panel", Static)
        if file_path:
            self._file_path = file_path
        panel.border_title = Text(os.path.basename(self._file_path))
        if content_lines:
            n = len(content_lines)
            first, last = content_lines[0][0], content_lines[-1][0]
            subtitle = render_str(widget_localizer(self), _READ_FILE_LINE_COUNT.bind(count=n))
            if first != 1 or last != n:
                subtitle += render_str(
                    widget_localizer(self),
                    _READ_FILE_LINE_RANGE.bind(first=first, last=last),
                )
            panel.border_subtitle = Text(subtitle)

        if render_status == "rejected":
            self.status = "rejected"
            panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_REJECTED.bind())
            self.add_class("-rejected")
            with suppress(Exception):
                update_static_in_place(panel, Text(result))
            self.add_class("-done")
            self._show_tool_copy_button()
            return

        if render_status == "error":
            self.status = "error"
            self.add_class("-error")
            with suppress(Exception):
                update_static_in_place(panel, Text(result))
            self.add_class("-done")
            self._show_tool_copy_button()
            return

        self.add_class("-success")

        # Truncate to max lines
        display_truncated = file_truncated
        display_lines = content_lines
        if len(display_lines) > _MAX_LINES:
            display_lines = display_lines[:_MAX_LINES]
            display_truncated = True

        # Build syntax-highlighted output
        if display_lines:
            code = sanitize_source_text("\n".join(text for _, text in display_lines))
            lexer = _guess_lexer(self._file_path)
            start_line = display_lines[0][0]
            syntax = Syntax(
                code,
                lexer,
                line_numbers=True,
                start_line=start_line,
                theme="ansi_dark",
            )
            with suppress(Exception):
                if display_truncated:
                    update_static_in_place(panel, Group(syntax, Text("...", style="dim")))
                else:
                    update_static_in_place(panel, syntax)
        else:
            with suppress(Exception):
                update_static_in_place(panel, "")

        self.add_class("-done")
        self._show_tool_copy_button()

    def set_error(self, error: str) -> None:
        """Mark as failed."""
        self.result_text = error
        self.status = "error"
        if self._timer is not None:
            self._timer.stop()

        panel = self.query_one("#rf-panel", Static)
        error_label = render_str(widget_localizer(self), _READ_FILE_ERROR_SUFFIX.bind())
        panel.border_title = Text(f"{self._file_path} [{error_label}]")
        self.add_class("-error")
        with suppress(Exception):
            update_static_in_place(panel, Text(error))
        self.add_class("-done")
        self._show_tool_copy_button()
