# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Renderers for skill tool calls.

The skills provider exposes progressive-disclosure tools:

``load_skill``
    Loads full skill instructions plus Chrys metadata for file-based skills.
``read_skill_resource``
    Reads a named resource attached to a skill.
``run_skill_script``
    Runs a named script attached to a skill.

This renderer keeps those calls compact in the transcript while preserving the
full tool input/output in the copy payload.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import suppress
from html import unescape as html_unescape
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
    TOOL_CARD_RUNNING,
    BaseToolCard,
    ToolCardHeader,
    fmt_duration,
    tool_result_render_status,
)
from chrys.app.tui.widgets.chat.tool_view_builders import TOOL_VIEW_OUTPUT
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.tool_result_metadata import (
    PARTIAL_OUTPUT_LABEL,
    result_text_exit_code,
    result_text_without_exit_code,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

_SKILL_EMPTY_PREVIEW = msg("tui.tool_card.skill.empty_preview", fallback="(empty)")
_SKILL_MORE = msg("tui.tool_card.skill.more", fallback="+{n} more")
_SKILL_RESOURCE_COUNT = msg(
    "tui.tool_card.skill.resource_count",
    fallback="{count} resource",
    plural_fallback="{count} resources",
)
_SKILL_SCRIPT_COUNT = msg(
    "tui.tool_card.skill.script_count",
    fallback="{count} script",
    plural_fallback="{count} scripts",
)
_SKILL_LINE_COUNT = msg(
    "tui.tool_card.skill.line_count",
    fallback="{count} line",
    plural_fallback="{count} lines",
)
_SKILL_CHAR_COUNT = msg(
    "tui.tool_card.skill.char_count",
    fallback="{count} char",
    plural_fallback="{count} chars",
)
_SKILL_TRUNCATED_LINES = msg("tui.tool_card.skill.truncated_lines", fallback="{lines}+ lines")
_SKILL_EMPTY = msg("tui.tool_card.skill.empty", fallback="empty")
_SKILL_DIR = msg("tui.tool_card.skill.dir", fallback="dir: ")
_SKILL_RESOURCES = msg("tui.tool_card.skill.resources", fallback="resources: ")
_SKILL_SCRIPTS = msg("tui.tool_card.skill.scripts", fallback="scripts: ")
_SKILL_EMPTY_INSTRUCTIONS = msg(
    "tui.tool_card.skill.empty_instructions",
    fallback="(empty skill instructions)",
)
_SKILL_LOADING = msg("tui.tool_card.skill.loading", fallback="loading skill")
_SKILL_READING_RESOURCE = msg("tui.tool_card.skill.reading_resource", fallback="reading resource")
_SKILL_RUNNING_SCRIPT = msg("tui.tool_card.skill.running_script", fallback="running script")
_SKILL_LOADED = msg("tui.tool_card.skill.loaded", fallback="loaded")
SKILL_EXIT = msg("tui.tool_card.skill.exit", fallback="exit {code}")
_SKILL_COMPLETED = msg("tui.tool_card.skill.completed", fallback="completed")
_SKILL_ERROR = msg("tui.tool_card.skill.error", fallback="error")
_SKILL_NOUN = msg("tui.tool_card.skill.noun.skill", fallback="skill")
_SKILL_RESOURCE_NOUN = msg("tui.tool_card.skill.noun.resource", fallback="resource")
_SKILL_SCRIPT_NOUN = msg("tui.tool_card.skill.noun.script", fallback="script")

_SKILL_COUNT_DEFINITIONS: dict[str, MessageDef] = {
    "resource": _SKILL_RESOURCE_COUNT,
    "script": _SKILL_SCRIPT_COUNT,
    "line": _SKILL_LINE_COUNT,
    "char": _SKILL_CHAR_COUNT,
}

_MAX_PREVIEW_LINES = 8
"""Max result lines displayed inline in the transcript."""

_MAX_PREVIEW_LINE_CHARS = 220
"""Max characters per displayed result line."""

_MAX_LIST_ITEMS = 4
"""Max resources/scripts shown in the compact metadata line."""

_PARTIAL_OUTPUT_SEPARATOR = f"\n{PARTIAL_OUTPUT_LABEL}\n"
"""Separates a timed-out script's error line from what it printed."""

_SUMMARY_LINE_COUNT_SCAN_CHARS = 64 * 1024
"""Max chars scanned for resource line-count subtitles."""

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)
_INSTRUCTIONS_RE = re.compile(r"<instructions>\s*(.*?)\s*</instructions>", re.DOTALL | re.IGNORECASE)
_SKILL_DIR_RE = re.compile(r"<skill_dir>\s*(.*?)\s*</skill_dir>", re.DOTALL | re.IGNORECASE)
_RESOURCE_RE = re.compile(r"<resource\b[^>]*\bname=(['\"])(.*?)\1[^>]*/?>", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script\b[^>]*\bname=(['\"])(.*?)\1[^>]*/?>", re.IGNORECASE)


def _arg_str(args: dict[str, Any], key: str) -> str:
    """Return a string argument value, or ``""`` for absent/non-string values."""
    value = args.get(key)
    return value if isinstance(value, str) else ""


def _preview_lines(text: str, *, max_lines: int = _MAX_PREVIEW_LINES) -> tuple[list[str], bool]:
    """Return bounded preview lines without materializing the whole payload."""
    if not text:
        return [], False

    lines: list[str] = []
    truncated = False
    pos = 0
    text_len = len(text)
    while pos < text_len and len(lines) < max_lines:
        scan_end = min(text_len, pos + _MAX_PREVIEW_LINE_CHARS + 1)
        newline = text.find("\n", pos, scan_end)
        if newline >= 0:
            lines.append(text[pos:newline].removesuffix("\r"))
            pos = newline + 1
            continue

        chunk = text[pos:scan_end]
        if scan_end >= text_len:
            if len(chunk) > _MAX_PREVIEW_LINE_CHARS:
                lines.append(chunk[:_MAX_PREVIEW_LINE_CHARS] + "...")
                truncated = True
            else:
                lines.append(chunk.removesuffix("\r"))
            pos = text_len
            break

        lines.append(text[pos : pos + _MAX_PREVIEW_LINE_CHARS].removesuffix("\r") + "...")
        truncated = True
        next_newline = text.find("\n", scan_end)
        pos = text_len if next_newline < 0 else next_newline + 1

    if pos < text_len:
        truncated = True
    return lines, truncated


def _append_preview(
    t: Text,
    text: str,
    *,
    style: str = "",
    render_message: Callable[[MessageRef], str] = format_message,
) -> None:
    """Append a bounded preview to *t*."""
    lines, truncated = _preview_lines(text)
    if not lines:
        t.append(render_message(_SKILL_EMPTY_PREVIEW.bind()), style="dim")
        return
    t.append(sanitize_source_text("\n".join(lines), tab_size=8), style=style)
    if truncated:
        t.append("\n...", style="dim")


def _format_list(
    items: list[str],
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Format a compact list of metadata names."""
    if len(items) <= _MAX_LIST_ITEMS:
        return ", ".join(items)
    shown = ", ".join(items[:_MAX_LIST_ITEMS])
    return f"{shown}, {render_message(_SKILL_MORE.bind(n=len(items) - _MAX_LIST_ITEMS))}"


def _count_label(
    count: int,
    singular: str,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Return a compact count label."""
    return render_message(_SKILL_COUNT_DEFINITIONS[singular].bind(count=count))


def _line_char_summary(
    text: str,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Return a compact line/char count for resource output."""
    if not text:
        return render_message(_SKILL_EMPTY.bind())
    text_len = len(text)
    scan_end = min(text_len, _SUMMARY_LINE_COUNT_SCAN_CHARS)
    newline_count = text.count("\n", 0, scan_end)
    if scan_end == text_len:
        lines = newline_count + (0 if text.endswith("\n") else 1)
        line_label = _count_label(lines, "line", render_message)
    else:
        lines = newline_count if scan_end > 0 and text[scan_end - 1] == "\n" else newline_count + 1
        line_label = render_message(_SKILL_TRUNCATED_LINES.bind(lines=lines))
    return f"{line_label}, {_count_label(text_len, 'char', render_message)}"


def _extract_names(pattern: re.Pattern[str], result: str) -> list[str]:
    """Extract XML-ish name attributes from provider metadata."""
    return [html_unescape(match.group(2)) for match in pattern.finditer(result)]


def _extract_skill_dir(result: str) -> str:
    """Extract the file-based skill directory from load_skill metadata."""
    match = None
    for candidate in _SKILL_DIR_RE.finditer(result):
        match = candidate
    if not match:
        return ""
    return html_unescape(match.group(1).strip())


def _load_skill_metadata_source(result: str) -> str:
    """Return only the provider-appended metadata region for a load_skill result."""
    match = None
    for candidate in _SKILL_DIR_RE.finditer(result):
        match = candidate
    if match is not None:
        return result[match.end() :]
    if instructions := _INSTRUCTIONS_RE.search(result):
        return result[instructions.end() :]
    return result


def _strip_skill_metadata(result: str) -> str:
    """Return the human-facing instruction body from a load_skill result."""
    if match := _INSTRUCTIONS_RE.search(result):
        return match.group(1).strip()

    match = None
    for candidate in _SKILL_DIR_RE.finditer(result):
        match = candidate
    if match is not None:
        return _FRONTMATTER_RE.sub("", result[: match.start()]).strip()
    return _FRONTMATTER_RE.sub("", result).strip()


def _parse_exit_code(result: str) -> int | None:
    """Return a trailing script exit code, if present."""
    return result_text_exit_code(result)


def _script_output_body(result: str) -> str:
    """Remove the trailing exit-code marker from displayed script output."""
    return result_text_without_exit_code(result)


class SkillToolCall(BaseToolCard):
    """Rich renderer for skill-related tool calls."""

    DEFAULT_CSS = """
    SkillToolCall {
        height: auto;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    SkillToolCall #skill-label {
        height: auto;
    }
    SkillToolCall > #skill-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-accent 50%;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-align: right;
        border-subtitle-color: $text-muted;
        padding: 0 1;
        color: $text-muted;
    }
    SkillToolCall.-success > #skill-panel {
        border: round $tui-border-success 30%;
        border-title-color: $success;
        border-subtitle-color: $success;
        border-title-style: not bold;
    }
    SkillToolCall.-error > #skill-panel {
        border: round $tui-border-error 50%;
        border-title-color: $text-error;
        border-subtitle-color: $text-error;
        border-title-style: not bold;
    }
    SkillToolCall.-rejected > #skill-panel {
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
        self._skill_name = _arg_str(self.args, "skill_name")
        self._resource_name = _arg_str(self.args, "resource_name")
        self._script_name = _arg_str(self.args, "script_name")

    def _label_suffix(self) -> str:
        """Return compact identifying text for the label."""
        if self.tool_name == "load_skill":
            return self._skill_name
        if self.tool_name == "read_skill_resource":
            if self._skill_name and self._resource_name:
                return f"{self._skill_name}/{self._resource_name}"
            return self._skill_name or self._resource_name
        if self.tool_name == "run_skill_script":
            if self._skill_name and self._script_name:
                return f"{self._skill_name}/{self._script_name}"
            return self._skill_name or self._script_name
        return self._skill_name

    def _label_text(self, duration_ms: int = 0) -> Text:
        t = Text()
        t.append("• ", style="bold")
        t.append(self.tool_name, style="bold")
        if duration_ms:
            t.append(f" ({fmt_duration(duration_ms)})", style="dim")
        if suffix := self._label_suffix():
            t.append(f" {sanitize_legacy_scalar(suffix)}", style="dim")
        return t

    def _panel_title(self) -> str:
        """Return the initial panel title."""
        if self.tool_name == "load_skill":
            return self._skill_name or self._render_message(_SKILL_NOUN.bind())
        if self.tool_name == "read_skill_resource":
            return self._resource_name or self._skill_name or self._render_message(_SKILL_RESOURCE_NOUN.bind())
        if self.tool_name == "run_skill_script":
            return self._script_name or self._skill_name or self._render_message(_SKILL_SCRIPT_NOUN.bind())
        return self.tool_name

    def compose(self) -> ComposeResult:
        yield ToolCardHeader(self._label_text(), id="skill-label")
        # A single bordered Static is the whole result panel: spinner while
        # running, the formatted result preview once done.
        panel = Static(self._render_spinner(), id="skill-panel")
        panel.border_title = Text(sanitize_legacy_scalar(self._panel_title()))
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
                update_static_in_place(self.query_one("#skill-panel", Static), self._render_spinner())

    def _spinner_verb(self) -> str:
        """Return the verb used in the running spinner."""
        if self.tool_name == "load_skill":
            return self._render_message(_SKILL_LOADING.bind())
        if self.tool_name == "read_skill_resource":
            return self._render_message(_SKILL_READING_RESOURCE.bind())
        if self.tool_name == "run_skill_script":
            return self._render_message(_SKILL_RUNNING_SCRIPT.bind())
        return self._render_message(TOOL_CARD_RUNNING.bind())

    def _render_spinner(self) -> Text:
        t = Text()
        t.append(f"{self._SPINNERS[self._spin_idx]} ", style="yellow")
        t.append(self._spinner_verb(), style="yellow")
        return t

    def _format_load_skill_result(self, result: str) -> tuple[Text, str, bool]:
        """Return ``(display, subtitle, is_error)`` for load_skill."""
        if result.startswith("Error:"):
            return Text(sanitize_source_text(result, tab_size=8)), self._render_message(_SKILL_ERROR.bind()), True

        skill_dir = _extract_skill_dir(result)
        metadata = _load_skill_metadata_source(result)
        resources = _extract_names(_RESOURCE_RE, metadata)
        scripts = _extract_names(_SCRIPT_RE, metadata)
        body = _strip_skill_metadata(result)

        subtitle_parts: list[str] = []
        if resources:
            subtitle_parts.append(_count_label(len(resources), "resource", self._render_message))
        if scripts:
            subtitle_parts.append(_count_label(len(scripts), "script", self._render_message))
        subtitle = ", ".join(subtitle_parts) if subtitle_parts else self._render_message(_SKILL_LOADED.bind())

        display = Text()
        if skill_dir:
            display.append(self._render_message(_SKILL_DIR.bind()), style="dim")
            display.append(sanitize_legacy_scalar(skill_dir), style="cyan")
        if resources:
            if display:
                display.append("\n")
            display.append(self._render_message(_SKILL_RESOURCES.bind()), style="dim")
            display.append(sanitize_legacy_scalar(_format_list(resources, self._render_message)), style="cyan")
        if scripts:
            if display:
                display.append("\n")
            display.append(self._render_message(_SKILL_SCRIPTS.bind()), style="dim")
            display.append(sanitize_legacy_scalar(_format_list(scripts, self._render_message)), style="cyan")
        if display and body:
            display.append("\n\n")
        if body:
            _append_preview(display, body, render_message=self._render_message)
        elif not display:
            display.append(self._render_message(_SKILL_EMPTY_INSTRUCTIONS.bind()), style="dim")

        return display, subtitle, False

    def _format_resource_result(self, result: str) -> tuple[Text, str, bool]:
        """Return ``(display, subtitle, is_error)`` for resource reads."""
        if result.startswith("Error:"):
            return Text(sanitize_source_text(result, tab_size=8)), self._render_message(_SKILL_ERROR.bind()), True

        display = Text()
        _append_preview(display, result, render_message=self._render_message)
        return display, _line_char_summary(result, self._render_message), False

    def _format_script_result(self, result: str) -> tuple[Text, str, bool]:
        """Return ``(display, subtitle, is_error)`` for script execution."""
        if result.startswith("Error:"):
            message, label, output = result.partition(_PARTIAL_OUTPUT_SEPARATOR)
            display = Text(sanitize_source_text(message, tab_size=8))
            if label:
                display.append("\n")
                _append_preview(display, output, render_message=self._render_message)
            return display, self._render_message(_SKILL_ERROR.bind()), True

        exit_code = _parse_exit_code(result)
        output_body = _script_output_body(result)
        display = Text()
        _append_preview(display, output_body, render_message=self._render_message)

        if exit_code is not None and exit_code != 0:
            return display, self._render_message(SKILL_EXIT.bind(code=exit_code)), True
        if exit_code is not None:
            return display, self._render_message(SKILL_EXIT.bind(code=exit_code)), False
        return display, self._render_message(_SKILL_COMPLETED.bind()), False

    def _format_result(self, result: str) -> tuple[Text, str, bool]:
        """Return ``(display, subtitle, is_error)`` for the current skill tool."""
        if self.tool_name == "load_skill":
            return self._format_load_skill_result(result)
        if self.tool_name == "read_skill_resource":
            return self._format_resource_result(result)
        if self.tool_name == "run_skill_script":
            return self._format_script_result(result)
        return (
            Text(sanitize_source_text(result, tab_size=8)),
            self._render_message(_SKILL_COMPLETED.bind()),
            result.startswith("Error:"),
        )

    def _tool_copy_sections(self) -> list[tuple[str, str, str]]:
        """Use a more helpful language hint for copied skill instructions."""
        language = "markdown" if self.tool_name == "load_skill" else "text"
        return [
            (
                self._render_message(TOOL_VIEW_OUTPUT.bind()),
                language,
                self.result_text or self._render_message(_SKILL_EMPTY_PREVIEW.bind()),
            )
        ]

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        """Mark this skill tool call complete."""
        self.result_text = result
        self.duration_ms = duration_ms
        self.approval = kwargs.get("approval")
        metadata = kwargs.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.status = "complete"
        if self._timer is not None:
            self._timer.stop()

        with suppress(Exception):
            self.query_one("#skill-label", Static).update(self._label_text(duration_ms))

        display, subtitle, is_error = self._format_result(result)
        render_status = tool_result_render_status(
            result,
            self.approval,
            self.tool_kind,
            self.metadata,
            self.tool_name,
            parsed_error=is_error,
        )
        if render_status != "error" and subtitle == self._render_message(_SKILL_ERROR.bind()):
            subtitle = self._render_message(_SKILL_COMPLETED.bind())
        panel = self.query_one("#skill-panel", Static)

        if render_status == "rejected":
            self.status = "rejected"
            panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_REJECTED.bind())
            self.add_class("-rejected")
        elif render_status == "error":
            self.status = "error"
            panel.border_subtitle = Text(subtitle)
            self.add_class("-error")
        else:
            panel.border_subtitle = Text(subtitle)
            self.add_class("-success")

        with suppress(Exception):
            update_static_in_place(panel, display)
        self.add_class("-done")
        self._show_tool_copy_button()

    def set_error(self, error: str) -> None:
        """Mark this skill tool call failed."""
        self.result_text = error
        self.status = "error"
        if self._timer is not None:
            self._timer.stop()

        panel = self.query_one("#skill-panel", Static)
        panel.border_title = Text(sanitize_legacy_scalar(self._panel_title()))
        panel.border_subtitle = render_text(widget_localizer(self), _SKILL_ERROR.bind())
        self.add_class("-error")
        with suppress(Exception):
            update_static_in_place(panel, Text(sanitize_source_text(error, tab_size=8)))
        self.add_class("-done")
        self._show_tool_copy_button()

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)
