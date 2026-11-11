# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Renderer for sub-agent tool calls with a reusable nested transcript."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Button, Static

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.util.invocation_progress import invocation_progress_parts
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    AgentTranscriptJournal,
    AgentTranscriptSurface,
    TranscriptAnswerWithdrawnOp,
    TranscriptAssistantOp,
    TranscriptCompactionFinishedOp,
    TranscriptCompactionStartOp,
    TranscriptPausedOp,
    TranscriptResumedOp,
    TranscriptToolArgsOp,
    TranscriptToolProgressOp,
    TranscriptToolResultOp,
    TranscriptToolStartOp,
    TranscriptToolStatusOp,
)
from chrys.app.tui.widgets.chat.renderers.sleep import SleepSkipClicked
from chrys.app.tui.widgets.chat.tool_call import (
    TOOL_CARD_COMPLETED,
    TOOL_CARD_ERRORED,
    TOOL_CARD_INTERRUPTED,
    TOOL_CARD_REJECTED,
    BaseToolCard,
    ToolCardHeader,
    fmt_duration,
    tool_activity_reference,
    tool_result_render_status,
)
from chrys.app.tui.widgets.chat.tool_view_builders import (
    TOOL_VIEW_EMPTY,
    TOOL_VIEW_OUTPUT,
    build_code_view,
    build_params_view,
)
from chrys.app.tui.widgets.loading import RunningTitle
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.tool_kinds import KIND_SLEEP, KIND_SUB_AGENT
from chrys.foundation.tool_result_metadata import TOOL_INTERRUPTED_METADATA_KEY
from chrys.foundation.util.sub_agent_context import (
    SUB_AGENT_PROGRESS_METADATA_KEY,
    SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY,
    SubAgentProgressTotals,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.session.sub_agent_transcript import PersistedSubAgentTranscript

_SUB_AGENT_TASK = msg("tui.tool_card.sub_agent.task", fallback="Task")
_SUB_AGENT_TASK_PROMPT = msg("tui.tool_card.sub_agent.task_prompt", fallback="Task Prompt")
_SUB_AGENT_DURATION = msg("tui.tool_card.sub_agent.duration", fallback="Duration: {duration}")
_SUB_AGENT_RETRYING = msg(
    "tui.tool_card.sub_agent.retrying",
    fallback="↻ Retrying in {delay_seconds}s ({attempt}/{max_attempts}): {message}",
)
_SUB_AGENT_REASON_STREAM_STALLED = msg(
    "tui.tool_card.sub_agent.reason.stream_stalled",
    fallback="Stream stalled",
)
_SUB_AGENT_REASON_COMPACTION_FAILED = msg(
    "tui.tool_card.sub_agent.reason.compaction_failed",
    fallback="Compaction failed",
)
_SUB_AGENT_REASON_FAILED = msg("tui.tool_card.sub_agent.reason.failed", fallback="Sub-agent failed")
_SUB_AGENT_REASON_ACP_INTERRUPTED = msg(
    "tui.tool_card.sub_agent.reason.acp_interrupted",
    fallback="External ACP transport interrupted",
)
_SUB_AGENT_REASON_PAUSED = msg("tui.tool_card.sub_agent.reason.paused", fallback="Sub-agent paused")
_SUB_AGENT_AFTER_RETRIES = msg(
    "tui.tool_card.sub_agent.after_retries",
    fallback="(after {count} auto-retry attempt)",
    plural_fallback="(after {count} auto-retry attempts)",
)
_SUB_AGENT_DIAGNOSTICS = msg("tui.tool_card.sub_agent.diagnostics", fallback="Diagnostics: {path}")
_SUB_AGENT_ERRORED_WITH_REASON = msg("tui.tool_card.sub_agent.errored_with_reason", fallback="Errored: {reason}")
_SUB_AGENT_PAUSED = msg(
    "tui.tool_card.sub_agent.paused",
    fallback="Paused — awaiting user",
)
_SUB_AGENT_SKIP_SLEEP = msg("tui.tool_card.sub_agent.button.skip_sleep", fallback="Skip sleep")
_SUB_AGENT_RETRY = msg("tui.tool_card.sub_agent.button.retry", fallback="Retry")
_SUB_AGENT_ABORT = msg("tui.tool_card.sub_agent.button.abort", fallback="Abort")
_SUB_AGENT_VIEW_DETAILS = msg("tui.tool_card.sub_agent.button.view_details", fallback="View details")
_SUB_AGENT_SKIP_SLEEP_TOOLTIP = msg(
    "tui.tool_card.sub_agent.button.skip_sleep_tooltip",
    fallback="Skip the active sleep in this sub-agent",
)

_SUB_AGENT_REASON_MESSAGES: dict[str, MessageDef] = {
    "stream_stall": _SUB_AGENT_REASON_STREAM_STALLED,
    "last_words": _SUB_AGENT_REASON_COMPACTION_FAILED,
    "framework_exc": _SUB_AGENT_REASON_FAILED,
    "acp_transport": _SUB_AGENT_REASON_ACP_INTERRUPTED,
}

_COMPACTION_ENTRY_KIND = "compaction"
"""Synthetic ``tool_kind`` for the Phase-4 compaction progress line."""

# The failure results the sub-agent policies write name the card's own agent
# ("sub-agent 'X' failed — <cause>"); the card line keeps only what the card
# does not already say.
_OWN_AGENT_PREFIX = re.compile(r"sub-agent '[^']*' (?:failed —\s*)?")
# Far wider than any card: the activity line ellipsizes to its own width.
_ERROR_REASON_MAX_CHARS = 400
# The start of an answer the activity line shows; text past it never reaches the line.
_ACTIVITY_TEXT_LIMIT = 512


def _error_reason(result: str) -> str:
    """One display line naming why the sub-agent call failed, or ``""``."""
    text = result.strip().removeprefix("Error:").lstrip()
    text = _OWN_AGENT_PREFIX.sub("", text, count=1) if text.startswith("sub-agent '") else text
    reason = sanitize_legacy_scalar(surrogate_safe_text(" ".join(text.split())))
    if len(reason) > _ERROR_REASON_MAX_CHARS:
        reason = reason[: _ERROR_REASON_MAX_CHARS - 1] + "…"
    return reason


@dataclass
class _InnerToolEntry:
    """State for a single inner tool call."""

    call_id: str
    tool_name: str
    tool_kind: str
    args: dict[str, Any]
    status: str = "running"
    completion_counted: bool = False


class _SubAgentActivityText(RunningTitle, Static):
    """Single-line latest activity with a real narrow-width ellipsis, after the running dot while the agent runs."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"sa-activity-text--shimmer"}
    SHIMMER_COMPONENT = "sa-activity-text--shimmer"

    def __init__(self, text: Text, *, running: bool) -> None:
        super().__init__(text, markup=False, id="sa-activity-text")
        self._full_text = text.copy()
        self.set_running(running)

    def set_text(self, text: Text, *, running: bool) -> None:
        self._full_text = text.copy()
        self.set_running(running)
        self.refresh(layout=False)

    def render(self) -> Text:
        text = self._full_text.copy()
        available = (self.content_size.width or self.size.width) - (2 if self._work_running else 0)
        if available > 0:
            text.truncate(available, overflow="ellipsis")
        return self.running_title(text) if self._work_running else text


# Custom Textual messages — bubble up to MainScreen so it can publish the
# corresponding bus event.  Keeping a translation layer between widget
# clicks and event bus events means the widget doesn't need to hold a
# reference to the bus.


class SubAgentRetryClicked(Message):
    """User clicked Retry on a paused :class:`SubAgentToolCall` card."""

    def __init__(self, invocation_id: str) -> None:
        super().__init__()
        self.invocation_id = invocation_id


class SubAgentAbortClicked(Message):
    """User clicked Abort on a paused :class:`SubAgentToolCall` card."""

    def __init__(self, invocation_id: str) -> None:
        super().__init__()
        self.invocation_id = invocation_id


class SubAgentToolCall(BaseToolCard):
    """Renderer for sub-agent tool invocations.

    Implements the ToolCall protocol so it can be used in ToolGroup.
    A dedicated :class:`VirtualizedMarkdown` renders the original prompt in a
    ``Task`` panel. The second panel keeps only the newest assistant/tool
    activity; a dedicated details action opens :class:`AgentTranscriptSurface`
    in the existing modal, reusing the main-agent transcript renderers.
    """

    DEFAULT_CSS = """
    SubAgentToolCall {
        padding: 0 0 0 2;
        height: auto;
        margin: 0 0 1 0;
    }
    SubAgentToolCall #sa-label {
        height: auto;
    }
    SubAgentToolCall > #sa-task-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-neutral-128 $border-opacity;
        border-title-color: $text-muted;
        border-title-style: not bold;
        padding: 0 1 0 1;
    }
    SubAgentToolCall > #sa-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-title-style: bold;
        border-title-align: left;
        border-subtitle-color: $warning;
        padding: 0 1 0 1;
    }
    SubAgentToolCall.-complete > #sa-panel {
        border: round $tui-border-success 30%;
        border-title-color: $success;
        border-title-style: not bold;
        border-subtitle-color: $success;
    }
    SubAgentToolCall.-error > #sa-panel {
        border: round $tui-border-error 50%;
        border-title-color: $error;
        border-title-style: not bold;
        border-subtitle-color: $error;
    }
    SubAgentToolCall.-rejected > #sa-panel {
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-title-style: not bold;
        border-subtitle-color: $warning;
    }
    SubAgentToolCall.-paused > #sa-panel {
        border: round $tui-border-warning $border-opacity;
        border-title-color: $warning;
        border-title-style: bold;
        border-subtitle-color: $warning;
    }
    SubAgentToolCall #sa-latest {
        height: 1;
        width: 100%;
    }
    SubAgentToolCall #sa-activity-text {
        color: $text-muted;
        height: 1;
        width: 1fr;
        overflow: hidden;
    }
    /* What the running dot, lit, and the middle of the band sweeping across the activity brighten to. */
    SubAgentToolCall #sa-activity-text > .sa-activity-text--shimmer {
        color: $text;
        text-style: not bold;
    }
    SubAgentToolCall #sa-task {
        height: auto;
        min-height: 1;
        padding: 0;
        background: transparent;
    }
    SubAgentToolCall #sa-retry-banner {
        height: auto;
        display: none;
        color: $warning;
        text-style: italic;
    }
    SubAgentToolCall.-retrying #sa-retry-banner {
        display: block;
    }
    SubAgentToolCall #sa-pause-info {
        height: auto;
        display: none;
        color: $error;
    }
    SubAgentToolCall.-paused #sa-pause-info {
        display: block;
    }
    SubAgentToolCall #sa-actions {
        height: auto;
        display: none;
        margin: 1 0 0 0;
    }
    SubAgentToolCall #sa-sleep-actions {
        height: auto;
        display: none;
        margin: 1 0 0 0;
    }
    SubAgentToolCall.-sleeping #sa-sleep-actions {
        display: block;
    }
    SubAgentToolCall.-done #sa-sleep-actions {
        display: none;
    }
    SubAgentToolCall.-paused #sa-actions {
        display: block;
    }
    SubAgentToolCall #sa-actions Button {
        margin: 0 1 0 0;
        min-width: 10;
        height: 1;
        border: none;
    }
    SubAgentToolCall #sa-retry-btn {
        background: $warning;
        color: $text;
        text-style: bold;
    }
    SubAgentToolCall #sa-abort-btn {
        background: $error;
        color: $text;
        text-style: bold;
    }
    SubAgentToolCall #sa-skip-sleep-btn {
        min-width: 12;
        height: 1;
        border: none;
        background: $warning;
        color: $text;
        text-style: bold;
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
        self._transcript_journal = AgentTranscriptJournal()
        self._final_transcript_recorded = False
        self._transcript_loader: Callable[[str], Awaitable[PersistedSubAgentTranscript | None]] | None = None
        self._transcript_log_file = ""
        self._transcript_profile_name = tool_name
        self._linked_display_name = ""
        self._latest_activity: MessageRef | str = self._agent_activity()
        self._activity_segment: str | None = None
        """The answer segment the latest activity shows, if it shows one."""

        self._invocation_id: str | None = None
        self._inner_tools: dict[str, _InnerToolEntry] = {}
        self._seen_inner_call_ids: set[str] = set()
        self._terminal_inner_call_ids: set[str] = set()
        self._total_inner_calls = 0
        self._completed_inner_calls = 0
        self._progress_tool_calls = 0
        self._progress_ctx_tokens = 0
        self._progress_total_usage_tokens = 0
        self._usage_unreported_attempts = 0
        # Committed Phase-4 compactions — persistent, unlike the compaction
        # entries in ``_inner_tools`` which the visibility cap can evict.
        # Driven by the committed signal, not finished(ok): a generated note
        # whose spill write fails is abandoned and must not count.
        self._compaction_count = 0

        self._start_time = time.monotonic()
        self._timer: Timer | None = None
        # NOTE: name must avoid ``_task`` — that attribute is owned by
        # :class:`textual.message_pump.MessagePump`, which assigns the
        # widget's running ``asyncio.Task`` to ``self._task`` before
        # :meth:`compose` runs.  Storing the prompt under that name makes
        # it look like a string in ``__init__`` and an ``asyncio.Task`` by
        # the time ``compose()`` reads it, which crashes ``Text(...)``.
        self._task_prompt = self._extract_prompt(self.args_summary, self.args)

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _activity_text(self) -> Text:
        activity = self._latest_activity
        plain = activity if isinstance(activity, str) else self._render_message(activity)
        return Text(plain, no_wrap=True, overflow="ellipsis")

    def _agent_activity(self) -> MessageRef | str:
        agent_name = self._linked_display_name or self.tool_name
        return tool_activity_reference(self.tool_name, KIND_SUB_AGENT, {"agent_name": agent_name})

    def _remember_activity(self, activity: MessageRef | str, *, segment_id: str | None = None) -> MessageRef | str:
        self._latest_activity = activity
        self._activity_segment = segment_id
        self._sync_activity_summary()
        return activity

    def _sync_activity_summary(self) -> None:
        with suppress(Exception):
            self.query_one("#sa-activity-text", _SubAgentActivityText).set_text(
                self._activity_text(), running=self.status == "running"
            )

    def _label_text(self, duration_ms: int = 0) -> Text:
        t = Text()
        t.append("• ", style="bold")
        t.append("SubAgent", style="bold")
        if duration_ms:
            t.append(f" ({fmt_duration(duration_ms)})", style="dim")
        return t

    def _running_label_text(self) -> Text:
        """Label with live elapsed time while running."""
        t = Text()
        t.append("• ", style="bold")
        t.append("SubAgent", style="bold")
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        if elapsed_ms >= 1000:
            t.append(f" ({fmt_duration(elapsed_ms)})", style="dim")
        return t

    def compose(self) -> ComposeResult:
        header = ToolCardHeader(
            self._running_label_text(),
            id="sa-label",
            view_only_label=_SUB_AGENT_VIEW_DETAILS.bind() if self.status in {"running", "paused"} else None,
        )
        if self.status not in {"running", "paused"}:
            header.show_actions()
        yield header
        if self._task_prompt:
            yield self._build_task_panel()
        with Vertical(id="sa-panel") as panel:
            panel.border_title = Text(self._render_title())
            with Horizontal(id="sa-latest"):
                yield _SubAgentActivityText(self._activity_text(), running=self.status == "running")
            with Horizontal(id="sa-sleep-actions"):
                skip = Button(
                    render_text(widget_localizer(self), _SUB_AGENT_SKIP_SLEEP.bind()),
                    id="sa-skip-sleep-btn",
                    compact=True,
                )
                skip.tooltip = render_text(widget_localizer(self), _SUB_AGENT_SKIP_SLEEP_TOOLTIP.bind())
                yield skip
            yield Static("", id="sa-retry-banner")
            yield Static("", id="sa-pause-info")
            with Horizontal(id="sa-actions"):
                yield Button(
                    render_text(widget_localizer(self), _SUB_AGENT_RETRY.bind()),
                    id="sa-retry-btn",
                    compact=True,
                )
                yield Button(
                    render_text(widget_localizer(self), _SUB_AGENT_ABORT.bind()),
                    id="sa-abort-btn",
                    compact=True,
                )

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._spin)
        self._sync_activity_summary()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    @staticmethod
    def _extract_prompt(args_summary: str, args: dict[str, Any]) -> str:
        """Extract prompt string safely from args_summary or args dict."""
        if args_summary:
            try:
                parsed = json.loads(args_summary)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                val = parsed.get("prompt", "")
                if isinstance(val, str):
                    return val
        val = args.get("prompt")
        if isinstance(val, str):
            return val
        return ""

    def _build_task_panel(self) -> Widget:
        task_panel = Widget(VirtualizedMarkdown(self._task_prompt, id="sa-task"), id="sa-task-panel")
        task_panel.border_title = render_text(widget_localizer(self), _SUB_AGENT_TASK.bind())
        return task_panel

    def update_args(self, args: dict[str, Any]) -> None:
        """Refresh the task prompt after approval edits the sub-agent handoff."""
        self.args = args
        self.args_summary = json.dumps(args, ensure_ascii=False) if args else ""
        self._task_prompt = self._extract_prompt(self.args_summary, self.args)
        if self._task_prompt:
            if self.query("#sa-task-panel"):
                with suppress(Exception):
                    self.query_one("#sa-task", VirtualizedMarkdown).update(self._task_prompt)
            else:
                with suppress(Exception):
                    self.mount(self._build_task_panel(), before=self.query_one("#sa-panel", Vertical))
            return
        with suppress(Exception):
            self.query_one("#sa-task-panel", Widget).remove()

    def _spin(self) -> None:
        if self.status == "running":
            self._spin_idx = (self._spin_idx + 1) % len(self._SPINNERS)
            # Only a shown card repaints (see ``ToolCall._spin``); the first tick after it shows paints it.
            if not is_widget_shown_on_active_screen(self):
                return
            with suppress(Exception):
                # The label shows whole seconds: most ticks leave it as it is, and
                # rewriting an equal label would only repaint the header.
                label = self._running_label_text()
                header = self.query_one("#sa-label", ToolCardHeader)
                if header.content != label:
                    header.update(label)
                self._update_title()

    def _update_title(self) -> None:
        """Update border title (top-left) and subtitle (bottom-right)."""
        panel = self.query_one("#sa-panel", Vertical)
        panel.border_title = Text(self._render_title())
        panel.border_subtitle = Text(self._render_subtitle())

    @staticmethod
    def _fmt_duration(ms: int) -> str:
        return fmt_duration(ms)

    def _render_title(self) -> str:
        if self.status == "running":
            return f"{self._SPINNERS[self._spin_idx]} {self.tool_name}"
        if self.status == "complete":
            return f"\u2713 {self.tool_name}"
        return f"\u2717 {self.tool_name}"

    def _render_subtitle(self) -> str:
        tool_calls = self._progress_tool_calls if self.status == "running" else self._total_inner_calls
        if not tool_calls and self.status != "running":
            # A restored card saw none of the run's tool calls: it shows the persisted count.
            tool_calls = self._progress_tool_calls
        parts = invocation_progress_parts(
            tool_calls=tool_calls,
            context_tokens=self._progress_ctx_tokens,
            usage_tokens=self._progress_total_usage_tokens,
            unreported_attempts=self._usage_unreported_attempts,
            compactions=self._compaction_count,
            render=self._render_message,
        )
        duration_ms = self.duration_ms
        if self.status == "running":
            duration_ms = int((time.monotonic() - self._start_time) * 1000)
            if duration_ms < 1000:
                duration_ms = 0
        if duration_ms:
            parts.append(self._render_message(_SUB_AGENT_DURATION.bind(duration=self._fmt_duration(duration_ms))))
        return " \u00b7 ".join(parts)

    def _running_sleep_call_id(self) -> str:
        """Return the newest running inner sleep call id, if one is visible."""
        for entry in reversed(self._inner_tools.values()):
            is_sleep = entry.tool_kind == KIND_SLEEP or (not entry.tool_kind and entry.tool_name == "sleep")
            if entry.status == "running" and is_sleep:
                return entry.call_id
        return ""

    def _refresh_sleep_action_state(self) -> None:
        if self.status == "running" and self._running_sleep_call_id():
            self.add_class("-sleeping")
        else:
            self.remove_class("-sleeping")

    def _show_completion_controls(self) -> None:
        """Expose copy/details affordances without mounting the final answer inline."""
        self.add_class("-done")
        self._show_tool_copy_button()

    async def mount_pending_content(self) -> bool:
        """The compact activity line has no deferred inline content."""
        return False

    def release_collapsed_content(self) -> None:
        """The one-line activity summary remains mounted when its parent collapses."""
        return

    def _tool_copy_input(self) -> tuple[str, str]:
        """Copy the sub-agent prompt without duplicating the full args dict."""
        return "markdown", self._task_prompt or self._render_message(TOOL_VIEW_EMPTY.bind())

    def _tool_view_input_widgets(self) -> list[Widget]:
        """Render the sub-agent prompt as markdown in the detail modal."""
        dark = self._view_dark()
        label = Static(
            Text(render_str(widget_localizer(self), _SUB_AGENT_TASK_PROMPT.bind()), style="bold"),
            classes="tool-view-section-title tool-view-section-title-first",
        )
        widgets: list[Widget] = [
            label,
            build_code_view(
                "markdown",
                self._task_prompt or self._render_message(TOOL_VIEW_EMPTY.bind()),
                dark=dark,
                render_message=self._render_message,
            ),
        ]

        args = self._tool_input_args()
        extra = {key: value for key, value in args.items() if key != "prompt"}
        if extra:
            widgets.extend(build_params_view(extra, dark=dark, render_message=self._render_message))
        return widgets

    def _tool_copy_sections(self) -> list[tuple[str, str, str]]:
        """Preserve the sub-agent's markdown final answer in the copy payload."""
        return [
            (
                self._render_message(TOOL_VIEW_OUTPUT.bind()),
                "markdown",
                self.result_text or self._render_message(TOOL_VIEW_EMPTY.bind()),
            )
        ]

    def _tool_view_copy_enabled(self) -> bool:
        """Disable stale whole-output copying while the sub-agent is live."""
        return self.status not in {"running", "paused"}

    def _tool_view_output_widgets(self) -> list[Widget]:
        """Mount a full live transcript projection in the existing detail modal."""
        persisted_replay_loader: Callable[[], Awaitable[PersistedSubAgentTranscript | None]] | None = None
        metadata = self.metadata or {}
        transcript_final = metadata.get(SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY)
        fallback_final_text = self.result_text
        if transcript_final == "" and self._transcript_journal.has_activity:
            # The empty override means the parent result already showed in the
            # transcript as its answer segments. Suppress fallback only while
            # that live evidence is retained; a failed durable replay must still
            # fall back to the authoritative parent result.
            fallback_final_text = ""
        metadata_log_file = metadata.get("sub_agent_log_file")
        log_file = (
            metadata_log_file if isinstance(metadata_log_file, str) and metadata_log_file else self._transcript_log_file
        )
        # A running/paused card's journal is the live authority. A terminal
        # audit may be written just before the parent result is published; if
        # a live modal consumed that audit, it could seal the journal before
        # the authoritative result arrived. Persisted replay is therefore a
        # terminal-card restoration path only.
        if (
            self.status in {"complete", "error", "rejected", "interrupted"}
            and log_file
            and self._transcript_loader is not None
        ):
            transcript_loader = self._transcript_loader

            async def load_persisted_replay() -> PersistedSubAgentTranscript | None:
                return await transcript_loader(log_file)

            persisted_replay_loader = load_persisted_replay
        return [
            AgentTranscriptSurface(
                self._transcript_journal,
                profile_name=self._transcript_profile_name,
                opening_prompt=self._task_prompt,
                fallback_final_text=fallback_final_text,
                persisted_replay_loader=persisted_replay_loader,
            )
        ]

    def configure_transcript_loader(
        self,
        loader: Callable[[str], Awaitable[PersistedSubAgentTranscript | None]],
    ) -> None:
        """Install the screen-owned safe persisted-transcript loader."""
        self._transcript_loader = loader

    def add_answer(self, segment_id: str, text: str, *, profile_name: str = "") -> MessageRef | str | None:
        """Add one whole answer segment to the nested transcript; return the new latest activity, if it changed."""
        if profile_name and not self._linked_display_name:
            self._transcript_profile_name = profile_name
        self._transcript_journal.record(TranscriptAssistantOp(text, segment_id=segment_id))
        single_line = sanitize_legacy_scalar(" ".join(text[:_ACTIVITY_TEXT_LIMIT].splitlines()).strip())
        if not single_line:
            return None
        if single_line == self._latest_activity:
            self._activity_segment = segment_id
            return None
        return self._remember_activity(single_line, segment_id=segment_id)

    def withdraw_answer(self, segment_ids: tuple[str, ...]) -> bool:
        """A retry dropped the answer these segments showed from history: the nested transcript drops it too.

        Returns whether the latest activity showed that answer, and so went back to the agent's own.
        """
        self._transcript_journal.record(TranscriptAnswerWithdrawnOp(segment_ids))
        if self._activity_segment not in segment_ids:
            return False
        self._remember_activity(self._agent_activity())
        return True

    def _record_final_transcript(self, text: str, *, streamed: bool) -> None:
        if self._final_transcript_recorded:
            return
        self._final_transcript_recorded = True
        self._transcript_journal.record(TranscriptAssistantOp(text=text, final=True, streamed=streamed))

    # --- Invocation linking ---

    def claim_invocation(self, invocation_id: str, agent_name: str = "", sub_agent_log_file: str = "") -> None:
        """Link this widget and apply the invocation's display name."""
        self._invocation_id = invocation_id
        if sub_agent_log_file:
            self._transcript_log_file = sub_agent_log_file
        if not agent_name:
            return
        self._linked_display_name = agent_name
        self._transcript_profile_name = agent_name
        self._remember_activity(self._agent_activity())

    def update_progress(
        self,
        tool_call_count: int,
        total_tokens: int,
        total_usage_tokens: int = 0,
        usage_unreported_attempts: int = 0,
    ) -> None:
        """Update cumulative progress stats and refresh the border title."""
        self._progress_tool_calls = tool_call_count
        self._progress_ctx_tokens = total_tokens
        self._progress_total_usage_tokens = total_usage_tokens
        self._usage_unreported_attempts = usage_unreported_attempts
        if self.status == "running":
            self._update_title()

    # --- Inner tool management ---

    async def add_inner_tool_start(
        self,
        call_id: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_kind: str = "",
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
    ) -> bool:
        """Insert or refresh a running inner tool call by its lifetime id; return whether it is new or changed."""
        if call_id in self._terminal_inner_call_ids:
            return False
        self._transcript_journal.record(
            TranscriptToolStartOp(
                call_id=call_id,
                tool_name=tool_name,
                tool_kind=tool_kind,
                args=dict(args),
                provider_hosted=provider_hosted,
                hosted_family=hosted_family,
                provider=provider,
                provider_item_type=provider_item_type,
                provider_status=provider_status,
                provider_call_id=provider_call_id,
            )
        )
        entry = self._inner_tools.get(call_id)
        # A running call's start can come again (ACP: when what it shows changes, hosted details included): only
        # a change of its name, kind or input moves the latest activity back to it.
        changed = entry is None or (entry.tool_name, entry.tool_kind, entry.args) != (tool_name, tool_kind, args)
        if changed:
            self._remember_activity(tool_activity_reference(tool_name, tool_kind, args))
        # A new inner tool call means the sub-agent is making forward
        # progress — if a retry banner is still visible from a prior
        # transient failure, the retry has clearly succeeded. Drop it
        # now instead of letting it linger until the parent tool call
        # finally resolves.
        if self.has_class("-retrying"):
            self.remove_class("-retrying")
            with suppress(Exception):
                self.query_one("#sa-retry-banner", Static).update("")
        if entry is None:
            entry = _InnerToolEntry(
                call_id=call_id,
                tool_name=tool_name,
                tool_kind=tool_kind,
                args=dict(args),
            )
            self._inner_tools[call_id] = entry
        else:
            entry.tool_name = tool_name
            entry.tool_kind = tool_kind
            entry.args = dict(args)
        if call_id not in self._seen_inner_call_ids:
            self._seen_inner_call_ids.add(call_id)
            self._total_inner_calls += 1
        self._refresh_sleep_action_state()
        return changed

    def update_inner_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        """Refresh arguments on a running nested tool entry."""
        entry = self._inner_tools.get(call_id)
        if entry is None or call_id in self._terminal_inner_call_ids:
            return
        self._transcript_journal.record(TranscriptToolArgsOp(call_id, dict(args)))
        entry.args = dict(args)
        self._remember_activity(tool_activity_reference(entry.tool_name, entry.tool_kind, entry.args))

    def update_inner_tool_progress(
        self,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[Any] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Apply the latest bounded progress snapshot to a nested entry."""
        entry = self._inner_tools.get(call_id)
        if entry is None or call_id in self._terminal_inner_call_ids:
            return
        self._transcript_journal.record(
            TranscriptToolProgressOp(
                call_id,
                list(lines),
                list(image_contents or []),
                dict(snapshot_metadata or {}),
                provider_status,
            )
        )

    def update_inner_tool_status(
        self,
        call_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        """Apply one canonical lifecycle status to a nested entry."""
        entry = self._inner_tools.get(call_id)
        if entry is None or call_id in self._terminal_inner_call_ids:
            return
        if status not in {"completed", "failed", "interrupted"}:
            return
        self._transcript_journal.record(TranscriptToolStatusOp(call_id, status, provider_status, dict(metadata or {})))
        if status == "completed":
            # A completed provider snapshot can precede output_item.done. Show
            # it as complete now, but leave the id open for one authoritative
            # result carrying final images, duration, and metadata.
            self._apply_inner_tool_completion(
                call_id,
                entry,
                str((metadata or {}).get("result_text", "")),
                0,
                metadata=metadata,
                authoritative_result=False,
            )
            return
        entry.status = "interrupted" if status == "interrupted" else "error"
        if not entry.completion_counted:
            entry.completion_counted = True
            self._completed_inner_calls += 1
        self._refresh_sleep_action_state()

    def add_compaction_start(self, compaction_id: str) -> None:
        """Show a live "Compacting conversation..." line in the inner feed.

        Phase-4 compaction is not a tool call, so this lifecycle entry is not
        counted in ``_total_inner_calls``. The visible card lives in the
        shared transcript journal.
        """
        self._transcript_journal.record(TranscriptCompactionStartOp(compaction_id))
        entry = _InnerToolEntry(
            call_id=f"compaction:{compaction_id}",
            tool_name="compaction",
            tool_kind=_COMPACTION_ENTRY_KIND,
            args={},
        )
        self._inner_tools[entry.call_id] = entry

    def complete_compaction(
        self,
        compaction_id: str,
        *,
        outcome: str,
        duration_ms: int = 0,
        format_violation: str = "",
        failure_reason: str = "",
    ) -> None:
        """Flip the compaction line to its terminal state."""
        entry = self._inner_tools.get(f"compaction:{compaction_id}")
        if entry is None:
            return
        self._transcript_journal.record(
            TranscriptCompactionFinishedOp(
                compaction_id,
                outcome,
                duration_ms,
                format_violation,
                failure_reason,
            )
        )
        if outcome == "ok":
            entry.status = "complete"
            # A successful finish proves the LAST_WORDS retry loop
            # recovered — drop a lingering "Retrying in …" banner, same
            # forward-progress rule as add_inner_tool_start. (Terminal
            # failure pauses the sub-agent, whose flow clears it instead.)
            if self.has_class("-retrying"):
                self.remove_class("-retrying")
                with suppress(Exception):
                    self.query_one("#sa-retry-banner", Static).update("")
        elif outcome == "canceled":
            entry.status = "interrupted"
        else:
            entry.status = "error"
        self._inner_tools.pop(entry.call_id, None)

    def record_compaction_committed(self, compaction_id: str) -> None:
        """Count a durably committed compaction round in the subtitle.

        Fires on the committed signal, which trails ``complete_compaction``'s
        finished(ok) — a successful note generation whose spill write later
        failed never commits, so counting here (not on finished-ok) keeps
        "Compactions: N" honest.  Independent of the feed entry: the line
        may already be evicted by the visibility cap.
        """
        self._compaction_count += 1
        with suppress(Exception):
            self._update_title()

    def complete_inner_tool(
        self,
        call_id: str,
        result: str,
        duration_ms: int,
        image_contents: list[Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        """Mark an inner tool call as complete."""
        entry = self._inner_tools.get(call_id)
        if not entry or call_id in self._terminal_inner_call_ids:
            return
        self._transcript_journal.record(
            TranscriptToolResultOp(
                call_id=call_id,
                tool_name=entry.tool_name,
                result=result,
                duration_ms=duration_ms,
                image_contents=list(image_contents or []),
                artifacts=[dict(artifact) for artifact in artifacts or []],
                approval=approval,
                metadata=dict(metadata or {}),
                provider_status=provider_status,
                canonical_status=canonical_status,
            )
        )
        self._apply_inner_tool_completion(
            call_id,
            entry,
            result,
            duration_ms,
            image_contents=image_contents,
            artifacts=artifacts,
            approval=approval,
            metadata=metadata,
            authoritative_result=True,
        )

    def _apply_inner_tool_completion(
        self,
        call_id: str,
        entry: _InnerToolEntry,
        result: str,
        duration_ms: int,
        *,
        image_contents: list[Any] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        approval: str | None = None,
        metadata: dict[str, Any] | None = None,
        authoritative_result: bool,
    ) -> None:
        """Render completion while keeping status-only snapshots replaceable."""
        if authoritative_result:
            self._terminal_inner_call_ids.add(call_id)
            # A terminal id short-circuits add_inner_tool_start before the
            # seen-check, so its _seen membership is dead weight — drop it to
            # keep the dedupe set bounded by in-flight calls.
            self._seen_inner_call_ids.discard(call_id)
        render_status = tool_result_render_status(result, approval, entry.tool_kind, metadata, entry.tool_name)
        if render_status == "rejected":
            entry.status = "rejected"
        elif isinstance(metadata, dict) and metadata.get("sleep_interrupted") is True:
            entry.status = "interrupted"
        elif isinstance(metadata, dict) and metadata.get("sleep_skipped") is True:
            entry.status = "skipped"
        elif render_status == "error":
            entry.status = "error"
        else:
            entry.status = "complete"
        if not entry.completion_counted:
            entry.completion_counted = True
            self._completed_inner_calls += 1
        if authoritative_result:
            self._inner_tools.pop(call_id, None)
        self._refresh_sleep_action_state()

    # --- ToolCall protocol ---

    def _settle_running_inner_tools(self, *, completed: bool) -> None:
        """Close transcript cards whose terminal event lost the race with the parent result."""
        canonical_status = "completed" if completed else "interrupted"
        for entry in tuple(self._inner_tools.values()):
            if entry.status != "running":
                continue
            if entry.tool_kind == _COMPACTION_ENTRY_KIND:
                self._transcript_journal.record(
                    TranscriptCompactionFinishedOp(
                        entry.call_id.removeprefix("compaction:"),
                        "canceled",
                    )
                )
            else:
                self._transcript_journal.record(TranscriptToolStatusOp(entry.call_id, canonical_status))
            entry.status = "complete" if completed else "interrupted"
            if not entry.completion_counted:
                entry.completion_counted = True
                self._completed_inner_calls += 1

    def _finish_transcript(
        self,
        result: str,
        status: Literal["complete", "error", "rejected", "interrupted"],
    ) -> None:
        """Finalize active nested cards, append the answer, and stop compact activity UI."""
        self._settle_running_inner_tools(completed=status == "complete")
        metadata = self.metadata or {}
        transcript_final = metadata.get(SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY)
        has_transcript_override = status == "complete" and isinstance(transcript_final, str)
        if not has_transcript_override or transcript_final:
            # Only a completed result is the answer the transcript streamed.
            self._record_final_transcript(
                transcript_final if has_transcript_override else result, streamed=status == "complete"
            )
        elif self._transcript_journal.has_activity:
            # The answer segments already showed the result: the last one settles as it, nothing is added.
            self._record_final_transcript("", streamed=True)
        self._transcript_journal.finalize_retention(
            durable_replay_available=(
                bool(metadata.get("sub_agent_log_file")) and metadata.get("sub_agent_audit_complete") is True
            )
        )
        status_message = {
            "complete": TOOL_CARD_COMPLETED,
            "error": TOOL_CARD_ERRORED,
            "rejected": TOOL_CARD_REJECTED,
            "interrupted": TOOL_CARD_INTERRUPTED,
        }[status]
        reason = _error_reason(result) if status == "error" else ""
        self._remember_activity(_SUB_AGENT_ERRORED_WITH_REASON.bind(reason=reason) if reason else status_message.bind())

    def _drop_inner_call_tracking(self) -> None:
        """Free per-call id bookkeeping once the parent call is terminal.

        Completed cards stay mounted for the session; keeping every inner
        call id (up to thousands per ACP attempt, fresh ids per retry)
        would grow the seen/terminal sets without bound across a long
        session. Late inner events are already inert after a parent
        terminal: completions need a live ``_inner_tools`` entry, and
        session replay rebuilds a fresh card from scratch.
        """
        self._inner_tools.clear()
        self._seen_inner_call_ids.clear()
        self._terminal_inner_call_ids.clear()

    def _clear_transient_state(self) -> None:
        """Drop stale retry/pause CSS classes + banner text before a terminal transition.

        Both ``-retrying`` and ``-paused`` belong to *in-flight* states.
        When the sub-agent finally completes or errors out, the banners
        those classes reveal should not remain visible — otherwise the
        card shows a "Retrying in 7s…" line *after* a successful
        "Recovered" result, or a "Stream stalled" pause-info line *after*
        the user has already aborted.
        """
        self.remove_class("-retrying")
        self.remove_class("-paused")
        self.remove_class("-sleeping")
        with suppress(Exception):
            self.query_one("#sa-retry-banner", Static).update("")
            self.query_one("#sa-pause-info", Static).update("")

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        """Mark this sub-agent call as complete (parent tool call finished).

        If structured metadata or legacy result text indicates a sub-agent
        error, redirect to ``set_error()`` so the widget renders with the
        error style.
        Rejected tools (approval declined) get a separate warning style.
        """
        self.approval = kwargs.get("approval")
        metadata = kwargs.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        if self.metadata.get(TOOL_INTERRUPTED_METADATA_KEY) is True:
            self.result_text = result
            self.duration_ms = duration_ms
            self.status = "interrupted"
            self._finish_transcript(result, "interrupted")
            self._drop_inner_call_tracking()
            self._clear_transient_state()
            self.add_class("-rejected")
            if self._timer is not None:
                self._timer.stop()
            with suppress(Exception):
                panel = self.query_one("#sa-panel")
                panel.border_title = Text(self.tool_name)
                panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_INTERRUPTED.bind())
                self.query_one("#sa-label", Static).update(self._label_text(duration_ms))
            self._show_completion_controls()
            return
        render_status = tool_result_render_status(
            result,
            self.approval,
            self.tool_kind or KIND_SUB_AGENT,
            self.metadata,
            self.tool_name,
        )
        if render_status == "rejected":
            self.result_text = result
            self.duration_ms = duration_ms
            self.status = "rejected"
            self._finish_transcript(result, "rejected")
            self._drop_inner_call_tracking()
            self._clear_transient_state()
            self.add_class("-rejected")
            if self._timer is not None:
                self._timer.stop()
            with suppress(Exception):
                panel = self.query_one("#sa-panel")
                panel.border_title = Text(self.tool_name)
                panel.border_subtitle = render_text(widget_localizer(self), TOOL_CARD_REJECTED.bind())
                self.query_one("#sa-label", Static).update(self._label_text(duration_ms))
            self._show_completion_controls()
            return
        if render_status == "error":
            self._set_error(result, duration_ms)
            return
        self.result_text = result
        self.duration_ms = duration_ms
        self.status = "complete"
        self._adopt_persisted_progress()
        self._finish_transcript(result, "complete")
        self._drop_inner_call_tracking()
        self._clear_transient_state()
        self.add_class("-complete")
        if self._timer is not None:
            self._timer.stop()
        with suppress(Exception):
            self.query_one("#sa-label", Static).update(self._label_text(duration_ms))
            self._update_title()
        self._show_completion_controls()

    def _adopt_persisted_progress(self) -> None:
        """Take each counter the card never saw live from the run's totals its parent result keeps.

        A card restored from a saved session saw none of them. A live card keeps
        every counter it saw, and takes only one that stayed at zero.
        """
        totals = SubAgentProgressTotals.from_metadata((self.metadata or {}).get(SUB_AGENT_PROGRESS_METADATA_KEY))
        self._progress_tool_calls = self._progress_tool_calls or totals.tool_calls
        self._progress_ctx_tokens = self._progress_ctx_tokens or totals.context_tokens
        self._progress_total_usage_tokens = self._progress_total_usage_tokens or totals.usage_tokens
        self._usage_unreported_attempts = self._usage_unreported_attempts or totals.unreported_attempts
        self._compaction_count = self._compaction_count or totals.compactions

    def set_error(self, error: str) -> None:
        """Mark this sub-agent call as failed."""
        self._set_error(error, int((time.monotonic() - self._start_time) * 1000))

    def _set_error(self, error: str, duration_ms: int) -> None:
        """Apply an error with either live elapsed or persisted duration."""
        self.result_text = error
        self.duration_ms = duration_ms
        self.status = "error"
        self._adopt_persisted_progress()
        self._finish_transcript(error, "error")
        self._drop_inner_call_tracking()
        self._clear_transient_state()
        self.add_class("-error")
        if self._timer is not None:
            self._timer.stop()
        with suppress(Exception):
            self.query_one("#sa-label", Static).update(self._label_text(duration_ms))
            self._update_title()
        self._show_completion_controls()

    # --- Paused / retry / resumed state ---

    def set_retry_attempt(self, message: str, attempt: int, max_attempts: int, delay_seconds: int) -> None:
        """Show an inline retry banner for an auto-retry attempt.

        Called for :class:`InvocationRetryAttempt` events — the controller
        is retrying transient failures without user intervention. The
        banner gives the user visibility into the retry cadence.  The
        ``↻`` prefix visually marks the line as a retry state sub-entry,
        distinct from the result line that follows it on recovery.
        """
        self.add_class("-retrying")
        retry_message = self._render_message(
            _SUB_AGENT_RETRYING.bind(
                delay_seconds=delay_seconds,
                attempt=attempt,
                max_attempts=max_attempts,
                message=message,
            )
        )
        with suppress(Exception):
            self.query_one("#sa-retry-banner", Static).update(Text(retry_message, style="italic"))

    def set_paused(
        self,
        reason: str,
        last_error: str,
        retry_attempts: int,
        diagnostic_path: str | None = None,
        last_error_display: str | None = None,
    ) -> None:
        """Transition the card to a paused state with Retry/Abort buttons.

        Called when :class:`InvocationPaused` arrives. Auto-retry (if any
        happened first) is now finished; the banner is replaced with the
        pause-info block and the action row is revealed.
        ``last_error_display`` is the rendered meaning of ``last_error``,
        shown above the raw text.
        """
        self.status = "paused"
        self._transcript_journal.record(TranscriptPausedOp())
        # Paused supersedes the retrying banner — the retry run completed
        # (unsuccessfully). Keep visual history of the last error only.
        self.remove_class("-retrying")
        self.remove_class("-sleeping")
        self.add_class("-paused")
        self.remove_class("-complete")
        self.remove_class("-error")
        self._sync_activity_summary()
        if self._timer is not None:
            self._timer.stop()
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        reason_definition = _SUB_AGENT_REASON_MESSAGES.get(reason, _SUB_AGENT_REASON_PAUSED)
        reason_label = self._render_message(reason_definition.bind())
        info_lines = [reason_label]
        if retry_attempts:
            info_lines.append(self._render_message(_SUB_AGENT_AFTER_RETRIES.bind(count=retry_attempts)))
        if last_error_display:
            info_lines.append(last_error_display)
        if last_error:
            info_lines.append(last_error)
        if diagnostic_path:
            # Display copy only — the operational path stays raw on the event.
            info_lines.append(
                self._render_message(_SUB_AGENT_DIAGNOSTICS.bind(path=surrogate_safe_text(diagnostic_path)))
            )
        info = "\n".join(info_lines)
        with suppress(Exception):
            self.query_one("#sa-label", Static).update(self._label_text(elapsed_ms))
            panel = self.query_one("#sa-panel", Vertical)
            panel.border_title = Text(f"\u25aa {self.tool_name}")  # small square = paused
            panel.border_subtitle = render_text(widget_localizer(self), _SUB_AGENT_PAUSED.bind())
            self.query_one("#sa-pause-info", Static).update(Text(info, style="red"))
            self.query_one("#sa-retry-banner", Static).update("")

    def set_resumed_after_pause(self) -> None:
        """Clear the paused state and reset the card to a live running view.

        Called when :class:`InvocationResumed` arrives — the user
        clicked Retry and the controller has re-entered running state.
        """
        if self.status != "paused":
            return
        self.status = "running"
        self._transcript_journal.record(TranscriptResumedOp())
        self.remove_class("-paused")
        self._sync_activity_summary()
        self._refresh_sleep_action_state()
        self._start_time = time.monotonic()  # reset elapsed clock for the new attempt
        self._spin_idx = 0
        # Inner-call tracking is attempt-local: the dead attempt's stream is
        # fully drained (the bus delivers synchronously), and a fresh attempt
        # may legitimately reuse raw call ids (the ACP translator scopes ids
        # with an attempt prefix for exactly that reason). Keeping the old
        # feed would pin its ids forever across unlimited manual retries and
        # make the stale terminal-guard swallow a reused id's new call.
        self._drop_inner_call_tracking()
        with suppress(Exception):
            self.query_one("#sa-pause-info", Static).update("")
            self.query_one("#sa-retry-banner", Static).update("")
            self.query_one("#sa-label", Static).update(self._running_label_text())
            self._update_title()
        self.remove_class("-retrying")
        # Restart the spinner timer.
        if self._timer is not None:
            self._timer.stop()
        self._timer = self.set_interval(0.12, self._spin)

    def set_cascade_aborted(self) -> None:
        """Mark the card as cascade-aborted by a global interrupt."""
        self.remove_class("-paused")
        self.remove_class("-retrying")
        self.set_error("Error: cancelled (global interrupt)")

    def set_aborted(self, last_error: str) -> None:
        """Mark the card as aborted by the user after a pause.

        Fired on :class:`InvocationAborted`.  Clears the paused banner (so
        the Retry/Abort buttons and pause-info disappear immediately) and
        routes through :meth:`set_error` so the card gets the standard
        error styling + a message indicating the abort was user-driven.
        """
        self.remove_class("-paused")
        self.remove_class("-retrying")
        error_text = (
            f"Error: sub-agent aborted by user after failure — {last_error}"
            if last_error
            else "Error: sub-agent aborted by user"
        )
        self.set_error(error_text)

    # --- Button actions ---

    def on_button_pressed(self, event: Button.Pressed) -> None:  # type: ignore[name-defined]
        """Translate a Retry/Abort click into a bubbled message.

        Guarded by ``_invocation_id`` being set — we should never
        dispatch a retry/abort for a card that hasn't been linked yet
        (that would mean no controller exists to receive it).
        """
        if event.button.id == "sa-skip-sleep-btn":
            if call_id := self._running_sleep_call_id():
                self.post_message(SleepSkipClicked(call_id))
            event.stop()
            return
        if self._invocation_id is None:
            return
        if event.button.id == "sa-retry-btn":
            self.post_message(SubAgentRetryClicked(self._invocation_id))
        elif event.button.id == "sa-abort-btn":
            self.post_message(SubAgentAbortClicked(self._invocation_id))
        event.stop()
