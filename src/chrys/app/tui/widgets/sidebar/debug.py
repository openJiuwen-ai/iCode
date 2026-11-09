# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DebugPanel — real-time event stream viewer for debugging.

Color-coded by event category:
  cyan    — user events (UserMessage, UserInterrupt, ...)
  green   — agent events (AgentThinking, InvocationMessage)
  yellow  — tool events (InvocationToolCallStart, InvocationToolCallResult)
  magenta — context events (ContextCompressed, UsageUpdate)
  red     — errors
  dim     — system events (SessionReady, StateChanged)
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self

from rich.text import Text
from textual.widget import Widget
from textual.widgets import RichLog, Static

from chrys.app.tui.clipboard import copy_text_to_clipboards
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Click

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import MessageRef


_MAX_LOG_LINES = 1000
"""Maximum lines retained in the debug event log."""

_EVENT_LABEL_MAX = 15
"""Max width for event type labels in the log."""

_EVENT_STREAM_COPIED = msg("tui.debug.copy.event_stream", fallback="Event stream copied")
_EVENT_STREAM_TITLE = msg("tui.sidebar.debug.event_stream", fallback="Event Stream")

# Event type → (short label, color)
_EVENT_STYLES: dict[str, tuple[str, str]] = {
    "UserMessage": ("UserMsg", "cyan"),
    "UserInterrupt": ("Interrupt", "cyan bold"),
    "UserInject": ("Inject", "cyan"),
    "UserInjectCancel": ("InjectCancel", "cyan"),
    "ApprovalResponse": ("Approval", "cyan"),
    "AgentProfileSwitch": ("Profile", "cyan"),
    "SessionReady": ("SessionReady", "dim"),
    "AgentThinking": ("Thinking", "green"),
    "InvocationMessage": ("AgentMsg", "green"),
    "InvocationToolCallStart": ("ToolStart", "yellow"),
    "InvocationToolCallArgsUpdated": ("ToolArgsUpdated", "yellow"),
    "InvocationToolCallResult": ("ToolResult", "yellow"),
    "ApprovalRequest": ("NeedApproval", "yellow bold"),
    "ToolCompacted:phase1": ("Compact:Phase1", "deep_pink4"),
    "ToolCompacted:phase2": ("Compact:Phase2", "deep_pink4"),
    "ToolCompacted:phase4": ("Compact:Phase4", "deep_pink4"),
    "ToolCompacted": ("Compacted", "magenta"),
    "ContextCompressed:auto": ("AutoCompress", "magenta bold"),
    "ContextCompressed": ("Compress", "magenta"),
    "UsageUpdate": ("Usage", "magenta"),
    "TodoListUpdated": ("Todos", "blue"),
    "StateChanged": ("State", "dim"),
    "Error": ("ERROR", "red bold"),
    "ShellCommand": ("Shell", "yellow bold"),
    "RollbackResult": ("Rollback", "cyan bold"),
}


class _ClickableLog(RichLog, can_focus=False):
    """RichLog that copies all entries to clipboard on click."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._plain_lines: deque[str] = deque(maxlen=self.max_lines)

    def write_with_text(self, renderable: Text, plain: str) -> None:
        """Write a Rich Text and track its plain-text representation."""
        self._plain_lines.append(plain)
        self.write(renderable)

    def on_click(self, event: Click) -> None:
        if self._plain_lines:
            payload = "\n".join(self._plain_lines)
            copy_text_to_clipboards(self.app, payload)
            localizer = widget_localizer(self)
            self.notify(
                render_str(localizer, _EVENT_STREAM_COPIED.bind()),
                title=render_str(localizer, COPIED_TITLE.bind()),
                timeout=1,
                markup=False,
            )

    def clear(self) -> Self:
        self._plain_lines.clear()
        return super().clear()


class DebugPanel(Widget, can_focus=False):
    """Scrollable event log for real-time debugging.

    Call ``log_event()`` to append a color-coded entry.
    """

    DEFAULT_CSS = """
    DebugPanel {
        height: 100%;
        width: 100%;
    }
    DebugPanel > Static {
        height: 1;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    DebugPanel > _ClickableLog {
        height: 1fr;
        border: round $tui-border-primary-darken-3 $inner-border-opacity;
        scrollbar-size: 1 1;
        overflow-x: auto;
        scrollbar-size-horizontal: 0;
    }
    SidebarPanel.-shell-active DebugPanel > _ClickableLog {
        border: round $tui-border-warning-darken-1 $inner-border-opacity;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._locale_controller = locale_controller

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        if controller is None:
            return render_str(widget_localizer(self), reference)
        return render_str(controller.localizer, reference)

    def compose(self) -> ComposeResult:
        yield Static(Text(self._render_message(_EVENT_STREAM_TITLE.bind())), classes="debug-title")
        yield _ClickableLog(id="debug-log", max_lines=_MAX_LOG_LINES, markup=True, wrap=False)

    def refresh_localization(self) -> None:
        """Re-render the localized title after a locale switch."""
        if not self.is_mounted:
            return
        title = self.query_one(".debug-title", Static)
        title.update(Text(self._render_message(_EVENT_STREAM_TITLE.bind())))

    def log_event(self, event_type: str, detail: str = "", ts: datetime | None = None) -> None:
        """Append a color-coded event entry."""
        log = self.query_one("#debug-log", _ClickableLog)
        timestamp = (ts or datetime.now(tz=UTC).astimezone()).strftime("%H:%M:%S")
        if event_type.startswith("Usage["):
            label, color = (_short_event_label(event_type), "magenta")
        else:
            label, color = _EVENT_STYLES.get(event_type, (_short_event_label(event_type), "dim"))

        t = Text()
        t.append(f"{timestamp} ", style="dim")
        t.append(f"{label:<{_EVENT_LABEL_MAX}}", style=color)
        if detail:
            t.append(f" {detail}", style="dim")

        plain = f"{timestamp} {label:<{_EVENT_LABEL_MAX}}"
        if detail:
            plain += f" {detail}"
        log.write_with_text(t, plain)

    def log_raw(self, text: str) -> None:
        """Append raw text (for conversation logs, etc.)."""
        log = self.query_one("#debug-log", _ClickableLog)
        log.write_with_text(Text(text, style="dim"), text)


def _short_event_label(event_type: str) -> str:
    """Return an event label sized for the debug stream column."""
    if len(event_type) <= _EVENT_LABEL_MAX:
        return event_type
    # ``Usage[<profile>]`` labels lose disambiguating value when the closing
    # bracket gets truncated.  Shrink the profile name inside the brackets
    # instead so the suffix stays attached.
    if event_type.startswith("Usage[") and event_type.endswith("]"):
        prefix = "Usage["
        suffix = "]"
        inner = event_type[len(prefix) : -len(suffix)]
        max_inner = _EVENT_LABEL_MAX - len(prefix) - len(suffix) - 1
        if max_inner > 0:
            return f"{prefix}{inner[:max_inner]}…{suffix}"
    return event_type[: _EVENT_LABEL_MAX - 1] + "…"
