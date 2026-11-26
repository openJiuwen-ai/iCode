# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming progress for headless text mode: one stderr line per meaningful event.

Progress goes to stderr and the final answer to stdout, so a redirected answer
stays exactly what it was. Lines are only ever appended — nothing animates or
moves the cursor — and Rich decides colour alone, so a log file or a pipe with
``FORCE_COLOR`` reads the same as a terminal. Granularity is one line per
event, never per token.

Every dynamic string is display text from a model, a tool, a remote agent or
the user's configuration: it is sanitized before it reaches the console.

Progress never costs the run its answer: a stderr that fails or whose reader
has gone ends progress, and the answer still reaches stdout.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import sys
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import IO, Any, Final

from rich.console import Console
from rich.text import Text

from chrys.app.cli.headless import warning_text
from chrys.app.cli.tool_summary import (
    ToolSummary,
    clamp,
    failure_reason,
    hosted_status_is_terminal,
    is_todo_tool,
    summarize_tool,
    tool_failed,
)
from chrys.app.tui.util.context_pressure import context_pressure_message
from chrys.foundation.errors.display import with_display_hint
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AGENT_LOAD_PHASE_MCP,
    AGENT_LOAD_STATUS_RUNNING,
    WORKFLOW_NOTICE_DATA_DROPPED,
    WORKFLOW_OUTPUT_EMIT,
    AgentLoadFinished,
    AgentLoadProgress,
    CompactionFinished,
    CompactionStarted,
    Event,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationContextPressure,
    InvocationEvent,
    InvocationMessage,
    InvocationPaused,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationStarted,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    TodoListUpdated,
    ToolCompacted,
    Warning,
    WorkflowLoopIteration,
    WorkflowNodeOutput,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
    WorkflowRunNotice,
    WorkflowRunStarted,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar, sanitize_terminal_block
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.todos import TodoItem
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.session_ids import session_short_id

logger = logging.getLogger(__name__)

QUIET_NOTICES: Final = frozenset({WORKFLOW_NOTICE_DATA_DROPPED})
"""Notice codes retained in run events and records but omitted from CLI progress."""

_MESSAGE_LIMIT: Final = 240
_DIM: Final = "dim"
_OK: Final = "green"
_FAIL: Final = "red"
_NOTE: Final = "yellow"
_ACCENT: Final = "cyan"

_NAMED_EVENTS: Final = (
    InvocationStarted,
    InvocationMessage,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationContentAbandoned,
    InvocationToolCallStart,
    InvocationToolCallArgsUpdated,
    InvocationToolCallStatusUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationProgress,
    InvocationCompactionStarted,
    InvocationCompactionFinished,
    InvocationCompactionCommitted,
    InvocationRetryAttempt,
    InvocationPaused,
    InvocationResumed,
    InvocationCascadeAborted,
    InvocationAborted,
)
"""Invocation events that carry the agent's display name."""

type Render = Callable[[MessageRef], str]
type Part = str | tuple[str, str]
type CallKey = tuple[str, int, str]


class _ProgressConsole(Console):
    """A stderr console whose broken pipe is an ordinary write failure.

    Rich's default policy points *stdout* at devnull and exits, which would drop the answer still to come.
    """

    def on_broken_pipe(self) -> None:
        self.quiet = True
        raise BrokenPipeError(errno.EPIPE, "The progress stream was closed.")


def progress_console() -> Console:
    """The stderr console progress writes to; built per command so a replaced ``sys.stderr`` is honoured."""
    return _ProgressConsole(file=sys.stderr, highlight=False, markup=False, emoji=False, soft_wrap=True)


def _discard(stream: IO[str]) -> None:
    """Point a stream whose reader has gone at devnull, so later writes and the exit-time flush succeed."""
    try:
        descriptor = stream.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError, ValueError:
        return
    try:
        os.dup2(devnull, descriptor)
    except OSError:
        pass
    finally:
        os.close(devnull)


def guarded(handle: Callable[[Event], None]) -> Callable[[Event], None]:
    """*handle* as a stream callback: a display failure is logged and dropped, never costing the run its answer."""

    def on_event(event: Event) -> None:
        try:
            handle(event)
        except Exception:
            logger.exception("Headless progress could not show %s", type(event).__name__)

    return on_event


def format_duration(seconds: float) -> str:
    """``0.4s``, ``12.3s``, ``2m 14s``, ``1h 05m``, ``1d 04h``."""
    seconds = max(seconds, 0.0)
    if round(seconds, 1) < 60:
        return f"{seconds:.1f}s"
    total = round(seconds)
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    if total >= 86400:
        return f"{total // 86400}d {total % 86400 // 3600:02d}h"
    return f"{total // 3600}h {total % 3600 // 60:02d}m"


def format_tokens(count: int) -> str:
    """``950``, ``41k``, ``1.2M``."""
    if count < 1000:
        return str(count)
    # Choose the unit after rounding: 999,600 is 1.0M, not 1000k.
    thousands = round(count / 1000)
    if thousands < 1000:
        return f"{thousands}k"
    return f"{count / 1_000_000:.1f}M"


def display_path(path: str) -> str:
    """A path for display: surrogates neutralized, the home directory shortened to ``~``."""
    text = surrogate_safe_text(path)
    home = surrogate_safe_text(os.path.expanduser("~"))
    if home not in ("", "~") and os.path.normcase(text).startswith(os.path.normcase(home)):
        rest = text[len(home) :]
        if not rest or rest[0] in (os.sep, "/"):
            text = f"~{rest}"
    return sanitize_legacy_scalar(text)


def _scalar(value: str, limit: int = _MESSAGE_LIMIT) -> str:
    return clamp(_unclamped(value), limit)


def _short_id(value: str) -> str:
    # Restored and persisted ids are not guaranteed hexadecimal; the raw id stays the identity elsewhere.
    return _unclamped(session_short_id(value))


def _unclamped(value: str) -> str:
    """One display line kept whole: warnings, notices and emits are already bounded by their producers."""
    return sanitize_legacy_scalar(surrogate_safe_text(value)).strip()


def _error_lines(error: str) -> list[str]:
    """A node error's non-empty lines, sanitized for the terminal; its cause often sits below the first."""
    return [line for line in sanitize_terminal_block(surrogate_safe_text(error)).splitlines() if line.strip()]


class ProgressWriter:
    """Appends styled lines to the console and remembers what the last line was.

    The memory serves two rules: a tool's end line is compact only when it
    directly follows that tool's start line, and a warning is printed once per
    ``(code, message)`` across everything this command has already reported.

    A ``quiet`` writer (``--quiet``) prints only the warnings: every mode renders
    the same events, so quiet output never misses a warning progress would show.
    """

    def __init__(
        self, console: Console, *, reported_warnings: Iterable[tuple[str, str]] = (), quiet: bool = False
    ) -> None:
        self._console = console
        self._last_start: CallKey | None = None
        self._warnings = set(reported_warnings)
        self._quiet = quiet
        self._failed = False

    def line(self, *parts: Part, start_of: CallKey | None = None) -> None:
        if not self._quiet:
            self._print(parts)
        self._last_start = start_of

    def always(self, *parts: Part) -> None:
        """A line every mode prints, quiet included: warnings and the run's captured output."""
        self._print(parts)
        self._last_start = None

    def _print(self, parts: Iterable[Part]) -> None:
        if self._failed:
            return
        text = Text()
        for part in parts:
            if isinstance(part, tuple):
                text.append(part[0], style=part[1])
            else:
                text.append(part)
        text.rstrip()
        try:
            self._console.print(text)
        except (OSError, ValueError) as exc:
            # A failing or closed stderr ends progress, never the run.
            self._failed = True
            if isinstance(exc, BrokenPipeError):
                _discard(self._console.file)
            logger.debug("Headless progress stopped writing", exc_info=True)

    def follows_start_of(self, key: CallKey) -> bool:
        return self._last_start == key

    def mark_reported(self, code: str, message: str) -> None:
        self._warnings.add((code, message))

    def unreported(self, warnings: Iterable[Warning]) -> list[Warning]:
        """The warnings not reported yet, now marked reported."""
        fresh: list[Warning] = []
        for warning in warnings:
            key = (warning.code, warning.message)
            if key not in self._warnings:
                self._warnings.add(key)
                fresh.append(warning)
        return fresh

    def warning(self, message: str, *, lead: str = "") -> None:
        self.always(lead, ("Warning: ", _NOTE), _unclamped(message))


@dataclass(slots=True)
class _Call:
    summary: ToolSummary
    tool_name: str
    tool_kind: str
    hosted_family: str
    started: datetime
    lead: str
    nested: bool
    silent: bool
    ended: bool = False
    failure_shown: bool = False


@dataclass(slots=True)
class _PendingAnswer:
    """Answer text streamed for one invocation attempt whose role is not known yet."""

    lead: str
    segments: dict[str, list[str]] = field(default_factory=dict)


def _depth(origin: InvocationOrigin) -> int:
    depth = 0
    current: InvocationOrigin | None = origin
    while current is not None:
        if current.kind == "sub_agent":
            depth += 1
        current = current.parent
    return depth


class InvocationActivity:
    """Renders ``Invocation*`` facts: tools, sub-agents, retries, compaction, pressure and commentary.

    ``prefix(origin)`` returns the lead every line for that origin starts with
    (``""`` for a chat turn, ``"  [node] "`` for a workflow node), or ``None``
    when the origin is not this command's to show.

    Streamed answer text waits until its role is known: text a tool call
    follows is commentary, text the final message completes is the answer,
    which goes to stdout, and the partial text of a failed or interrupted
    pass is commentary too. Text a retry gave up is never shown, unless a
    hosted card that started mid-stream already printed it. Reasoning is
    never shown.
    """

    def __init__(self, writer: ProgressWriter, *, prefix: Callable[[InvocationOrigin], str | None], render: Render):
        self._writer = writer
        self._prefix = prefix
        self._render = render
        self._calls: dict[CallKey, _Call] = {}
        self._names: dict[str, str] = {}
        self._answers: dict[tuple[str, int], _PendingAnswer] = {}
        self.top_level_calls = 0

    def handle(self, event: InvocationEvent) -> None:
        origin = event.origin
        prefix = self._prefix(origin)
        if prefix is None:
            return
        name = self._agent_name(event)
        nested = _depth(origin) > 0
        lead = f"{prefix}{'  ' * _depth(origin)}↳ {name} " if nested else prefix
        match event:
            case InvocationStarted():
                return
            case InvocationToolCallStart():
                self._flush_answer(origin, lead)
                self._tool_start(event, lead, nested=nested)
            case InvocationToolCallArgsUpdated():
                self._tool_args(event)
            case InvocationToolCallStatusUpdated():
                self._tool_status(event)
            case InvocationToolCallResult():
                self._tool_result(event)
            case InvocationContentDelta():
                if not nested and event.channel == "answer":
                    # Child prose stays private.
                    key = (origin.invocation_id, origin.attempt)
                    answer = self._answers.get(key)
                    if answer is None:
                        answer = self._answers[key] = _PendingAnswer(lead)
                    answer.segments.setdefault(event.segment_id, []).append(event.text)
            case InvocationContentAbandoned():
                # A retry gave this text up: it was never shown, and the run keeps none of it.
                answer = self._answers.get((origin.invocation_id, origin.attempt))
                if answer is not None:
                    for segment_id in event.segment_ids:
                        answer.segments.pop(segment_id, None)
            case InvocationContentFinished():
                if event.status != "completed":
                    self._flush_answer(origin, lead)
            case InvocationMessage():
                # The final answer goes to stdout; text it does not hold was commentary no tool card followed.
                answer = self._answers.pop((origin.invocation_id, origin.attempt), None)
                for parts in answer.segments.values() if answer is not None else ():
                    if (text := "".join(parts)) not in event.text:
                        self._commentary(lead, text)
            case InvocationRetryAttempt():
                self._retry(event, lead)
            case InvocationPaused():
                reason = self._display(event.last_error_display, event.last_error, event.last_error_hint)
                self._writer.line(lead, ("paused", _NOTE), f": {reason}" if reason else "")
            case InvocationResumed():
                self._writer.line(lead, ("resumed", _DIM))
            case InvocationAborted():
                self._writer.line(lead, ("✗ ", _FAIL), _scalar(event.last_error) or "failed")
            case InvocationCascadeAborted():
                self._writer.line(lead, ("cancelled", _DIM))
            case InvocationCompactionStarted():
                self._status(lead, "Compacting conversation…", nested=nested)
            case InvocationCompactionFinished():
                if event.outcome == "canceled":
                    self._status(lead, "Compaction interrupted", nested=nested)
                elif event.outcome == "failed":
                    self._compaction_failed(lead, event.failure_reason)
            case InvocationCompactionCommitted():
                self._status(lead, "Conversation compacted", nested=nested)
            case InvocationContextPressure():
                message = self._render(context_pressure_message(event.reason, source=event.source))
                self._writer.warning(message, lead=lead)
            case _:
                return

    def _agent_name(self, event: InvocationEvent) -> str:
        # Registered from any event that names its agent (InvocationStarted first), so an event without the field
        # (context pressure) still resolves through its origin.
        invocation_id = event.origin.invocation_id
        if isinstance(event, _NAMED_EVENTS) and event.agent_name.strip():
            self._names[invocation_id] = _scalar(event.agent_name, 60)
        return self._names.get(invocation_id, "sub-agent")

    def _status(self, lead: str, text: str, *, nested: bool) -> None:
        if nested:
            self._writer.line(lead, (text[0].lower() + text[1:], _DIM))
        else:
            self._writer.line(lead, ("• ", _ACCENT), text)

    def _compaction_failed(self, lead: str, reason: str) -> None:
        cause = _scalar(reason, 120)
        self._writer.warning(f"compaction failed ({cause})" if cause else "compaction failed", lead=lead)

    def _display(self, reference: MessageRef | None, fallback: str, hint: MessageRef | None = None) -> str:
        if reference is None:
            return _scalar(fallback)
        message = self._render(reference)
        if hint is not None:
            message = with_display_hint(message, self._render(hint), render=self._render)
        return _scalar(message)

    # -- tools -------------------------------------------------------------------------------

    @staticmethod
    def _key(origin: InvocationOrigin, call_id: str) -> CallKey:
        # Call ids repeat across invocations and across a workflow node's attempts.
        return (origin.invocation_id, origin.attempt, call_id)

    def _tool_start(self, event: InvocationToolCallStart, lead: str, *, nested: bool) -> None:
        key = self._key(event.origin, event.call_id)
        summary = summarize_tool(event.tool_name, event.tool_kind, event.args, hosted_family=event.hosted_family)
        call = self._calls.get(key)
        if call is not None:
            # ACP emits a call's start again when what it shows changes (title, kind, input, hosted details): the
            # open call keeps its first timestamp and only learns the newer description.
            call.summary = summary
            call.tool_name, call.tool_kind = event.tool_name, event.tool_kind
            call.hosted_family = event.hosted_family or call.hosted_family
            return
        silent = is_todo_tool(event.tool_name, event.tool_kind)
        self._calls[key] = _Call(
            summary=summary,
            tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            hosted_family=event.hosted_family,
            started=event.timestamp,
            lead=lead,
            nested=nested,
            silent=silent,
        )
        if silent:
            return
        if not nested:
            self.top_level_calls += 1
        self._writer.line(lead, ("→ ", _ACCENT), *self._describe(summary), start_of=key)

    def _tool_args(self, event: InvocationToolCallArgsUpdated) -> None:
        call = self._calls.get(self._key(event.origin, event.call_id))
        if call is None:
            return
        call.summary = summarize_tool(
            event.tool_name or call.tool_name,
            event.tool_kind or call.tool_kind,
            event.args,
            hosted_family=event.hosted_family or call.hosted_family,
        )

    def _tool_status(self, event: InvocationToolCallStatusUpdated) -> None:
        call = self._calls.get(self._key(event.origin, event.call_id))
        status = event.status or event.provider_status
        if call is None or call.ended or not hosted_status_is_terminal(status):
            return
        result_text = event.metadata.get("result_text")
        self._end(
            call,
            self._key(event.origin, event.call_id),
            result=result_text if isinstance(result_text, str) else "",
            metadata=event.metadata,
            provider_status=status,
            ended_at=event.timestamp,
            duration_ms=0,
        )

    def _tool_result(self, event: InvocationToolCallResult) -> None:
        key = self._key(event.origin, event.call_id)
        call = self._calls.pop(key, None)
        if call is None:
            return
        if not call.ended:
            self._end(
                call,
                key,
                result=event.result,
                metadata=event.metadata,
                provider_status=event.provider_status,
                ended_at=event.timestamp,
                duration_ms=event.duration_ms,
            )
            return
        # A hosted call already ended on its terminal status; its result only matters when it contradicts a
        # success that was shown.
        failed = tool_failed(
            event.result,
            event.metadata,
            tool_name=call.tool_name,
            tool_kind=call.tool_kind,
            hosted_family=call.hosted_family or event.hosted_family,
            provider_status=event.provider_status,
        )
        if failed and not call.failure_shown:
            reason = failure_reason(event.result, event.metadata, hosted_family=call.hosted_family)
            self._end_line(call, failed=True, reason=reason, elapsed=None, compact=False)

    def _end(
        self,
        call: _Call,
        key: CallKey,
        *,
        result: str,
        metadata: dict[str, object],
        provider_status: str,
        ended_at: datetime,
        duration_ms: int,
    ) -> None:
        call.ended = True
        failed = tool_failed(
            result,
            metadata,
            tool_name=call.tool_name,
            tool_kind=call.tool_kind,
            hosted_family=call.hosted_family,
            provider_status=provider_status,
        )
        if not failed and (call.silent or call.nested):
            return
        elapsed = duration_ms / 1000 if duration_ms > 0 else (ended_at - call.started).total_seconds()
        reason = failure_reason(result, metadata, hosted_family=call.hosted_family) if failed else ""
        compact = not call.nested and self._writer.follows_start_of(key)
        self._end_line(call, failed=failed, reason=reason, elapsed=elapsed, compact=compact)

    def _end_line(self, call: _Call, *, failed: bool, reason: str, elapsed: float | None, compact: bool) -> None:
        call.failure_shown = call.failure_shown or failed
        mark = ("✗ ", _FAIL) if failed else ("✓ ", _OK)
        tail: list[Part] = []
        if failed:
            tail.append(reason)
        if elapsed is not None:
            tail.append((format_duration(elapsed), _DIM))
        joined: list[Part] = []
        for index, part in enumerate(tail):
            if index:
                joined.append((" · ", _DIM))
            joined.append(part)
        indent = "" if call.nested else "  "
        if compact:
            self._writer.line(call.lead, indent, mark, *joined)
            return
        separator: list[Part] = [(" · ", _DIM)] if joined else []
        self._writer.line(call.lead, indent, mark, *self._describe(call.summary), *separator, *joined)

    @staticmethod
    def _describe(summary: ToolSummary) -> list[Part]:
        if not summary.detail:
            return [summary.label]
        return [f"{summary.label.ljust(5)}  ", summary.detail]

    # -- commentary --------------------------------------------------------------------------

    def flush_pending(self, invocation_id: str | None = None) -> None:
        """Print the answer text still held, all of it or *invocation_id*'s, as commentary.

        Its pass failed, so no later fact says what it was: a segment a
        streamed call closed has ended as ``completed`` even when that call
        never ran.
        """
        for key in [key for key in self._answers if invocation_id is None or key[0] == invocation_id]:
            answer = self._answers.pop(key)
            self._print_held(answer, answer.lead)

    def _flush_answer(self, origin: InvocationOrigin, lead: str) -> None:
        answer = self._answers.pop((origin.invocation_id, origin.attempt), None)
        if answer is not None:
            self._print_held(answer, lead)

    def _print_held(self, answer: _PendingAnswer, lead: str) -> None:
        for parts in answer.segments.values():
            self._commentary(lead, "".join(parts))

    def _commentary(self, lead: str, text: str) -> None:
        body = sanitize_terminal_block(surrogate_safe_text(text)).strip("\n")
        if not body.strip():
            return
        for line in body.splitlines():
            self._writer.line(lead, line)

    # -- retries -----------------------------------------------------------------------------

    def _retry(self, event: InvocationRetryAttempt, lead: str) -> None:
        message = self._display(event.display_message, event.message, event.display_hint).rstrip(" .")
        attempt = f" (attempt {event.attempt}/{event.max_attempts})" if event.max_attempts else ""
        wait = f"Retrying in {event.delay_seconds}s{attempt}"
        self._writer.line(lead, ("↻ ", _NOTE), f"{message}. {wait}" if message else wait)


@dataclass(frozen=True, slots=True)
class RunContext:
    """What the ready line names beyond the agent: the model profile and the working directory."""

    model: str = ""
    workdir: str = ""


def _todo_line(items: list[TodoItem]) -> str:
    if not items:
        return "Todo list cleared"
    completed = sum(1 for item in items if item.status == "completed")
    head = f"Todo {completed}/{len(items)}"
    active = next((item for item in items if item.status == "in_progress"), None)
    if active is not None:
        return f"{head} · → {_scalar(active.active_form or active.content, 160)}"
    pending = next((item for item in items if item.status == "pending"), None)
    if pending is not None:
        return f"{head} · next: {_scalar(pending.content, 160)}"
    return f"{head} · all done"


class TurnProgress:
    """Progress for ``icode run``: startup, the turn's activity, and a closing summary."""

    def __init__(self, writer: ProgressWriter, *, render: Render, context: Callable[[], RunContext]) -> None:
        self._writer = writer
        self._render = render
        self._context = context
        self._activity = InvocationActivity(writer, prefix=self._prefix, render=render)
        self._ready = False
        self._restored_name = ""

    @staticmethod
    def _prefix(origin: InvocationOrigin) -> str | None:
        return "" if origin.root.kind == "turn" else None

    def restoring(self, selector: str) -> None:
        self._writer.line(("• ", _ACCENT), f"Restoring session {_scalar(selector, 80)}…")

    @contextlib.asynccontextmanager
    async def observe_restore(self, bus: EventBus) -> AsyncGenerator[None]:
        """Show MCP connections and warnings while a restore runs before the turn's own event stream."""

        async def on_progress(event: AgentLoadProgress) -> None:
            self._load_progress(event)

        async def on_warning(event: Warning) -> None:
            self.warnings([event])

        async def on_finished(event: AgentLoadFinished) -> None:
            self._restored_name = event.display_name or event.agent_profile

        await bus.subscribe(AgentLoadProgress, on_progress)
        await bus.subscribe(Warning, on_warning)
        await bus.subscribe(AgentLoadFinished, on_finished)
        try:
            yield
        finally:
            await bus.unsubscribe(AgentLoadProgress, on_progress)
            await bus.unsubscribe(Warning, on_warning)
            await bus.unsubscribe(AgentLoadFinished, on_finished)

    def restored(self, session_id: str) -> None:
        self._ready = True
        parts = [f"Restored session {_short_id(session_id)}"]
        if self._restored_name:
            parts.append(_scalar(self._restored_name, 80))
        parts.extend(self._context_parts())
        self._writer.line(("• ", _ACCENT), " · ".join(parts))

    def warnings(self, warnings: Iterable[Warning]) -> None:
        for warning in self._writer.unreported(warnings):
            self._writer.warning(warning_text(warning, self._render))

    def handle(self, event: Event) -> None:
        match event:
            case InvocationEvent():
                self._activity.handle(event)
            case AgentLoadProgress():
                self._load_progress(event)
            case AgentLoadFinished():
                if not self._ready:
                    self._ready = True
                    parts = [f"{_scalar(event.display_name or event.agent_profile, 80) or 'Agent'} ready"]
                    parts.extend(self._context_parts(event.session_id or ""))
                    self._writer.line(("• ", _ACCENT), " · ".join(parts))
            case Warning():
                self.warnings([event])
            case CompactionStarted():
                self._writer.line(("• ", _ACCENT), "Compacting conversation…")
            case CompactionFinished():
                # A generated note is not a committed compaction yet: phase-4 ToolCompacted reports that.
                if event.outcome == "canceled":
                    self._writer.line(("• ", _ACCENT), "Compaction interrupted")
                elif event.outcome == "failed":
                    cause = _scalar(event.failure_reason, 120)
                    self._writer.warning(f"compaction failed ({cause})" if cause else "compaction failed")
            case ToolCompacted(phase="phase4"):
                tokens = ""
                if event.tokens_before > 0:
                    tokens = f" · {format_tokens(event.tokens_before)} → {format_tokens(event.tokens_after)} tokens"
                self._writer.line(("• ", _ACCENT), f"Conversation compacted{tokens}")
            case TodoListUpdated():
                self._writer.line(_todo_line(event.items))
            case _:
                return

    def _load_progress(self, event: AgentLoadProgress) -> None:
        # Only a named server's own start: the collective event has no name, and a failure is reported by the
        # adapter's mcp.connect_failed warning.
        if event.phase != AGENT_LOAD_PHASE_MCP or event.status != AGENT_LOAD_STATUS_RUNNING:
            return
        server = _scalar(event.subject or event.server_name, 80)
        if server:
            self._writer.line(("• ", _ACCENT), f"Connecting MCP server {server}…")

    def _context_parts(self, session_id: str = "") -> list[str]:
        context = self._context()
        parts: list[str] = []
        if context.model:
            parts.append(_scalar(context.model, 80))
        if session_id:
            parts.append(f"session {_short_id(session_id)}")
        if context.workdir:
            parts.append(display_path(context.workdir))
        return parts

    def failed(self) -> None:
        """The turn failed: the answer text it streamed shows before the error."""
        self._activity.flush_pending()

    def succeeded(self, *, duration: float, session_id: str) -> None:
        count = self._activity.top_level_calls
        calls = f"{count} tool call" if count == 1 else f"{count} tool calls"
        parts = ["Done", format_duration(duration), calls]
        if session_id:
            parts.append(f"session {_short_id(session_id)}")
        self._writer.line()
        self._writer.line(("✓ ", _OK), " · ".join(parts))


class WorkflowProgress:
    """Progress for ``icode workflow run``: the run header, node states, agent-node activity and a summary."""

    def __init__(self, writer: ProgressWriter, *, render: Render) -> None:
        self._writer = writer
        self._render = render
        self._nodes: dict[str, str] = {}
        self._running: dict[tuple[str, int], datetime] = {}
        # Each running agent attempt's invocation: a failed one's held answer text shows before its state.
        self._invocations: dict[tuple[str, int], str] = {}
        self._session_id = ""
        self._activity = InvocationActivity(writer, prefix=self._prefix, render=render)

    def _prefix(self, origin: InvocationOrigin) -> str | None:
        node_id = self._nodes.get(origin.root.invocation_id) if origin.root.kind == "workflow_node" else None
        return None if node_id is None else f"  [{node_id}] "

    def starting(self, workflow_id: str, *, checking: bool = False) -> None:
        verb = "Checking" if checking else "Starting"
        self._writer.line(("• ", _ACCENT), f"{verb} workflow {_scalar(workflow_id, 80)}…")

    def warnings(self, warnings: Iterable[Warning]) -> None:
        for warning in self._writer.unreported(warnings):
            self._writer.warning(warning_text(warning, self._render))

    def reported(self, code: str, message: str) -> None:
        """Record a warning the command printed itself, so the run's stream does not repeat it."""
        self._writer.mark_reported(code, message)

    def handle(self, event: Event) -> None:
        match event:
            case InvocationEvent():
                self._activity.handle(event)
            case WorkflowRunAccepted():
                self._session_id = event.session_id or ""
            case WorkflowRunStarted():
                self._header(event)
            case WorkflowNodeStateChanged():
                self._node_state(event)
            case WorkflowNodeOutput(kind=kind) if kind == WORKFLOW_OUTPUT_EMIT:
                self._writer.line(f"  [{_scalar(event.node_id, 80)}] {_unclamped(event.summary_text)}")
            case WorkflowLoopIteration():
                self._writer.line(
                    ("↻ ", _NOTE),
                    f"[{_scalar(event.loop_id, 80)}] iteration {event.iteration}: {_scalar(event.verdict, 40)}",
                )
            case WorkflowRunNotice(code=code) if code not in QUIET_NOTICES:
                self._writer.line(("Notice: ", _NOTE), _unclamped(event.message))
            case Warning():
                self.warnings([event])
            case _:
                return

    def _flush_attempt(self, attempt_key: tuple[str, int]) -> None:
        if invocation_id := self._invocations.pop(attempt_key, ""):
            self._activity.flush_pending(invocation_id)

    def _header(self, event: WorkflowRunStarted) -> None:
        workflow_id = _scalar(event.workflow_id, 80)
        title = _scalar(event.title, 120)
        name = f"{title} ({workflow_id})" if title and title != workflow_id else workflow_id
        parts = [f"Workflow {name}", f"run {_short_id(event.run_id)}"]
        if self._session_id:
            parts.append(f"session {_short_id(self._session_id)}")
        self._writer.line((" · ".join(parts), "bold"))

    def _node_state(self, event: WorkflowNodeStateChanged) -> None:
        node = f"[{_scalar(event.node_id, 80)}]"
        error_lines = _error_lines(event.error)
        error = error_lines[0].strip() if error_lines else ""
        attempt_key = (event.activation_id, event.attempt)
        match event.state:
            case "running":
                if event.invocation_id:
                    self._nodes[event.invocation_id] = _scalar(event.node_id, 80)
                    self._invocations[attempt_key] = event.invocation_id
                self._running[attempt_key] = event.timestamp
                attempt = f" (attempt {event.attempt})" if event.attempt > 1 else ""
                self._writer.line(("▸ ", _ACCENT), f"{node} running{attempt}")
            case "completed":
                self._invocations.pop(attempt_key, None)
                started = self._running.pop(attempt_key, None)
                duration: list[Part] = []
                if started is not None:
                    duration = [(" · ", _DIM), (format_duration((event.timestamp - started).total_seconds()), _DIM)]
                self._writer.line(("✓ ", _OK), f"{node} completed", *duration)
            case "failed":
                self._running.pop(attempt_key, None)
                self._flush_attempt(attempt_key)
                self._writer.line(("✗ ", _FAIL), f"{node} failed", f": {error}" if error else "")
                self._error_detail(error_lines)
            case "retrying" | "awaiting_retry":
                self._running.pop(attempt_key, None)
                self._flush_attempt(attempt_key)
                state = event.state.replace("_", " ")
                self._writer.line(("↻ ", _NOTE), f"{node} {state}", f": {error}" if error else "")
                self._error_detail(error_lines)
            case "skipped" | "cancelled":
                self._running.pop(attempt_key, None)
                self._flush_attempt(attempt_key)
                self._writer.line(("· ", _DIM), f"{node} {event.state}")
            case _:
                return

    def _error_detail(self, error_lines: list[str]) -> None:
        for line in error_lines[1:]:
            self._writer.line("    ", line.rstrip())

    def captured(self, diagnostics: Mapping[str, Any]) -> None:
        """The run's captured load output and output outside nodes, and a failure to read it; it precedes the summary."""
        for key, label in (("load", "Load output"), ("native", "Output outside nodes")):
            captured = diagnostics.get(key, {})
            if captured.get("text"):
                self._writer.always(f"{label}:")
                for line in sanitize_terminal_block(surrogate_safe_text(captured["text"])).splitlines():
                    self._writer.always(line)
            if captured.get("truncated") or captured.get("dropped_bytes"):
                self._writer.always(f"{label}: some output was omitted by the capture limit.")
        if diagnostics.get("error"):
            self._writer.always(_unclamped(diagnostics["error"]))

    def failed(self) -> None:
        """The run ended early: the answer text its nodes streamed shows before the error."""
        self._activity.flush_pending()

    def succeeded(self, *, duration: float) -> None:
        self._writer.line(("✓ ", _OK), "Workflow completed", (" · ", _DIM), format_duration(duration))
