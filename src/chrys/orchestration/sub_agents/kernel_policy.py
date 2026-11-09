# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Kernel child recipe, retry history, result and audit policies for the common shell."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeGuard, cast

from chrys.foundation.errors import clean_error_message, failure_facts
from chrys.foundation.errors.display import display_fields
from chrys.foundation.events.types import (
    InvocationPaused,
    InvocationRetryAttempt,
    InvocationToolCallResult,
)
from chrys.foundation.hosted_tools import HostedToolStatus
from chrys.foundation.models.failures import FailureFacts
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.foundation.retry import (
    TRANSIENT_RETRY_BACKOFF_SECONDS,
    StreamStall,
    StreamStallExhausted,
)
from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.foundation.trajectory.event_types import RetryMode, RetryReason
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id
from chrys.kernel import (
    TOOL_RESULT_CONTENT_TYPES,
    AgentResponse,
    LoopRecorder,
    Message,
)
from chrys.orchestration.invoker.attempts import (
    AgentRunKwargs,
    AttemptRecipe,
    AttemptRunner,
    AttemptTaskHandle,
    BlockingCallTiming,
    HistoryRollback,
    KeepAndRaise,
    ModelRunTrace,
    RetryBoundaryPolicy,
    has_live_continuation_token,
)
from chrys.orchestration.invoker.child_compaction import CompactionRollback
from chrys.orchestration.invoker.child_history import ChildHistory, service_storage_side
from chrys.orchestration.invoker.content_projection import ContentProjector
from chrys.orchestration.invoker.contracts import (
    AbortCause,
    ContinuationTicket,
    Failed,
    FailureDisposition,
    InvocationOutcome,
    Ok,
    RunIntent,
    RunRequest,
    SubAgentFailureReason,
    SubAgentStatus,
)
from chrys.orchestration.invoker.kernel import KernelConversation
from chrys.orchestration.invoker.origin import BoundEmitter
from chrys.orchestration.invoker.resources import Conversation, PassResources
from chrys.service.agent_middleware.events.hosted_tools import FinalSegment, adapt_hosted_tool, hosted_replay_status
from chrys.service.context.compaction.last_words import LastWordsGenerationError
from chrys.service.session.sub_agent_logs import preview_text
from chrys.service.tools.result_metadata import record_tool_success, tool_error
from chrys.service.trajectory.retries import RetryBackoffTrace

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from chrys.foundation.i18n import MessageRef
    from chrys.foundation.models.invocations import ContentStatus
    from chrys.kernel import Agent, AgentSession, ChatResponse
    from chrys.service.agent_middleware.control.sleep import SleepMiddleware
    from chrys.service.agent_middleware.events.sub_agent_events import SubAgentEventMiddleware
    from chrys.service.session.sub_agent_logs import SubAgentLogStats, SubAgentSessionLogWriter

from .shell import SubAgentToolShell

logger = logging.getLogger(__name__)


class HostedBaselineHook(Protocol):
    """Starts a pass's hosted-work baseline, as ``ResponseValidationMiddleware.begin_pass_hosted_baseline`` does."""

    def __call__(self, *, resumes_background_response: bool) -> None: ...


def _is_string_keyed_dict(value: object) -> TypeGuard[dict[str, Any]]:
    """Narrow controller-owned kwargs dictionaries with string keys."""
    return isinstance(value, dict)


# Retry/backoff knobs mirror TurnBindings's streaming retry budget so the UX
# feels the same between main-agent and sub-agent failures. The sub-agent
# retry loop uses the same schedule and cap.
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_STREAM_ATTEMPT_TIMEOUT = 300.0


def _is_last_words_error(exc: BaseException) -> bool:
    """Detect :class:`LastWordsGenerationError` directly or on the cause chain."""
    return any(isinstance(e, LastWordsGenerationError) for e in (exc, exc.__cause__) if e is not None)


def _service_retry_reason(exc: BaseException) -> str:
    """Closed trajectory reason for a sub-agent whole-run retry."""
    if isinstance(exc, StreamStall | StreamStallExhausted):
        return RetryReason.STREAM_STALL
    return RetryReason.TRANSIENT_ERROR


class _CascadeInterruptProbe:
    """Read the controller latch without transferring cancellation ownership."""

    def __init__(self, controller: KernelSubAgentPolicy) -> None:
        self._controller = controller

    @property
    def is_interrupted(self) -> bool:
        return self._controller._shell.cascade_requested


class KernelSubAgentPolicy:
    """Backend-specific policy; decision future, evidence and close binding live in the shell."""

    def __init__(
        self,
        *,
        shell: SubAgentToolShell,
        conversation: Conversation,
        agent: Agent,
        session: AgentSession,
        loop_recorder: LoopRecorder,
        prompt: str,
        run_kwargs: AgentRunKwargs,
        parent_call_id: str = "",
        parent_event_call_id: str = "",
        sub_agent_log_file: str = "",
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_schedule: tuple[int, ...] = TRANSIENT_RETRY_BACKOFF_SECONDS,
        persist_dir: Path | None = None,
        log_writer: SubAgentSessionLogWriter | None = None,
        log_stats: SubAgentLogStats | None = None,
        terminal_audit_callback: Callable[[], None] | None = None,
        pending_record_finalizer: Callable[[Path | None], None] | None = None,
        tool_event_middleware: SubAgentEventMiddleware | None = None,
        stream: bool = False,
        stream_attempt_timeout: float | None = None,
        shows_answer_text: bool = True,
        sleep_middleware: SleepMiddleware | None = None,
        pass_start_hooks: Sequence[Callable[[], None]] = (),
        hosted_commits_probe: Callable[[], tuple[str, ...]] | None = None,
        begin_hosted_baseline: HostedBaselineHook | None = None,
        trajectory_context: TrajectoryContext | None = None,
        trajectory_boundary_operation_id: str | None = None,
    ) -> None:
        self._shell = shell
        invocation_id = shell.invocation_id
        tool_name = shell.tool_name
        agent_name = shell.agent_name
        event_bus = shell.bus
        session_id = shell.origin.session_id or None
        self._invocation_id = invocation_id
        self.origin = shell.origin
        self._emitter = BoundEmitter(event_bus, self.origin)
        self._tool_name = tool_name
        self._agent_name = agent_name
        self._agent = agent
        self._session = session
        self._loop_recorder = loop_recorder
        self._prompt = prompt
        self._run_kwargs: AgentRunKwargs = {**run_kwargs, "session": session}
        self._history = ChildHistory(session, loop_recorder)
        self._service_storage = service_storage_side(agent.client, run_kwargs.get("options"))
        self._compaction = CompactionRollback(run_kwargs.get("compaction_strategy"))
        self._bus = event_bus
        self._session_id = session_id
        # The ``Content.call_id`` of the parent assistant function_call
        # that invoked this sub-agent.  Persisted so reload-recovery can
        # pair a paused record back to its dangling function_call by id
        # rather than by name+appearance-order, which mis-matches when
        # the same sub-agent tool is invoked concurrently.
        self._parent_call_id = parent_call_id
        self._parent_event_call_id = parent_event_call_id
        self._sub_agent_log_file = sub_agent_log_file
        self._terminal_audit_persisted = False
        self._max_retries = max_retries
        self._backoff = backoff_schedule
        self._persist_dir = persist_dir
        self._log_writer = log_writer
        self._log_stats = log_stats
        self._terminal_audit_callback = terminal_audit_callback
        self._pending_record_finalizer = pending_record_finalizer
        self._tool_event_middleware = tool_event_middleware
        self._stream = stream
        self._stream_attempt_timeout = (
            stream_attempt_timeout if stream_attempt_timeout is not None else _DEFAULT_STREAM_ATTEMPT_TIMEOUT
        )
        # Whether a frontend shows this child's answer text; text nobody sees never stops a retry.
        self._shows_answer_text = shows_answer_text
        self._sleep_middleware = sleep_middleware
        # The hosted baseline reads this policy's own request options, not the caller's run_kwargs: a
        # whole-run retry replaces them, so only these say whether the pass polls a background response.
        self._begin_hosted_baseline_hook = begin_hosted_baseline
        # Fired at the start of every pass (initial run, or a user Retry
        # decision after a pause).  Components carrying state across a pass's
        # whole-run retry attempts register here so an aborted pass cannot
        # leak state into the next one; mirrors the main executor's
        # run_cycle_start_hooks.
        self._pass_start_hooks = (*pass_start_hooks, self._begin_hosted_baseline)
        # Validation-middleware probe for provider-hosted tool executions the
        # loop recorder cannot see; consulted by the whole-run retry gate.
        self._hosted_commits_probe = hosted_commits_probe
        self._trajectory_context = trajectory_context
        self._trajectory_boundary_operation_id = trajectory_boundary_operation_id
        self._service_retry_trace: RetryBackoffTrace | None = None

        self._last_error: str = ""
        self._last_error_display: MessageRef | None = None
        self._last_error_hint: MessageRef | None = None
        self._last_error_facts: FailureFacts | None = None
        self._failure_reason: SubAgentFailureReason | None = None
        self._retry_attempts_total: int = 0
        # The answer of the pass that completed the invocation, if any.
        self.final_segment: FinalSegment | None = None
        # The pass's only publisher of model text, and how its open segment ends.
        self._projector: ContentProjector | None = None
        self._pass_status: ContentStatus = "completed"
        # The seed prompt is one persisted context item: it keeps a single
        # analytics identity across prompt replays so the trajectory can
        # account for every model request that re-sent it.
        self._seed_item_id = new_analytics_id()
        self._active_run_input: list[Any] = self._seed_input()
        self._next_run_input: list[Any] = self._seed_input()
        self._pass_start_index = 0
        self._attempt_handle = AttemptTaskHandle()
        attempt_recipe = AttemptRecipe(
            stall_exhaustion=KeepAndRaise(),
            stall_error=lambda timeout: StreamStall(f"no streaming updates received for {timeout:g}s"),
            retry_boundary=RetryBoundaryPolicy.OBSERVE,
            blocking_call_timing=BlockingCallTiming.BEFORE_ATTEMPT_TASK,
            before_attempt=self._check_cascade,
            history_state=self._history.state,
        )
        self._rollback = HistoryRollback(
            session,
            history_state=attempt_recipe.history_state,
            snapshot_caller=self._compaction.snapshot,
            restore_caller=self._compaction.restore,
        )
        self._attempts = AttemptRunner(
            agent=agent,
            session=session,
            handle=self._attempt_handle,
            rollback=self._rollback,
            retry_participant=None,
            interrupt=_CascadeInterruptProbe(self),
            # Children have no model.run layer, so context stays None; the notice callback orders RetryBackoffTrace.
            trace=ModelRunTrace(
                interrupted=lambda: self._shell.cascade_requested,
                service_side=lambda: self._service_storage,
                committed=lambda: self._loop_recorder.committed_count > 0,
            ),
            stream_observer=lambda: self._projector,
            content_exposed=lambda: self._shows_answer_text and self._projector is not None and self._projector.exposed,
            publish_retry=self._publish_service_retry_attempt,
            interruptible_sleep=self._sleep_for_service_retry,
            max_retries=lambda: self.max_retries,
            backoff_schedule=lambda: self.backoff_schedule,
            stream_timeout=lambda: self._stream_attempt_timeout,
            committed_count=lambda: self._loop_recorder.committed_count,
            hosted_commits=lambda: self._hosted_commits_probe() if self._hosted_commits_probe is not None else (),
            recipe=attempt_recipe,
        )

        self.backend = KernelConversation(
            owner=conversation,
            session=session,
            attempts=self._attempts,
            attempt_handle=self._attempt_handle,
            observer=self,
            run_kwargs=lambda: self._run_kwargs,
            stream=lambda: self._stream,
            service_side=lambda: self._service_storage,
            recorder=loop_recorder,
            hosted_observed=hosted_commits_probe,
            start_hooks=self._pass_start_hooks,
            failure_disposition=FailureDisposition.CALLER_DECISION,
        )

    async def begin(self, request: RunRequest, resources: PassResources) -> None:
        self._pass_start_index = len(self._history.messages())
        # Sub-agent views show each answer segment whole once it completes.
        self._projector = ContentProjector(self._emitter, agent_name=self._agent_name, shows_whole_segments=True)
        if self._tool_event_middleware is not None:
            self._tool_event_middleware.bind_text_segments(self._projector.end_segment)
        self._pass_status = "completed"
        resources.begin()
        self._shell.set_status(SubAgentStatus.RUNNING)
        await self._write_log(status="running")
        self._active_run_input = list(request.messages)

    async def succeeded(self, response: AgentResponse[Any]) -> None:
        if self._tool_event_middleware is not None:
            await self._tool_event_middleware.reconcile_hosted_response(response.messages)

    async def failed(self, error: Exception) -> None:
        self._pass_status = "failed"
        self._record_failure(error)

    async def finished(self) -> None:
        # The pass's open segment ends with it, keeping the text it showed.
        if self._projector is not None:
            await self._projector.finish(self._pass_status)

    def cancelled(self) -> None:
        self._pass_status = "interrupted"

    async def response_ready(self, response: ChatResponse, final: bool) -> None:
        """The kernel landed a response: the pass projector shows what it did not stream."""
        del final
        if self._projector is not None:
            shown = self._tool_event_middleware.hosted_text_shown if self._tool_event_middleware is not None else None
            await self._projector.response_ready(response, shown=shown)

    def abort_cause(self) -> AbortCause | None:
        return self._shell.owner_close_cause or (AbortCause.CASCADE if self._shell.cascade_requested else None)

    async def interrupt(self) -> None:
        await self._shell.cascade_abort()

    def _record_failure(self, exc: Exception) -> None:
        # The child's own window: its profile may differ from the parent's.
        strategy = self._compaction.strategy
        self._last_error_display, self._last_error_hint = display_fields(
            exc, max_context_tokens=strategy.max_context_tokens if strategy is not None else None
        )
        self._last_error_facts = failure_facts(exc)
        if isinstance(exc, StreamStallExhausted):
            # Keep the original stall message (chained via __cause__)
            # so the pause banner shows the underlying reason instead
            # of a generic "retries exhausted" placeholder. Falls back
            # to a static label when the chain is empty (e.g. stalls
            # without a stored cause — defensive only).
            cause_msg = clean_error_message(exc) if exc.__cause__ else ""
            self._last_error = (
                f"Stream stalled after {self._max_retries} retries: {cause_msg}"
                if cause_msg
                else f"Stream stalled after {self._max_retries} retries"
            )
            self._failure_reason = SubAgentFailureReason.STREAM_STALL
            logger.warning(
                "Sub-agent '%s' (inv=%s) stalled after %d retries — pausing",
                self._tool_name,
                self._invocation_id,
                self._max_retries,
            )

        elif isinstance(exc, StreamStall):
            self._last_error = clean_error_message(exc) or "Stream stalled"
            self._failure_reason = SubAgentFailureReason.STREAM_STALL
            logger.warning(
                "Sub-agent '%s' (inv=%s) stalled — pausing: %s",
                self._tool_name,
                self._invocation_id,
                self._last_error,
            )

        else:
            e = exc
            if _is_last_words_error(e):
                self._failure_reason = SubAgentFailureReason.LAST_WORDS
            else:
                self._failure_reason = SubAgentFailureReason.FRAMEWORK_EXC
            self._last_error = clean_error_message(e)
            logger.warning(
                "Sub-agent '%s' (inv=%s) failed with %s — pausing: %s",
                self._tool_name,
                self._invocation_id,
                self._failure_reason.value,
                self._last_error,
            )

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_error_display(self) -> MessageRef | None:
        return self._last_error_display

    @property
    def last_error_hint(self) -> MessageRef | None:
        return self._last_error_hint

    @property
    def last_error_facts(self) -> FailureFacts | None:
        return self._last_error_facts

    @property
    def failure_reason(self) -> SubAgentFailureReason | None:
        return self._failure_reason

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def backoff_schedule(self) -> tuple[int, ...]:
        return self._backoff

    async def _interrupt_active_sleep(self, call_ids: set[str]) -> None:
        """Let an active inner sleep publish InvocationToolCallResult before cancellation."""
        if not call_ids or self._sleep_middleware is None or self._bus is None:
            return
        pending = set(call_ids)
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[None] = loop.create_future()

        async def _on_tool_result(event: InvocationToolCallResult) -> None:
            if event.origin.invocation_id != self._invocation_id:
                return
            pending.discard(event.call_id)
            if not pending and not completed.done():
                completed.set_result(None)

        await self._bus.subscribe(InvocationToolCallResult, _on_tool_result)
        try:
            interrupted = set(self._sleep_middleware.interrupt_active())
            pending.intersection_update(interrupted)
            if not pending:
                return
            # Keep global interrupt responsive: give the inner sleep a
            # small writeback window, then let task cancellation win.
            await asyncio.wait_for(completed, timeout=0.5)
        except TimeoutError:
            return
        finally:
            await self._bus.unsubscribe(InvocationToolCallResult, _on_tool_result)

    def _check_cascade(self) -> None:
        """Keep the caller's cancellation check before every agent attempt."""
        if self._shell.cascade_requested:
            raise asyncio.CancelledError

    async def _reject_hosted_attempt(self, message: str) -> None:
        if self._tool_event_middleware is not None:
            await self._tool_event_middleware.reject_hosted_attempt(message)

    async def _publish_retry_attempt(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
        *,
        scope: Literal["wire", "run"] = "run",
    ) -> None:
        self._retry_attempts_total += 1
        if self._tool_event_middleware is not None and not self._has_live_continuation_token():
            await self._tool_event_middleware.reject_hosted_attempt(message)
        if self._bus is None:
            return
        display_message, display_hint = display_fields(exc, retry_notice=True)
        await self._emitter.publish(
            InvocationRetryAttempt(
                origin=self.origin,
                agent_name=self._agent_name,
                message=message,
                attempt=attempt,
                scope=scope,
                max_attempts=max_attempts,
                delay_seconds=delay_seconds,
                session_id=self._session_id,
                display_message=display_message,
                display_hint=display_hint,
                facts=failure_facts(exc),
            )
        )

    async def _publish_service_retry_attempt(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
    ) -> None:
        await self._publish_retry_attempt(message, attempt, max_attempts, delay_seconds, exc)
        trace = RetryBackoffTrace.open(
            context=self._trajectory_context,
            parent_operation_id=self._trajectory_boundary_operation_id,
            retry_mode=RetryMode.RUN,
        )
        self._service_retry_trace = trace
        if trace is not None:
            await trace.scheduled(reason_code=_service_retry_reason(exc), delay_seconds=delay_seconds)

    async def _sleep_for_service_retry(self, seconds: int) -> bool:
        trace, self._service_retry_trace = self._service_retry_trace, None
        try:
            interrupted = await self._interruptible_sleep(seconds)
        except asyncio.CancelledError:
            raise
        if trace is not None and not interrupted:
            await trace.started()
        return interrupted

    async def publish_wire_retry_attempt(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
    ) -> None:
        """Publish a kernel wire-retry notice through the controller bus."""
        await self._publish_retry_attempt(message, attempt, max_attempts, delay_seconds, exc, scope="wire")

    async def sleep_for_wire_retry(self, seconds: int) -> bool:
        """Use the controller's cascade-aware backoff for kernel retries."""
        return await self._interruptible_sleep(seconds)

    def wire_answer_shown(self) -> bool:
        """Whether the wire call being streamed showed answer text: an answer segment completed where it shows."""
        return self._shows_answer_text and self._projector is not None and self._projector.response_exposed

    def observe_continuation_token(self, token: Any) -> None:
        """Mirror the kernel's live continuation token into retry-owned options.

        A transient failure while polling a stored background response must
        resume that response on the next attempt, not re-issue the original
        create request. Reads ``_run_kwargs`` at call time so a
        handle-stripping restore that replaces the options dict cannot
        orphan the observer.
        """
        raw_options = self._run_kwargs.get("options")
        options = raw_options if _is_string_keyed_dict(raw_options) else None
        if token is None:
            if options is not None:
                options.pop("continuation_token", None)
            return
        if options is None:
            options = {}
            self._run_kwargs["options"] = options
        options["continuation_token"] = token

    def _has_live_continuation_token(self) -> bool:
        return has_live_continuation_token(self._run_kwargs)

    def _begin_hosted_baseline(self) -> None:
        if self._begin_hosted_baseline_hook is not None:
            self._begin_hosted_baseline_hook(resumes_background_response=self._has_live_continuation_token())

    async def _write_log(
        self,
        *,
        status: str,
        result: str = "",
        failure_reason: str = "",
        last_error: str = "",
        ended: bool = False,
    ) -> bool:
        """Best-effort audit-log update for this invocation.

        Terminal records (``ended=True``) are the only durable copy of the
        inner history once the controller is removed, so a failed write gets
        one retry and a loud warning instead of the silent debug log.  A
        terminal write is also attempted on a writer whose initial write
        failed (``active`` still False) — success there re-creates the log
        file rather than silently discarding the committed inner history.
        """
        if self._log_writer is None or self._log_writer.path is None:
            return False
        if not ended and not self._log_writer.active:
            return False
        for _attempt in range(2):
            try:
                if await self._log_writer.write(
                    status=status,
                    state=self._history.state(),
                    result=result,
                    failure_reason=failure_reason,
                    last_error=last_error,
                    ended=ended,
                ):
                    if ended:
                        self._terminal_audit_persisted = True
                        if self._terminal_audit_callback is not None:
                            self._terminal_audit_callback()
                    return True
            except Exception:
                logger.debug("sub-agent audit log update failed for invocation %s", self._invocation_id, exc_info=True)
            if not ended:
                return False
        logger.warning(
            "sub-agent audit log terminal write failed for invocation %s; inner history may be incomplete on reload",
            self._invocation_id,
        )
        return False

    @property
    def terminal_audit_persisted(self) -> bool:
        """Whether a complete terminal audit record was written successfully."""
        return self._terminal_audit_persisted

    def _repair_paused_history(self) -> None:
        # Taken once: a pause and a later cancel both repair the same pass.
        partial_answer = self._projector.take_unlanded_answer() if self._projector is not None else ()
        self._history.repair_after_failure(self._active_run_input, self._pass_start_index, partial_answer)

    def _seed_input(self) -> list[Any]:
        """Build the run input that starts (or restarts) the sub-agent from its prompt.

        Handing Agent.run a ready ``Message`` instead of the bare string
        lets the analytics item id ride along: Agent.run would
        otherwise mint an anonymous user message that no context revision
        can identify.
        """
        message = Message("user", [self._prompt])
        ensure_analytics_item_id(message.additional_properties, item_id=self._seed_item_id)
        return [message]

    async def _interruptible_sleep(self, seconds: int) -> bool:
        """Sleep in 1-second ticks, returning True if cascade fired."""
        for _ in range(max(0, seconds)):
            if self._shell.cascade_requested:
                return True
            await asyncio.sleep(1)
        return self._shell.cascade_requested

    def _persist_path(self) -> Path | None:
        if self._persist_dir is None:
            return None
        return self._persist_dir / f"{self._invocation_id}.json"

    def _serialize_state(self) -> dict[str, Any]:
        """Snapshot the paused state as a plain JSON-safe dict.

        We deliberately persist only pause metadata — not the ``Agent``,
        middleware, run_kwargs, or the prompt-level token counts.  On
        restore we reconstruct the controller as read-only-abort (see
        :meth:`restore_from_data`); there's no safe way to resume the
        original tool call after the parent process died.
        """
        now = datetime.now(UTC).isoformat()
        return {
            "schema_version": 1,
            "record_type": "sub_agent_pending",
            "invocation_id": self._invocation_id,
            "tool_name": self._tool_name,
            "agent_name": self._agent_name,
            "prompt_preview": preview_text(self._prompt),
            "session_id": self._session_id or "",
            # ``parent_call_id`` lets the reload-recovery injector pair
            # this record back to its dangling assistant function_call
            # by framework call_id rather than by tool_name + appearance
            # order.  The injector treats the field as optional so
            # records from future code paths that don't supply it still
            # flow through the name+order fallback.
            "parent_call_id": self._parent_call_id,
            "parent_provider_call_id": self._parent_call_id,
            "parent_event_call_id": self._parent_event_call_id,
            **({"sub_agent_log_file": self._sub_agent_log_file} if self._sub_agent_log_file else {}),
            "created_at": now,
            "paused_at": now,
            "failure_reason": (self._failure_reason.value if self._failure_reason else ""),
            "last_error": self._last_error,
            "retry_attempts_total": self._retry_attempts_total,
        }

    def _write_persisted(self) -> None:
        """Write the paused-state snapshot to disk.  Best-effort — IO errors are logged."""
        path = self._persist_path()
        if path is None:
            return
        try:
            atomic_write_owner_only_text(
                path,
                json.dumps(self._serialize_state(), indent=2, allow_nan=False),
            )
        except OSError as e:
            logger.warning("Failed to persist paused sub-agent %s: %s", self._invocation_id, e)

    def _queue_persisted_cleanup_if_terminal(self) -> None:
        """Queue the pause record for deletion once parent session save succeeds."""
        if self._shell.status == SubAgentStatus.PAUSED:
            return
        path = self._persist_path()
        if path is None or self._pending_record_finalizer is None:
            return
        self._pending_record_finalizer(path)

    @staticmethod
    def _final_segment(response: AgentResponse[Any]) -> FinalSegment:
        """Return the parent result and the transcript's final text for *response*.

        ``FinalSegment`` decides both, as it does for a workflow agent node.
        A hosted image/artifact-only response is still a successful
        structured result, so synthesize a neutral parent result, shown in the
        transcript too, after its rich payload has been published by
        reconciliation.
        """
        segment = FinalSegment.of(response)
        if segment.result:
            return segment

        has_images = False
        has_artifacts = False
        for message in response.messages:
            for content in message.contents:
                if content.type not in TOOL_RESULT_CONTENT_TYPES or not content.provider_hosted:
                    continue
                view = adapt_hosted_tool(None, content)
                if hosted_replay_status(view, has_result=True) != HostedToolStatus.COMPLETED:
                    continue
                has_images = has_images or bool(view.image_contents)
                has_artifacts = has_artifacts or bool(view.artifacts)
        if has_images and has_artifacts:
            note = "Sub-agent returned image and artifact output."
        elif has_images:
            note = "Sub-agent returned image output."
        elif has_artifacts:
            note = "Sub-agent returned artifact output."
        else:
            return segment
        return FinalSegment(note, note)

    def request(self, ticket: ContinuationTicket | None) -> RunRequest:
        return RunRequest(
            self._next_run_input, RunIntent.RETRY if ticket is not None else RunIntent.FRESH, self.origin, ticket
        )

    async def project_result(self, outcome: InvocationOutcome) -> str | None:
        try:
            if isinstance(outcome, Failed):
                response = None
            else:
                if not isinstance(outcome, Ok):
                    raise TypeError("A completed sub-agent pass must have an Ok or Failed outcome.")
                response = cast(AgentResponse[Any], outcome.backend_payload)
            if response is not None:
                self._shell.set_status(SubAgentStatus.COMPLETED)
                segment = self._final_segment(response)
                text = segment.result
                if not text:
                    error_text = tool_error(
                        "sub_agent_empty_output",
                        f"sub-agent '{self._tool_name}' returned no output",
                        details={"tool_name": self._tool_name, "invocation_id": self._invocation_id},
                    )
                    await self._write_log(
                        status="completed",
                        result=error_text,
                        failure_reason="empty_output",
                        last_error=error_text,
                        ended=True,
                    )
                    return error_text
                record_tool_success()
                await self._write_log(status="completed", result=text, ended=True)
                self.final_segment = segment
                return text

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_failure(exc)

        return None

    async def prepare_pause(self) -> None:
        await self._reject_hosted_attempt(self._last_error or "Sub-agent execution paused")
        self._repair_paused_history()

    async def record_pause(self) -> None:
        self._write_persisted()
        await self._write_log(
            status="paused",
            failure_reason=self._failure_reason.value if self._failure_reason else "",
            last_error=self._last_error,
        )

    def pause_event(self) -> InvocationPaused:
        return InvocationPaused(
            origin=self.origin,
            agent_name=self._agent_name,
            tool_name=self._tool_name,
            reason=self._failure_reason.value if self._failure_reason else "",
            last_error=self._last_error,
            last_error_display=self._last_error_display,
            last_error_hint=self._last_error_hint,
            last_error_facts=self._last_error_facts,
            retry_attempts=self._retry_attempts_total,
            session_id=self._session_id,
        )

    def resolve_late_cascade(self, decision: asyncio.Future[str]) -> None:
        # Preserve the kernel callback window: the operation owner wakes the
        # late future by cancelling its caller after audit convergence.
        pass

    def check_terminal_race(self) -> None:
        # Kernel keeps its existing writer/return arbitration; ACP rechecks.
        pass

    def prepare_retry(self) -> tuple[str, ...]:
        self._next_run_input, withdrawn = self._history.retry_input(self._seed_input)
        return withdrawn

    async def abort_result(self, *, by_user: bool) -> str:
        reason = self._failure_reason.value if self._failure_reason else ""
        details = {"tool_name": self._tool_name, "invocation_id": self._invocation_id, "failure_reason": reason}
        if by_user:
            status = "aborted"
            text = tool_error(
                "sub_agent_aborted",
                f"sub-agent '{self._tool_name}' aborted by user after failure — {self._last_error}",
                details=details,
            )
        else:
            status = "failed"
            text = tool_error(
                "sub_agent_failed", f"sub-agent '{self._tool_name}' failed — {self._last_error}", details=details
            )
        await self._write_log(
            status=status, result=text, failure_reason=reason, last_error=self._last_error, ended=True
        )
        return text

    def latch_abort(self, cause: AbortCause) -> None:
        # The backend observer reads the shell latch; no second task owner.
        pass

    async def cancel_active(self) -> None:
        task = self._attempt_handle.task
        if task is not None and not task.done():
            sleep_call_ids = self._sleep_middleware.active_call_ids if self._sleep_middleware is not None else ()
            if sleep_call_ids:
                await self._interrupt_active_sleep(set(sleep_call_ids))
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._shell.finalize_cancellation()

    async def finalize_cancellation(self) -> None:
        if self._tool_event_middleware is not None:
            await self._tool_event_middleware.reject_hosted_attempt("Sub-agent execution interrupted")
        self._repair_paused_history()
        if self._shell.cascade_requested:
            await self._shell.publish_cascade_event()
        else:
            self._shell.set_status(SubAgentStatus.ABORTED)
            await self._write_log(status="cancelled", result="cancelled", last_error="cancelled", ended=True)

    async def before_cascade_event(self) -> None:
        await self._write_log(
            status="cascade_aborted",
            result="cancelled by parent interrupt",
            failure_reason="cascade_aborted",
            last_error="cancelled by parent interrupt",
            ended=True,
        )

    async def run_cancelled(self) -> None:
        await self._shell.finalize_cancellation()

    async def finish_run(self) -> None:
        self._queue_persisted_cleanup_if_terminal()
