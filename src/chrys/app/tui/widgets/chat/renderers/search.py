# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Renderers for the ``grep`` and ``glob`` search tools.

grep: ``- grep (Nms)`` label + bordered panel with pattern title,
      match results with file:line headers and context lines.
glob: ``- glob (Nms)`` label + bordered panel with pattern title,
      file list inside.
"""

from __future__ import annotations

import re
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.timer import Timer
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
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.tool_kinds import KIND_SEARCH

if TYPE_CHECKING:
    from textual.app import ComposeResult

_SEARCH_SEARCHING = msg("tui.tool_card.search.searching", fallback="searching")
_SEARCH_ERROR_SUFFIX = msg("tui.tool_card.search.error_suffix", fallback="error")

_GREP_MAX_BLOCKS = 1
"""Max match blocks to show in grep results."""

_GLOB_MAX_FILES = 3
"""Max files to show in glob results."""

_HEADER_RE = re.compile(r"^Found \d+ (?:match|file)\S* ")
"""Matches the 'Found N match(es)...' or 'Found N file(s)...' header line."""

_HEADER_PARTS_RE = re.compile(r"^(Found \d+ \S+).* in (.+?)( \(limited to \d+\))?$")
"""Captures count prefix, path, and optional limit suffix from the header line."""


# ---------------------------------------------------------------------------
# Base search widget — shared by grep and glob
# ---------------------------------------------------------------------------


class _SearchToolCall(BaseToolCard):
    """Base for search tool renderers (grep, glob).

    Layout: ``- toolname (Nms)`` label + indented bordered panel.
    """

    DEFAULT_CSS = """
    _SearchToolCall {
        height: auto;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    _SearchToolCall #search-label {
        height: auto;
    }
    _SearchToolCall > #search-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-primary 50%;
        border-title-color: $text-error;
        border-title-style: bold;
        border-subtitle-align: right;
        border-subtitle-color: $text-muted;
        padding: 0 1;
        color: $text-muted;
    }
    _SearchToolCall.-success > #search-panel {
        border: round $tui-border-success 30%;
        border-title-color: $success;
        border-subtitle-color: $success;

        border-title-style: not bold;
    }
    _SearchToolCall.-error > #search-panel {
        border: round $tui-border-error 50%;
        border-title-color: $text-error;
        border-subtitle-color: $text-error;

        border-title-style: not bold;
    }
    _SearchToolCall.-rejected > #search-panel {
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

    def _label_suffix(self) -> str:
        """Extra text appended to the label. Override in subclasses."""
        return ""

    def _label_text(self, duration_ms: int = 0) -> Text:
        t = Text()
        t.append("• ", style="bold")
        t.append(self.tool_name, style="bold")
        if duration_ms:
            t.append(f" ({fmt_duration(duration_ms)})", style="dim")
        suffix = self._label_suffix()
        if suffix:
            t.append(f" {sanitize_legacy_scalar(suffix)}", style="dim")
        return t

    def _build_title(self) -> str:
        """Build panel border title from args. Override in subclasses."""
        return self.tool_name

    def _format_result(self, result: str) -> tuple[str, str, str, bool]:
        """Parse result into (title, body, subtitle, is_error). Override in subclasses."""
        return "", result, "", result.startswith(("Error:", "No "))

    def compose(self) -> ComposeResult:
        yield ToolCardHeader(self._label_text(), id="search-label")
        # A single bordered Static is the whole result panel: spinner while
        # running, the formatted result body once done.
        panel = Static(self._render_spinner(), id="search-panel")
        panel.border_title = Text(sanitize_legacy_scalar(self._build_title()))
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
                update_static_in_place(self.query_one("#search-panel", Static), self._render_spinner())

    def _render_spinner(self) -> Text:
        t = Text()
        t.append(f"{self._SPINNERS[self._spin_idx]} ", style="yellow")
        t.append(render_str(widget_localizer(self), _SEARCH_SEARCHING.bind()), style="yellow")
        return t

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        """Mark as complete."""
        self.result_text = result
        self.duration_ms = duration_ms
        self.approval = kwargs.get("approval")
        metadata = kwargs.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.status = "complete"
        if self._timer is not None:
            self._timer.stop()

        header, body, subtitle, is_error = self._format_result(result)
        render_status = tool_result_render_status(
            result,
            self.approval,
            self.tool_kind or KIND_SEARCH,
            self.metadata,
            parsed_error=is_error,
            parsed_error_overrides_structured_success=True,
        )

        # Update label with duration
        with suppress(Exception):
            self.query_one("#search-label", Static).update(self._label_text(duration_ms))

        # Update panel title and subtitle
        panel = self.query_one("#search-panel", Static)
        if header:
            panel.border_title = Text(sanitize_legacy_scalar(header))
        if subtitle:
            panel.border_subtitle = Text(sanitize_legacy_scalar(subtitle))

        if render_status == "rejected":
            self.status = "rejected"
            panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_REJECTED.bind())
            self.add_class("-rejected")
        elif render_status == "error":
            self.status = "error"
            self.add_class("-error")
        else:
            self.add_class("-success")

        with suppress(Exception):
            update_static_in_place(panel, Text(sanitize_source_text(body, tab_size=8)))
        self.add_class("-done")
        self._show_tool_copy_button()

    def set_error(self, error: str) -> None:
        """Mark as failed."""
        self.result_text = error
        self.status = "error"
        if self._timer is not None:
            self._timer.stop()

        panel = self.query_one("#search-panel", Static)
        error_label = render_str(widget_localizer(self), _SEARCH_ERROR_SUFFIX.bind())
        panel.border_title = Text(f"{sanitize_legacy_scalar(self._build_title())} [{error_label}]")
        self.add_class("-error")
        with suppress(Exception):
            update_static_in_place(panel, Text(sanitize_source_text(error, tab_size=8)))
        self.add_class("-done")
        self._show_tool_copy_button()


# ---------------------------------------------------------------------------
# GrepToolCall
# ---------------------------------------------------------------------------


class GrepToolCall(_SearchToolCall):
    """Renderer for ``grep`` tool — shows pattern matches with context."""

    def _label_suffix(self) -> str:
        return self.args.get("pattern", "")

    def _build_title(self) -> str:
        pattern = self.args.get("pattern", "")
        glob_filter = self.args.get("glob", "")
        parts = [f'grep "{pattern}"']
        if glob_filter:
            parts.append(f"--glob {glob_filter}")
        return " ".join(parts)

    def _format_result(self, result: str) -> tuple[str, str, str, bool]:
        if result.startswith(("Error:", "No matches")):
            return "", result, "", True

        lines = result.split("\n")
        header = ""
        subtitle = ""
        body_lines = lines

        # Extract header line ("Found N match(es) in /path (limited to N)")
        if lines and _HEADER_RE.match(lines[0]):
            m = _HEADER_PARTS_RE.match(lines[0])
            if m:
                header = m.group(1) + (m.group(3) or "")
                subtitle = m.group(2)
            else:
                header = lines[0]
            body_lines = lines[1:]

        # Strip leading empty lines
        while body_lines and not body_lines[0].strip():
            body_lines = body_lines[1:]

        # Truncate to max blocks (blocks separated by empty lines)
        truncated = False
        blocks: list[list[str]] = []
        current_block: list[str] = []
        for line in body_lines:
            if not line.strip() and current_block:
                blocks.append(current_block)
                current_block = []
                if len(blocks) >= _GREP_MAX_BLOCKS:
                    truncated = True
                    break
            else:
                current_block.append(line)
        if current_block and len(blocks) < _GREP_MAX_BLOCKS:
            blocks.append(current_block)

        body = "\n\n".join("\n".join(b) for b in blocks)
        if truncated:
            body += "\n..."

        return header, body, subtitle, False


# ---------------------------------------------------------------------------
# GlobToolCall
# ---------------------------------------------------------------------------


class GlobToolCall(_SearchToolCall):
    """Renderer for ``glob`` tool — shows matching file list."""

    def _label_suffix(self) -> str:
        return self.args.get("pattern", "")

    def _build_title(self) -> str:
        pattern = self.args.get("pattern", "")
        return f'glob "{pattern}"'

    def _format_result(self, result: str) -> tuple[str, str, str, bool]:
        if result.startswith(("Error:", "No files")):
            return "", result, "", True

        lines = result.split("\n")
        header = ""
        subtitle = ""
        file_lines = lines

        # Extract header line ("Found N file(s) matching 'pattern' in /path (limited to N)")
        if lines and _HEADER_RE.match(lines[0]):
            m = _HEADER_PARTS_RE.match(lines[0])
            if m:
                header = m.group(1) + (m.group(3) or "")
                subtitle = m.group(2)
            else:
                header = lines[0]
            file_lines = lines[1:]

        # Truncate file list
        truncated = False
        if len(file_lines) > _GLOB_MAX_FILES:
            file_lines = file_lines[:_GLOB_MAX_FILES]
            truncated = True

        body = "\n".join(file_lines)
        if truncated:
            body += "\n  ..."

        return header, body, subtitle, False
