# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reusable transcript surface and semantic journal for nested agents."""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

from textual import events as textual_events
from textual.containers import VerticalScroll
from textual.scrollbar import ScrollTo
from textual.widget import Widget

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.util.bottom_follow import BottomFollow
from chrys.app.tui.util.removal import remove_shielded
from chrys.app.tui.widgets.chat.fold_gap import show_fold_gaps
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    RetryMessage,
    SystemMessage,
    UserMessage,
    remove_withdrawn_answer,
)
from chrys.app.tui.widgets.chat.scroll_controller import ManualScrollGcGuard
from chrys.app.tui.widgets.chat.toc_model import TurnTocModel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.errors.display import DISPLAY_WITH_HINT
from chrys.foundation.events.types import InvocationContentDelta
from chrys.foundation.i18n import MessageRef
from chrys.foundation.patches.textual_precompose import precompose_tree

if TYPE_CHECKING:
    from textual.layout import Layout

    from chrys.foundation.events.types import InvocationContentFinished
    from chrys.service.session.sub_agent_transcript import PersistedSubAgentTranscript


_TERMINAL_JOURNAL_MAX_OPERATIONS = 256
_TERMINAL_JOURNAL_TEXT_LIMIT = 16_384
_TERMINAL_JOURNAL_STRUCTURED_LIMIT = 8_192


@dataclass(frozen=True, slots=True)
class TranscriptUserOp:
    """User input received by the nested agent."""

    text: str


@dataclass(frozen=True, slots=True)
class TranscriptAssistantOp:
    """Assistant text: one whole answer segment (:class:`AnswerSegments`), or the invocation's final text."""

    text: str
    final: bool = False
    segment_id: str = ""
    streamed: bool = True
    """Whether a final text is the answer its segments showed; one that is not shows as its own message."""


@dataclass(frozen=True, slots=True)
class TranscriptToolStartOp:
    """Start one nested tool occurrence."""

    call_id: str
    tool_name: str
    tool_kind: str
    args: dict[str, Any] = field(default_factory=dict)
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_status: str = ""
    provider_call_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    """When the call started, as ``time.monotonic()`` read as the start was recorded: a view built later counts a
    running sleep's time left from it."""


@dataclass(frozen=True, slots=True)
class TranscriptToolArgsOp:
    """Replace the visible arguments of one running tool."""

    call_id: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TranscriptToolStatusOp:
    """Apply a structured tool lifecycle status."""

    call_id: str
    status: str
    provider_status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    stopped_at: float = field(default_factory=time.monotonic)
    """When it was recorded, as ``time.monotonic()``: a status that ends a running call ends it then, and a view built
    later counts how long the call ran up to it."""


@dataclass(frozen=True, slots=True)
class TranscriptToolProgressOp:
    """Latest bounded progress snapshot for a running tool."""

    call_id: str
    lines: list[str]
    image_contents: list[Any] = field(default_factory=list)
    snapshot_metadata: dict[str, Any] = field(default_factory=dict)
    provider_status: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptToolResultOp:
    """Terminal result for one nested tool occurrence."""

    call_id: str
    tool_name: str
    result: str
    duration_ms: int = 0
    image_contents: list[Any] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    approval: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provider_status: str = ""
    canonical_status: str = "completed"


@dataclass(frozen=True, slots=True)
class TranscriptCompactionStartOp:
    """Start one nested context compaction."""

    compaction_id: str


@dataclass(frozen=True, slots=True)
class TranscriptCompactionFinishedOp:
    """Complete one nested context compaction."""

    compaction_id: str
    outcome: str
    duration_ms: int = 0
    format_violation: str = ""
    failure_reason: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptInterruptedOp:
    """Close an invocation's transcript and settle its unfinished activity."""

    reason: MessageRef
    stopped_at: float = field(default_factory=time.monotonic)
    """When it stopped the calls still running, as ``time.monotonic()`` read as it was recorded: a view built later
    counts how long they ran up to it."""


@dataclass(frozen=True, slots=True)
class TranscriptErrorOp:
    """Settle failed activity and show the shared chat error card."""

    reason: str | MessageRef
    stopped_at: float = field(default_factory=time.monotonic)
    """When it stopped the calls still running, as ``time.monotonic()`` read as it was recorded: a view built later
    counts how long they ran up to it."""


@dataclass(frozen=True, slots=True)
class TranscriptPausedOp:
    """A failed attempt paused for Retry or Abort: the answer it was writing stops streaming."""


@dataclass(frozen=True, slots=True)
class TranscriptResumedOp:
    """Clear a failed attempt's status before accepting the next attempt."""

    stopped_at: float = field(default_factory=time.monotonic)
    """When it stopped the calls still running, as ``time.monotonic()`` read as it was recorded: a view built later
    counts how long they ran up to it."""


@dataclass(frozen=True, slots=True)
class TranscriptAnswerWithdrawnOp:
    """A retry dropped the answer these segments showed from history, to ask for it again: it goes."""

    segment_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TranscriptRetryOp:
    """Automatic retry, either in the main transcript or its compaction card."""

    message: str | MessageRef
    attempt: int
    max_attempts: int
    delay_seconds: int
    compaction: bool = False
    # Shown after a display ``message`` (e.g. "seems offline"); never without one.
    hint: MessageRef | None = None


@dataclass(frozen=True, slots=True)
class TranscriptWarningOp:
    """A non-terminal warning that must not stop running tools."""

    message: MessageRef


type AgentTranscriptOp = (
    TranscriptUserOp
    | TranscriptAssistantOp
    | TranscriptToolStartOp
    | TranscriptToolArgsOp
    | TranscriptToolStatusOp
    | TranscriptToolProgressOp
    | TranscriptToolResultOp
    | TranscriptCompactionStartOp
    | TranscriptCompactionFinishedOp
    | TranscriptInterruptedOp
    | TranscriptErrorOp
    | TranscriptPausedOp
    | TranscriptResumedOp
    | TranscriptAnswerWithdrawnOp
    | TranscriptRetryOp
    | TranscriptWarningOp
)


@dataclass(slots=True)
class _OpenAnswer:
    segment_id: str
    parts: list[str] = field(default_factory=list)


class AnswerSegments:
    """Streamed answer text, held until its segment ends.

    A nested transcript shows each answer segment whole, as a run that does not
    stream does: a view few people watch live takes no work per delta. A tool
    call's card starts after the text before it ended its segment, so that text
    shows above the card. A segment cut off by a failure or an interrupt shows
    nothing; a retry asks for that answer again.
    """

    def __init__(self) -> None:
        # The open answer segment of each invocation attempt: it has one at a time.
        self._open: dict[tuple[str, int], _OpenAnswer] = {}

    def take(self, event: InvocationContentDelta | InvocationContentFinished) -> TranscriptAssistantOp | None:
        """Hold an answer delta; return the answer segment *event* completes, if it is not blank."""
        key = (event.origin.invocation_id, event.origin.attempt)
        answer = self._open.get(key)
        if isinstance(event, InvocationContentDelta):
            if event.channel == "answer":
                if answer is None or answer.segment_id != event.segment_id:
                    answer = self._open[key] = _OpenAnswer(event.segment_id)
                answer.parts.append(event.text)
            return None
        if answer is None or answer.segment_id != event.segment_id:
            return None
        del self._open[key]
        text = "".join(answer.parts)
        if event.status != "completed" or not text.strip():
            return None
        return TranscriptAssistantOp(text, segment_id=answer.segment_id)


class AgentTranscriptJournal:
    """Runner-neutral semantic transcript shared by live detail views.

    Tool progress snapshots are coalesced in retained history so a long-running
    stream cannot grow the mounted card without bound. Live subscribers still
    receive every update in order.
    """

    def __init__(self) -> None:
        self._operations: list[AgentTranscriptOp] = []
        self._progress_positions: dict[str, int] = {}
        self._subscribers: WeakSet[AgentTranscriptSurface] = WeakSet()
        self._retention_released = False
        self._interrupted = False
        self._failed = False

    def record(self, operation: AgentTranscriptOp) -> None:
        """Retain and fan out one semantic operation."""
        if self._retention_released:
            return
        if self._interrupted:
            return
        if isinstance(operation, TranscriptResumedOp):
            self._failed = False
        elif self._failed and not isinstance(operation, TranscriptInterruptedOp):
            return
        if isinstance(operation, TranscriptAnswerWithdrawnOp):
            # Retained no longer, so a view opened later never shows it; open views remove it.
            self._withdraw(frozenset(operation.segment_ids))
        elif isinstance(operation, TranscriptToolProgressOp) and operation.call_id in self._progress_positions:
            self._operations[self._progress_positions[operation.call_id]] = operation
        else:
            self._operations.append(operation)
            if isinstance(operation, TranscriptToolProgressOp):
                self._progress_positions[operation.call_id] = len(self._operations) - 1
        if isinstance(operation, TranscriptToolResultOp):
            self._progress_positions.pop(operation.call_id, None)
        if isinstance(operation, TranscriptErrorOp):
            # Retry keeps the invocation identity. Ignore late activity until
            # the scheduler explicitly starts its next attempt.
            self._failed = True
            self._progress_positions.clear()
        if isinstance(operation, TranscriptInterruptedOp):
            # Cancellation can precede provider/tool drain. Late events must
            # not resurrect cancelled activity in live or reopened views.
            self._interrupted = True
            self._progress_positions.clear()
        for subscriber in tuple(self._subscribers):
            subscriber.enqueue(operation)

    def _withdraw(self, segment_ids: frozenset[str]) -> None:
        self._operations = [
            operation
            for operation in self._operations
            if not (isinstance(operation, TranscriptAssistantOp) and operation.segment_id in segment_ids)
        ]
        self._progress_positions = {
            operation.call_id: position
            for position, operation in enumerate(self._operations)
            if isinstance(operation, TranscriptToolProgressOp) and operation.call_id in self._progress_positions
        }

    def continue_from(self, earlier: AgentTranscriptJournal) -> None:
        """Take *earlier*'s retained transcript as this one's beginning: an attempt resuming another.

        Called on an empty journal before its first operation; live subscribers receive the carried
        operations in order. The answer the resumed attempt asks for again is withdrawn after the
        resume (:class:`TranscriptAnswerWithdrawnOp`).
        """
        operations = list(earlier.operations)
        self._operations.extend(operations)
        self._progress_positions = {
            call_id: position + len(self._operations) - len(operations)
            for call_id, position in earlier._progress_positions.items()
        }
        for subscriber in tuple(self._subscribers):
            for operation in operations:
                subscriber.enqueue(operation)

    def finalize_retention(self, *, durable_replay_available: bool) -> None:
        """Seal terminal history, releasing or bounding retained operations.

        Existing surfaces have already received each operation through their
        own render queues. When durable replay exists, newly opened surfaces
        can use it and this duplicate is discarded. Otherwise a bounded
        semantic tail preserves useful details in non-persistent environments.
        """
        self._retention_released = True
        if durable_replay_available:
            self._operations.clear()
        else:
            self._operations = _compact_terminal_operations(self._operations)
        self._progress_positions.clear()

    def subscribe(self, surface: AgentTranscriptSurface) -> tuple[AgentTranscriptOp, ...]:
        """Subscribe and return the retained snapshot without an await gap."""
        self._subscribers.add(surface)
        return tuple(self._operations)

    def unsubscribe(self, surface: AgentTranscriptSurface) -> None:
        """Stop forwarding live operations to a detached surface."""
        self._subscribers.discard(surface)

    @property
    def operations(self) -> tuple[AgentTranscriptOp, ...]:
        """Return the retained semantic transcript."""
        return tuple(self._operations)

    @property
    def has_activity(self) -> bool:
        """Whether live routing recorded anything beyond a terminal answer."""
        return any(
            not (isinstance(operation, TranscriptAssistantOp) and operation.final) for operation in self._operations
        )


def _truncate_terminal_text(value: str, limit: int = _TERMINAL_JOURNAL_TEXT_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


def _compact_terminal_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Keep ordinary structured payloads intact and summarize oversized ones."""
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except TypeError, ValueError:
        serialized = repr(value)
    if len(serialized) <= _TERMINAL_JOURNAL_STRUCTURED_LIMIT:
        return dict(value)
    return {"summary": _truncate_terminal_text(serialized, _TERMINAL_JOURNAL_STRUCTURED_LIMIT)}


def _compact_terminal_operation(operation: AgentTranscriptOp) -> AgentTranscriptOp:
    """Drop heavyweight terminal-only payloads while preserving replay shape."""
    if isinstance(operation, TranscriptUserOp | TranscriptAssistantOp):
        return replace(operation, text=_truncate_terminal_text(operation.text))
    if isinstance(operation, TranscriptErrorOp) and isinstance(operation.reason, str):
        return replace(operation, reason=_truncate_terminal_text(operation.reason))
    if isinstance(operation, TranscriptRetryOp) and isinstance(operation.message, str):
        return replace(operation, message=_truncate_terminal_text(operation.message))
    if isinstance(operation, TranscriptToolStartOp):
        return replace(operation, args=_compact_terminal_mapping(operation.args))
    if isinstance(operation, TranscriptToolArgsOp):
        return replace(operation, args=_compact_terminal_mapping(operation.args))
    if isinstance(operation, TranscriptToolStatusOp):
        return replace(operation, metadata=_compact_terminal_mapping(operation.metadata))
    if isinstance(operation, TranscriptToolProgressOp):
        lines = [_truncate_terminal_text(line, 2_048) for line in operation.lines[-32:]]
        return replace(
            operation,
            lines=lines,
            image_contents=[],
            snapshot_metadata=_compact_terminal_mapping(operation.snapshot_metadata),
        )
    if isinstance(operation, TranscriptToolResultOp):
        return replace(
            operation,
            result=_truncate_terminal_text(operation.result),
            image_contents=[],
            artifacts=[],
            metadata=_compact_terminal_mapping(operation.metadata),
        )
    if isinstance(operation, TranscriptCompactionFinishedOp):
        return replace(
            operation,
            format_violation=_truncate_terminal_text(operation.format_violation, 4_096),
            failure_reason=_truncate_terminal_text(operation.failure_reason, 4_096),
        )
    return operation


def _compact_terminal_operations(
    operations: list[AgentTranscriptOp],
) -> list[AgentTranscriptOp]:
    """Retain a bounded semantic tail with starts needed by terminal updates."""
    if len(operations) <= _TERMINAL_JOURNAL_MAX_OPERATIONS:
        retained = list(operations)
    else:
        retained = list(operations[-_TERMINAL_JOURNAL_MAX_OPERATIONS:])
        retained_call_ids = {
            operation.call_id for operation in retained if isinstance(operation, TranscriptToolStartOp)
        }
        referenced_call_ids = {
            operation.call_id
            for operation in retained
            if isinstance(
                operation,
                TranscriptToolArgsOp | TranscriptToolStatusOp | TranscriptToolProgressOp | TranscriptToolResultOp,
            )
        }
        retained_compaction_ids = {
            operation.compaction_id for operation in retained if isinstance(operation, TranscriptCompactionStartOp)
        }
        referenced_compaction_ids = {
            operation.compaction_id for operation in retained if isinstance(operation, TranscriptCompactionFinishedOp)
        }
        missing_start_keys = {("tool", call_id) for call_id in referenced_call_ids - retained_call_ids} | {
            ("compaction", compaction_id) for compaction_id in referenced_compaction_ids - retained_compaction_ids
        }
        starts: dict[
            tuple[str, str],
            tuple[int, TranscriptToolStartOp | TranscriptCompactionStartOp],
        ] = {}
        if missing_start_keys:
            prefix = operations[:-_TERMINAL_JOURNAL_MAX_OPERATIONS]
            for index in range(len(prefix) - 1, -1, -1):
                operation = prefix[index]
                if isinstance(operation, TranscriptToolStartOp):
                    key = ("tool", operation.call_id)
                elif isinstance(operation, TranscriptCompactionStartOp):
                    key = ("compaction", operation.compaction_id)
                else:
                    continue
                if key in missing_start_keys and key not in starts:
                    starts[key] = (index, operation)
        preserved_starts = [operation for _, operation in sorted(starts.values(), key=lambda item: item[0])]
        retained = preserved_starts + retained
    return [_compact_terminal_operation(operation) for operation in retained]


@dataclass(frozen=True, slots=True)
class _TranscriptLocalization:
    """Resolve renderer copy lazily, after the surface has its mounting locale."""

    resolve: Callable[[MessageRef], str]

    def render(self, reference: MessageRef) -> str:
        return self.resolve(reference)


class AgentTranscriptSurface(VerticalScroll, can_focus=True):
    """A nested transcript using the main live renderer and tool registry.

    It opens at its newest content and, as the chat does, keeps up with what
    lands below while the user leaves it at the bottom.
    """

    DEFAULT_CSS = """
    AgentTranscriptSurface {
        height: 1fr;
        min-height: 8;
        padding: 0;
        background: transparent;
        scrollbar-size: 1 1;
    }
    """

    def __init__(
        self,
        journal: AgentTranscriptJournal,
        *,
        profile_name: str = "",
        opening_prompt: str = "",
        fallback_final_text: str = "",
        persisted_replay_loader: Callable[[], Awaitable[PersistedSubAgentTranscript | None]] | None = None,
        persisted_replay: PersistedSubAgentTranscript | None = None,
        replay_tail: tuple[AgentTranscriptOp, ...] = (),
        **kwargs: Any,
    ) -> None:
        from chrys.app.tui.widgets.chat.live_renderer import LiveTranscriptRenderer
        from chrys.app.tui.widgets.chat.replay import HistoryReplayRenderer
        from chrys.app.tui.widgets.chat.tool_registry import ToolGroupRegistry

        super().__init__(**kwargs)
        self._scroll_gc_guard = ManualScrollGcGuard(self)
        self._bottom_follow = BottomFollow(self)
        self._journal = journal
        self._profile_name = profile_name
        self._opening_prompt = opening_prompt
        self._fallback_final_text = fallback_final_text
        self._persisted_replay_loader = persisted_replay_loader
        self._persisted_replay = persisted_replay
        self._replay_tail = replay_tail
        self._loading_persisted_replay = False
        self._pending: deque[AgentTranscriptOp] = deque()
        self._draining = False
        self._drain_scheduled = False
        self._tools = ToolGroupRegistry(
            self,
            self,
            self,
            finalize_agent_message=self._settle_answer_before_tools,
            tool_groups_expanded=lambda: False,
        )
        localization = _TranscriptLocalization(self._render_message)
        self._live = LiveTranscriptRenderer(
            self,
            self,
            self,
            self,
            TurnTocModel(),
            self._tools,
            localization=localization,
            cleanup_failed_input=True,
        )
        self._live.set_profile(profile_name)
        self._replay = HistoryReplayRenderer(
            self,
            self,
            TurnTocModel(),
            lambda _tool_name: "",
            default_profile=lambda: self._profile_name,
            localization=localization,
        )
        # Subscribe before this widget is handed to Textual. The parent tool
        # can become terminal between surface construction and on_mount(); a
        # durable completion releases the retained journal, so waiting until
        # mount would miss the authoritative final operation entirely.
        self._pending.extend(self._journal.subscribe(self))

    def focus_on_click(self) -> bool:
        """Keep passive transcript clicks from stealing composer focus."""
        return False

    def set_profile(self, profile_name: str) -> None:
        """Update the assistant label used by subsequently mounted messages."""
        self._profile_name = profile_name
        self._live.set_profile(profile_name)

    async def on_mount(self) -> None:
        self.watch(self.vertical_scrollbar, "grabbed", self._on_scrollbar_grabbed_changed, init=False)
        self._loading_persisted_replay = self._persisted_replay_loader is not None
        try:
            replay = self._persisted_replay
            if self._persisted_replay_loader is not None:
                try:
                    replay = await self._persisted_replay_loader()
                except Exception:
                    replay = None
            if replay is not None and not replay.terminal_audit:
                replay = None
            if replay is not None:
                # A terminal audit is the complete replacement for the live
                # journal snapshot. Dropping the snapshot here prevents
                # duplicate replay and lazily releases cancelled-card memory
                # without a dedicated audit-ready event.
                self._pending.clear()
                self._journal.finalize_retention(durable_replay_available=True)
            if self._opening_prompt and not self._replay_starts_with_user(replay):
                await self._live.add_user_message(self._opening_prompt)
            if replay is not None and replay.messages:
                if replay.profile_name:
                    self.set_profile(replay.profile_name)
                await self._replay.replay_history(
                    [dict(message) for message in replay.messages],
                    initial_profile=replay.profile_name or self._profile_name,
                )
                if replay.covers_result_text(self._fallback_final_text):
                    self._pending = deque(
                        operation
                        for operation in self._pending
                        if not (isinstance(operation, TranscriptAssistantOp) and operation.final)
                    )
            replay_covers_fallback = bool(replay is not None and replay.covers_result_text(self._fallback_final_text))
            if (
                self._fallback_final_text
                and not replay_covers_fallback
                and not any(
                    isinstance(operation, TranscriptAssistantOp) and operation.final for operation in self._pending
                )
            ):
                self._pending.append(TranscriptAssistantOp(self._fallback_final_text, final=True))
            if replay is not None:
                self._pending.extend(self._replay_tail)
        finally:
            self._loading_persisted_replay = False
            self._schedule_drain()

    @staticmethod
    def _replay_starts_with_user(replay: PersistedSubAgentTranscript | None) -> bool:
        if replay is None:
            return False
        return bool(replay.messages and replay.messages[0].get("role") == "user")

    def on_unmount(self) -> None:
        self._journal.unsubscribe(self)
        self._pending.clear()
        self._drain_scheduled = False
        self._live.reset_for_clear()
        self._tools.reset_for_clear()
        self._scroll_gc_guard.stop()

    def _on_scrollbar_grabbed_changed(self, grabbed: object) -> None:
        self._scroll_gc_guard.set_scrollbar_grabbed(grabbed is not None)

    def scroll_to(self, x: float | None = None, y: float | None = None, **kwargs: Any) -> None:
        """Let the bottom follow wait for a scroll Textual runs after the next refresh.

        A key's or the scrollbar's scroll heads for a row worked out from where
        the view is now: were the follow to take the view to new content first,
        Down at the bottom would scroll back up.
        """
        deferred = y is not None and not kwargs.get("immediate", False)
        if deferred:
            self._bottom_follow.defer()
        super().scroll_to(x, y, **kwargs)
        # Posted after Textual's own deferred scroll, so it runs once that has.
        if deferred and not self.call_after_refresh(self._bottom_follow.deferred_ran):
            self._bottom_follow.deferred_ran()

    def _scroll_to(self, x: float | None = None, y: float | None = None, **kwargs: Any) -> bool:
        with self._bottom_follow.scroll(y):
            return super()._scroll_to(x, y, **kwargs)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Keep cyclic GC out of manual and animated scroll frames, but for the moves that follow the bottom."""
        super().watch_scroll_y(old_value, new_value)
        self._bottom_follow.scrolled(old_value, new_value)
        if round(old_value) != round(new_value) and not self._bottom_follow.pinning:
            self._scroll_gc_guard.pause()

    def watch_virtual_size(self) -> None:
        self._bottom_follow.resized()

    def on_resize(self) -> None:
        self._bottom_follow.resized()

    def _on_mouse_scroll_up(self, event: textual_events.MouseScrollUp) -> None:
        """Pause GC before Textual starts the wheel scroll."""
        self._scroll_gc_guard.pause()

    def _on_mouse_scroll_down(self, event: textual_events.MouseScrollDown) -> None:
        """Pause GC before Textual starts the wheel scroll."""
        self._scroll_gc_guard.pause()

    def _on_scroll_to(self, message: ScrollTo) -> None:
        """Pause GC before a scrollbar position update."""
        self._scroll_gc_guard.pause()

    def enqueue(self, operation: AgentTranscriptOp) -> None:
        """Queue one live operation for ordered rendering."""
        self._pending.append(operation)
        self._schedule_drain()

    def _accepts_transcript_updates(self) -> bool:
        """Whether dynamic transcript mounts may still attach to this surface."""
        return self.is_attached and not self._closing and not self._pruning

    def _schedule_drain(self) -> None:
        if (
            self._loading_persisted_replay
            or not self._accepts_transcript_updates()
            or self._draining
            or self._drain_scheduled
        ):
            return
        self._drain_scheduled = True
        self.call_later(self._drain_pending)

    async def _drain_pending(self) -> None:
        self._drain_scheduled = False
        if self._draining or not self._accepts_transcript_updates():
            if self._closing or self._pruning:
                self._pending.clear()
            return
        self._draining = True
        try:
            while self._pending and self._accepts_transcript_updates():
                await self._apply(self._pending.popleft())
        finally:
            self._draining = False
        if not self._accepts_transcript_updates():
            if self._closing or self._pruning:
                self._pending.clear()
                self._live.reset_for_clear()
                self._tools.reset_for_clear()
            return
        self.refresh(layout=True)

    async def _apply(self, operation: AgentTranscriptOp) -> None:
        if isinstance(operation, TranscriptUserOp):
            await self._live.add_user_message(operation.text)
        elif isinstance(operation, TranscriptAssistantOp):
            if operation.final:
                await self._live.add_agent_message(operation.text, streamed=operation.streamed)
            else:
                await self._live.add_answer_delta(operation.segment_id, operation.text)
        elif isinstance(operation, TranscriptToolStartOp):
            await self.remove_trailing_status()
            await self._tools.add_tool_start(
                operation.call_id,
                operation.tool_name,
                operation.tool_kind,
                json.dumps(operation.args, ensure_ascii=False, default=str),
                args=dict(operation.args),
                provider_hosted=operation.provider_hosted,
                hosted_family=operation.hosted_family,
                provider=operation.provider,
                provider_item_type=operation.provider_item_type,
                provider_status=operation.provider_status,
                provider_call_id=operation.provider_call_id,
                started_at=operation.started_at,
                # A running call's start can come again (ACP: when what it shows changes), after later output
                # ended its group: it updates the card where it is and never opens another.
                running_in_place=True,
            )
        elif isinstance(operation, TranscriptToolArgsOp):
            self._tools.update_tool_args(operation.call_id, dict(operation.args))
        elif isinstance(operation, TranscriptToolStatusOp):
            self._tools.update_tool_status(
                operation.call_id,
                operation.status,
                provider_status=operation.provider_status,
                metadata=dict(operation.metadata),
                stopped_at=operation.stopped_at,
            )
        elif isinstance(operation, TranscriptToolProgressOp):
            self._tools.update_tool_progress(
                operation.call_id,
                list(operation.lines),
                image_contents=list(operation.image_contents),
                snapshot_metadata=dict(operation.snapshot_metadata),
                provider_status=operation.provider_status,
            )
        elif isinstance(operation, TranscriptToolResultOp):
            await self._tools.add_tool_result(
                operation.call_id,
                operation.tool_name,
                operation.result,
                operation.duration_ms,
                image_contents=list(operation.image_contents),
                approval=operation.approval,
                metadata=dict(operation.metadata),
                artifacts=[dict(artifact) for artifact in operation.artifacts],
                provider_status=operation.provider_status,
                canonical_status=operation.canonical_status,
            )
        elif isinstance(operation, TranscriptCompactionStartOp):
            await self._live.add_compaction_start(operation.compaction_id)
        elif isinstance(operation, TranscriptCompactionFinishedOp):
            self._live.complete_compaction(
                operation.compaction_id,
                outcome=operation.outcome,
                duration_ms=operation.duration_ms,
                format_violation=operation.format_violation,
                failure_reason=operation.failure_reason,
            )
        elif isinstance(operation, TranscriptInterruptedOp):
            await self._live.add_interrupted(
                self._render_message(operation.reason),
                source="system",
                show_action=False,
                stopped_at=operation.stopped_at,
            )
        elif isinstance(operation, TranscriptErrorOp):
            await self._live.add_error(
                self._render_message(operation.reason), action_label=None, stopped_at=operation.stopped_at
            )
        elif isinstance(operation, TranscriptPausedOp):
            # As the main chat's error card does; Retry then removes the text, Abort keeps it.
            self._live.stop_answer()
        elif isinstance(operation, TranscriptResumedOp):
            # Released as intermediate text: the answer the resumed attempt asks for again is withdrawn next.
            self._live.settle_answer_before_tools()
            await self._live.prepare_retry(stopped_at=operation.stopped_at)
            await self.remove_trailing_status()
        elif isinstance(operation, TranscriptAnswerWithdrawnOp):
            await remove_withdrawn_answer(self, operation.segment_ids)
        elif isinstance(operation, TranscriptRetryOp):
            message = self._render_message(operation.message)
            if operation.hint is not None:
                message = render_str(
                    widget_localizer(self),
                    DISPLAY_WITH_HINT.bind(message=message, hint=self._render_message(operation.hint)),
                )
            if operation.compaction:
                self._live.show_compaction_retry(
                    message, operation.attempt, operation.max_attempts, operation.delay_seconds
                )
            else:
                await self._live.prepare_retry()
                await self._live.add_retry(message, operation.attempt, operation.max_attempts, operation.delay_seconds)
        elif isinstance(operation, TranscriptWarningOp):
            await self._live.add_system(self._render_message(operation.message), warning=True)

    def _render_message(self, message: str | MessageRef) -> str:
        return message if isinstance(message, str) else render_str(widget_localizer(self), message)

    def _settle_answer_before_tools(self) -> None:
        # Nested transcripts fold nothing as tools start.
        self._live.settle_answer_before_tools()

    async def mount_transcript_widget(self, widget: Widget) -> bool:
        if not self._accepts_transcript_updates():
            return False
        precompose_tree([widget])
        await self.mount(widget)
        return widget.is_attached and not widget._closing and not widget._pruning and self._accepts_transcript_updates()

    async def mount_transcript_widgets(
        self, widgets: list[Widget], *, process_failures_after: tuple[Widget, ...] = ()
    ) -> None:
        # Nested transcripts do not fold completed turns.
        _ = process_failures_after
        if not self._accepts_transcript_updates():
            return
        precompose_tree(widgets)
        await self.mount(*widgets)

    async def remove_transcript_widget(self, widget: Widget) -> None:
        if widget.parent is self:
            await remove_shielded(widget)

    async def dismiss_welcome_for_content(self) -> None:
        return

    def on_live_user_turn_started(self) -> None:
        return

    def on_replay_started(self) -> None:
        return

    def scroll_user_message_to_top(self, widget: Widget) -> None:
        self.scroll_to_widget(widget, top=True, animate=False)

    def on_final_response_started(self) -> None:
        return

    def schedule_anchor_sync(self) -> None:
        return

    def on_status_message_mounted(self) -> None:
        self.schedule_anchor_sync()

    def scroll_inline_prompt_to_top_after_refresh(self, widget: Widget) -> None:
        self.call_after_refresh(self.scroll_to_widget, widget, top=True, animate=False)

    def on_inline_prompt_resized(self, group: ToolGroup | None) -> None:
        _ = group
        self.refresh(layout=True)

    def after_replay_history_mounted(self) -> None:
        self.schedule_anchor_sync()

    def pre_layout(self, layout: Layout) -> None:
        show_fold_gaps(self.displayed_children)

    async def remove_trailing_status(self) -> None:
        status = self.failed_turn_status()
        if isinstance(status, (ErrorMessage, InterruptedMessage, RetryMessage)):
            await remove_shielded(status)

    def failed_turn_status(self) -> Widget | None:
        return next(
            (child for child in reversed(self.children) if not isinstance(child, SystemMessage)),
            None,
        )

    def preceding_content(self, widget: Widget) -> Widget | None:
        previous = None
        for child in self.children:
            if child is widget:
                return previous
            previous = child
        return None

    def mounted_tool_groups(self) -> list[ToolGroup]:
        return list(self.query(ToolGroup))

    def mounted_agent_messages(self) -> list[AgentMessage]:
        return list(self.query(AgentMessage))

    def mounted_user_messages(self) -> list[UserMessage]:
        return list(self.query(UserMessage))

    def direct_children(self) -> list[Widget]:
        return list(self.children)

    def transcript_content_children(self) -> list[Widget]:
        return list(self.children)
