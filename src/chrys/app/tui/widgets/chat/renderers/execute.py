# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Renderer for shell execution tool calls.

Shows a ``• toolname`` label followed by two bordered panels:
one for the command input (dim) and one for the output (colour-coded).

While running, streams up to 10 lines of live output inside the output
panel and displays elapsed time alongside the configured timeout.
"""

from __future__ import annotations

import json
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.util.static_update import update_static_in_place
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.chat.tool_call import (
    TOOL_CARD_COMPLETED,
    TOOL_CARD_ERRORED,
    TOOL_CARD_ERRORED_WITH_CODE,
    TOOL_CARD_REJECTED,
    TOOL_CARD_RUNNING,
    BaseToolCard,
    ToolCardHeader,
    fmt_duration,
    tool_result_render_status,
)
from chrys.app.tui.widgets.chat.tool_view_builders import build_code_view, build_params_view
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.tool_kinds import KIND_SHELL
from chrys.foundation.tool_result_metadata import (
    SHELL_BACKGROUND_TASK_METADATA_KEY,
    result_text_exit_code,
    result_text_without_exit_code,
    shell_exit_code_from_metadata,
    shell_timed_out_from_metadata,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

_EXECUTE_OUTPUT = msg("tui.tool_card.execute.output", fallback="Output")
_EXECUTE_NO_COMMAND = msg("tui.tool_card.execute.no_command", fallback="(no command)")
_EXECUTE_TIMEOUT = msg("tui.tool_card.execute.timeout", fallback="timeout {duration}")
_EXECUTE_BACKGROUND_HINT = msg("tui.tool_card.execute.background_hint", fallback="{key} moves it to the background")
_EXECUTE_BACKGROUNDED = msg(
    "tui.tool_card.execute.backgrounded",
    fallback="→ in the background as {task}, pid {pid}",
)

_OUTPUT_MAX_LINES = 5
"""Max lines of output to show after completion."""

_PROGRESS_MAX_LINES = 10
"""Max live-streaming lines shown while running (rolling window)."""

_PROGRESS_MAX_CHARS = 512
"""Max characters per progress line before truncation."""

_DEFAULT_TIMEOUT_SECONDS = 30
"""Fallback shell timeout shown when replayed args contain an invalid value."""


def _coerce_display_text(value: Any) -> str:
    """Return a safe string for persisted shell argument values."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_timeout_seconds(value: Any) -> int:
    """Return a non-negative timeout value from external/replayed tool args."""
    if isinstance(value, bool):
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, int):
        return value if value >= 0 else _DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, float):
        with suppress(ValueError, OverflowError):
            timeout = int(value)
            return timeout if timeout >= 0 else _DEFAULT_TIMEOUT_SECONDS
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return _DEFAULT_TIMEOUT_SECONDS
        with suppress(ValueError, OverflowError):
            timeout = int(stripped)
            return timeout if timeout >= 0 else _DEFAULT_TIMEOUT_SECONDS
        with suppress(ValueError, OverflowError):
            timeout = int(float(stripped))
            return timeout if timeout >= 0 else _DEFAULT_TIMEOUT_SECONDS
    return _DEFAULT_TIMEOUT_SECONDS


def _fmt_duration(seconds: int) -> str:
    """Format seconds into a compact human-readable string."""
    if seconds >= 86400:
        days, remainder = divmod(seconds, 86400)
        return f"{days}d {_fmt_duration(remainder)}" if remainder else f"{days}d"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m" if s == 0 else f"{m}m {s}s"
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if m == 0 and s == 0:
        return f"{h}h"
    if s == 0:
        return f"{h}h {m}m"
    return f"{h}h {m}m {s}s"


def _parse_result(result: str) -> tuple[str, int | None]:
    """Split result into (output_body, exit_code)."""
    exit_code = result_text_exit_code(result)
    if exit_code is None:
        return result, None
    return result_text_without_exit_code(result), exit_code


def _shell_exit_code(result: str, metadata: dict[str, Any] | None) -> int | None:
    """Return structured shell exit code, falling back to legacy text suffixes."""
    if shell_timed_out_from_metadata(metadata):
        return None
    exit_code = shell_exit_code_from_metadata(metadata)
    if exit_code is not None:
        return exit_code
    return result_text_exit_code(result)


class ExecuteToolCall(BaseToolCard):
    """Rich renderer for shell execution tool calls."""

    DEFAULT_CSS = """
    ExecuteToolCall {
        height: auto;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    ExecuteToolCall #exec-label {
        height: auto;
    }
    ExecuteToolCall > #exec-cmd {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-warning 50%;
        border-title-color: $text;
        border-title-style: not bold;
        padding: 0 1;
        color: $text-muted;
    }
    ExecuteToolCall > #exec-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-title-style: bold;
        border-subtitle-align: right;
        border-subtitle-color: $warning;
        border-subtitle-style: bold;

        padding: 0 1;
        color: $text-muted;
    }
    ExecuteToolCall.-success > #exec-panel {
        border: round $tui-border-success 30%;
        border-title-color: $success;
        border-title-style: not bold;
        border-subtitle-color: $success;
        border-subtitle-style: not bold;
    }
    ExecuteToolCall.-error > #exec-panel {
        border: round $tui-border-error 50%;
        border-title-color: $text-error;
        border-title-style: not bold;
        border-subtitle-color: $text-error;
        border-subtitle-style: not bold;
    }
    ExecuteToolCall.-rejected > #exec-panel, ExecuteToolCall.-backgrounded > #exec-panel {
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-title-style: not bold;
        border-subtitle-color: $warning;
        border-subtitle-style: not bold;
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
        self._command = _coerce_display_text(self.args.get("command", ""))
        self._start_time = time.monotonic()
        self._timeout: int = _coerce_timeout_seconds(self.args.get("timeout", _DEFAULT_TIMEOUT_SECONDS))
        self._progress_lines: list[str] = []
        self._last_elapsed_sec = -1
        self._timer: Timer | None = None
        self.offers_background = False
        """Whether the running label says how to move the command to the background (main-agent commands)."""

    def _label_text(self, duration_ms: int = 0) -> Text:
        t = Text()
        t.append("• ", style="bold")
        t.append(self.tool_name, style="bold")
        if duration_ms:
            t.append(f" ({fmt_duration(duration_ms)})", style="dim")
        return t

    def _running_label_text(self) -> Text:
        """Label text with elapsed time and timeout while running."""
        t = Text()
        t.append("• ", style="bold")
        t.append(self.tool_name, style="bold")
        elapsed = int(time.monotonic() - self._start_time)
        timeout_label = render_str(
            widget_localizer(self),
            _EXECUTE_TIMEOUT.bind(duration=_fmt_duration(self._timeout)),
        )
        hint = (
            " • " + render_str(widget_localizer(self), _EXECUTE_BACKGROUND_HINT.bind(key="ctrl+t"))
            if self.offers_background
            else ""
        )
        t.append(
            f" ({_fmt_duration(elapsed)} • {timeout_label}{hint})",
            style="dim",
        )
        return t

    def compose(self) -> ComposeResult:
        yield ToolCardHeader(self._running_label_text(), id="exec-label")

        # Command input panel (dim border) — a single bordered Static.
        if self._command:
            cmd_panel = Static(Text(sanitize_source_text(self._command, tab_size=8)), id="exec-cmd")
            reason = _coerce_display_text(self.args.get("reason", ""))
            if reason:
                cmd_panel.border_title = Text(sanitize_legacy_scalar(reason))
            yield cmd_panel

        # Output panel — a single bordered Static: streamed progress tail
        # while running, the final output once done.
        panel = Static(self._progress_text() if self._progress_lines else "", id="exec-panel")
        panel.border_title = render_text(widget_localizer(self), _EXECUTE_OUTPUT.bind())
        panel.border_subtitle = self._spinner_title()
        yield panel

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._spin)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def _progress_text(self) -> Text:
        """Build a no-wrap Text from current progress lines (auto-clipped to widget width)."""
        return Text(
            sanitize_source_text("\n".join(self._progress_lines), tab_size=8),
            style="dim",
            no_wrap=True,
            overflow="ellipsis",
        )

    def _spin(self) -> None:
        if self.status == "running":
            self._spin_idx = (self._spin_idx + 1) % len(self._SPINNERS)
            # Only a shown card repaints (see ``ToolCall._spin``). A skipped tick leaves
            # ``_last_elapsed_sec`` alone, so the first tick after the card shows repaints the
            # elapsed label too.
            if not is_widget_shown_on_active_screen(self):
                return
            with suppress(Exception):
                self.query_one("#exec-panel", Static).border_subtitle = self._spinner_title()
            # The progress tail changes only in update_progress, which paints it at once.
            # Update label with elapsed time (only when the displayed second changes)
            elapsed = int(time.monotonic() - self._start_time)
            if elapsed != self._last_elapsed_sec:
                self._last_elapsed_sec = elapsed
                with suppress(Exception):
                    self.query_one("#exec-label", Static).update(self._running_label_text())

    def _spinner_title(self) -> Text:
        """Border title with animated spinner for the output panel."""
        t = Text()
        t.append(f"{self._SPINNERS[self._spin_idx]} ", style="yellow")
        t.append(render_str(widget_localizer(self), TOOL_CARD_RUNNING.bind()), style="yellow")
        return t

    def update_progress(self, lines: list[str]) -> None:
        """Add streaming output lines (rolling window)."""
        for line in lines:
            if len(line) > _PROGRESS_MAX_CHARS:
                line = line[:_PROGRESS_MAX_CHARS] + "…"
            self._progress_lines.append(line)
        if len(self._progress_lines) > _PROGRESS_MAX_LINES:
            self._progress_lines = self._progress_lines[-_PROGRESS_MAX_LINES:]
        self._paint_progress_tail()

    def _paint_progress_tail(self) -> None:
        """Show the streamed tail now; a full rolling tail keeps the panel's height."""
        with suppress(Exception):
            update_static_in_place(self.query_one("#exec-panel", Static), self._progress_text())

    def compact_display_state(self) -> dict[str, Any] | None:
        """Return the streamed output tail needed to rebuild the visible card."""
        if not self._progress_lines:
            return None
        return {"progress_lines": list(self._progress_lines)}

    def restore_compact_display_state(self, state: dict[str, Any]) -> None:
        """Restore streamed output tail before terminal rendering."""
        raw_lines = state.get("progress_lines")
        if not isinstance(raw_lines, list):
            return
        lines: list[str] = []
        for raw_line in raw_lines:
            line = _coerce_display_text(raw_line)
            if len(line) > _PROGRESS_MAX_CHARS:
                line = line[:_PROGRESS_MAX_CHARS] + "…"
            lines.append(line)
        self._progress_lines = lines[-_PROGRESS_MAX_LINES:]
        if self.status == "running" and self._progress_lines:
            self._paint_progress_tail()

    def _tool_view_input_widgets(self) -> list[Widget]:
        """Show the shell command plus any remaining tool input fields.

        Handles the replay path where only a JSON ``args_summary`` is present.
        """
        dark = self._view_dark()
        args = self.args
        if not args and self.args_summary:
            try:
                parsed = json.loads(self.args_summary)
            except TypeError, ValueError:
                parsed = None
            if isinstance(parsed, dict):
                args = parsed
            else:
                return [build_code_view("text", self.args_summary, dark=dark)]

        command = _coerce_display_text(args.get("command", "")) or self._command
        widgets: list[Widget] = [
            build_code_view(
                "bash", command or render_str(widget_localizer(self), _EXECUTE_NO_COMMAND.bind()), dark=dark
            )
        ]
        extra = {key: value for key, value in args.items() if key != "command"}
        if extra:
            widgets.extend(build_params_view(extra, dark=dark))
        return widgets

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        """Mark as complete."""
        self.result_text = result
        self.duration_ms = duration_ms
        self.approval = kwargs.get("approval")
        metadata = kwargs.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        if shell_timed_out_from_metadata(self.metadata):
            output_body, parsed_exit_code = result, None
        else:
            output_body, parsed_exit_code = _parse_result(result)
        exit_code = _shell_exit_code(result, self.metadata)
        if exit_code is None:
            exit_code = parsed_exit_code
        render_status = tool_result_render_status(result, self.approval, KIND_SHELL, self.metadata)
        is_error = render_status == "error"

        self.status = render_status
        if self._timer is not None:
            self._timer.stop()

        # Update label with duration
        with suppress(Exception):
            self.query_one("#exec-label", Static).update(self._label_text(duration_ms))

        # Apply styling and border subtitle based on approval / exit code
        panel = self.query_one("#exec-panel", Static)
        background = self.metadata.get(SHELL_BACKGROUND_TASK_METADATA_KEY)
        backgrounded = (
            render_text(
                widget_localizer(self),
                _EXECUTE_BACKGROUNDED.bind(task=str(background.get("id")), pid=str(background.get("pid"))),
            )
            if isinstance(background, dict)
            else None
        )
        if render_status == "rejected":
            panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_REJECTED.bind())
            self.add_class("-rejected")
        elif is_error:
            definition = (
                TOOL_CARD_ERRORED_WITH_CODE.bind(code=f"[{exit_code}]")
                if exit_code is not None
                else TOOL_CARD_ERRORED.bind()
            )
            errored = render_text(widget_localizer(self), definition)
            # Its shell failed, but what it started still moved to the background.
            panel.border_subtitle = errored if backgrounded is None else Text(" ").join((errored, backgrounded))
            self.add_class("-error")
        elif backgrounded is not None:
            panel.border_subtitle = backgrounded
            self.add_class("-backgrounded")
        else:
            panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_COMPLETED.bind())
            self.add_class("-success")

        # Keep the streamed output tail when available. Timeout/internal
        # shell-tool errors have no exit code and need the final Error line
        # to be visible instead of stale progress.
        if self._progress_lines and not (is_error and exit_code is None):
            display_text = "\n".join(self._progress_lines)
        else:
            lines = output_body.split("\n")
            if len(lines) > _OUTPUT_MAX_LINES:
                display_text = "\n".join(lines[:_OUTPUT_MAX_LINES]) + "\n..."
            else:
                display_text = output_body

        with suppress(Exception):
            update_static_in_place(
                panel, Text(sanitize_source_text(display_text, tab_size=8), no_wrap=True, overflow="ellipsis")
            )
        self.add_class("-done")
        self._show_tool_copy_button()

    def set_error(self, error: str) -> None:
        """Mark as failed."""
        self.result_text = error
        self.status = "error"
        if self._timer is not None:
            self._timer.stop()

        panel = self.query_one("#exec-panel", Static)
        panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_ERRORED.bind())
        self.add_class("-error")
        with suppress(Exception):
            update_static_in_place(panel, Text(sanitize_source_text(error, tab_size=8)))
        self.add_class("-done")
        self._show_tool_copy_button()
