# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Kernel pass attempts, task ownership and synchronous retry ports.

Local calls keep the kernel wire retry lane; stored calls use the existing
whole-run retry loop. Caller policy, presentation and pass lifecycle arrive
through explicit ports. No caller shell or disk writer is owned here.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, TypedDict, TypeGuard, cast

from chrys.foundation.errors import clean_error_message, is_retryable
from chrys.foundation.retry import (
    HistorySnapshot,
    StreamRetryLoop,
    StreamStall,
    StreamStallExhausted,
    restore_message_properties,
    snapshot_message_properties,
)
from chrys.foundation.trajectory.context import TRAJECTORY_CONTEXT_KWARG, trajectory_scope
from chrys.foundation.trajectory.envelope import Link, MeasurementSource, measurement
from chrys.foundation.trajectory.event_types import EventType as TrajectoryEventType
from chrys.foundation.trajectory.event_types import ModelRunEndReason, RetryMode, RetryReason
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.kernel import is_retry_boundary_update, resolve_storage_mode_and_handles, wire_progress_scope
from chrys.service.context.providers.history import PRE_OUTPUT_HISTORY_LEN_STATE_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Mapping, Sequence

    from chrys.foundation.trajectory.context import TrajectoryContext
    from chrys.kernel import Agent, AgentResponse, AgentResponseUpdate, AgentSession, Message, ResponseStream
    from chrys.kernel.loop import StallExhaustedAction
    from chrys.kernel.middleware import ChatMiddleware, FunctionMiddleware

logger = logging.getLogger(__name__)


class AgentRunKwargs(TypedDict, total=False):
    """Keyword arguments forwarded unchanged to ``Agent.run``."""

    session: AgentSession
    middleware: Sequence[ChatMiddleware | FunctionMiddleware]
    options: Mapping[str, Any]
    compaction_strategy: Any
    tokenizer: Any
    client_kwargs: Mapping[str, Any]


def is_string_keyed_dict(value: object) -> TypeGuard[dict[str, Any]]:
    """Narrow internal kwargs dictionaries whose producers use string keys."""
    return isinstance(value, dict)


def has_live_continuation_token(run_kwargs: Mapping[str, object] | None) -> bool:
    """True when the retry-owned options still reference a live background response."""
    if run_kwargs is None:
        return False
    options = run_kwargs.get("options")
    return is_string_keyed_dict(options) and options.get("continuation_token") is not None


def drop_continuation_token(run_kwargs: AgentRunKwargs) -> None:
    """Forget the retry-owned options' background response, so the next request starts a new one."""
    options = run_kwargs.get("options")
    if is_string_keyed_dict(options):
        options.pop("continuation_token", None)


def continuation_token_observer_for(run_kwargs: AgentRunKwargs) -> Callable[[Any], None]:
    """Mirror the kernel's live continuation token into the retry-owned options.

    A transient failure while polling a stored background response must
    resume that response on the next whole-run attempt — the token has to
    live in the options the retry loop re-issues, not only in the kernel's
    attempt-local copy. Writes go through ``run_kwargs`` at call time so a
    handle-stripping restore that replaces the options dict cannot orphan
    the observer.
    """

    def _observe(token: Any) -> None:
        if token is None:
            drop_continuation_token(run_kwargs)
            return
        raw_options = run_kwargs.get("options")
        options = raw_options if is_string_keyed_dict(raw_options) else None
        if options is None:
            options = {}
            run_kwargs["options"] = options
        options["continuation_token"] = token

    return _observe


def _run_retry_reason(exc: BaseException) -> str:
    """Closed reason code for a whole-run retry decision."""
    if isinstance(exc, StreamStall | StreamStallExhausted):
        return RetryReason.STREAM_STALL
    return RetryReason.TRANSIENT_ERROR


class WireRetryPolicyAdapter:
    """Adapt caller configuration and callbacks to the kernel WireRetryPolicy.

    Budgets and backoff accept values (the TurnBindings's per-run snapshot) or
    readers (the child controller's live configuration). No budget or stall
    action is defaulted across those policies. An absent pre-retry callback
    means no injection transaction; an absent hosted or answer probe remains
    None.
    """

    def __init__(
        self,
        *,
        max_retries: int | Callable[[], int],
        stall_timeout_seconds: float | None,
        stall_max_retries: int | Callable[[], int],
        stall_exhausted_action: StallExhaustedAction,
        backoff_schedule: tuple[int, ...] | Callable[[], tuple[int, ...]],
        interrupted: Callable[[], bool],
        interruptible_sleep: Callable[[int], Awaitable[bool]],
        publish_retry: Callable[[str, int, int, int, BaseException], Awaitable[None]],
        prepare_retry: Callable[[], None] | None = None,
        hosted_commits_in_flight: Callable[[], tuple[str, ...]] | None = None,
        answer_shown: Callable[[], bool] | None = None,
    ) -> None:
        self._max_retries = max_retries
        self.stall_timeout_seconds = stall_timeout_seconds
        self._stall_max_retries = stall_max_retries
        self.stall_exhausted_action = stall_exhausted_action
        self.backoff_schedule = backoff_schedule
        self.interrupted = interrupted
        self.interruptible_sleep = interruptible_sleep
        self.publish_retry = publish_retry
        self.prepare_retry = prepare_retry
        # Describes only the current wire attempt, never the pass total.
        self.hosted_commits_in_flight = hosted_commits_in_flight
        self.answer_shown = answer_shown

    @property
    def max_retries(self) -> int:
        return self._max_retries if isinstance(self._max_retries, int) else self._max_retries()

    @max_retries.setter
    def max_retries(self, value: int) -> None:
        self._max_retries = value

    @property
    def stall_max_retries(self) -> int:
        return self._stall_max_retries if isinstance(self._stall_max_retries, int) else self._stall_max_retries()

    @stall_max_retries.setter
    def stall_max_retries(self, value: int) -> None:
        self._stall_max_retries = value

    def backoff_seconds(self, attempt: int) -> int:
        backoff = self.backoff_schedule if isinstance(self.backoff_schedule, tuple) else self.backoff_schedule()
        if not backoff:
            return 0
        return backoff[min(attempt, len(backoff) - 1)]

    def is_retryable(self, exc: BaseException) -> bool:
        return is_retryable(exc)

    def is_interrupted(self) -> bool:
        return self.interrupted()

    async def sleep(self, seconds: int) -> bool:
        return await self.interruptible_sleep(seconds)

    async def on_retry(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
    ) -> None:
        await self.publish_retry(message, attempt, max_attempts, delay_seconds, exc)

    def before_retry(self) -> None:
        if self.prepare_retry is not None:
            self.prepare_retry()


class RetryParticipant(Protocol):
    """Caller transaction participating in a whole pass and retry rollback."""

    def begin_retry(self) -> None: ...
    def end_retry(self) -> None: ...
    def restore_for_retry(self) -> None: ...


class InterruptProbe(Protocol):
    """Read the caller's synchronous interrupt latch."""

    @property
    def is_interrupted(self) -> bool: ...


class StreamObserver(Protocol):
    """Present each streamed update as it arrives; the pass's content projector."""

    async def on_update(self, update: AgentResponseUpdate) -> None: ...
    async def on_retry_boundary(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RestoreAndFallback:
    """Restore the exhausted stream, reject its presentation, then block once."""

    reject_stream: Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class KeepAndRaise:
    """Keep the exhausted attempt for the shell's pause projection."""


type StallExhaustionPolicy = RestoreAndFallback | KeepAndRaise


class RetryBoundaryPolicy(Enum):
    """Keep the caller's scheduling observation at an empty retry boundary."""

    SKIP = "skip"
    OBSERVE = "observe"


class BlockingCallTiming(Enum):
    """Task context in which the blocking run awaitable is constructed."""

    IN_ATTEMPT_TASK = "in_attempt_task"
    BEFORE_ATTEMPT_TASK = "before_attempt_task"


@dataclass(frozen=True, slots=True)
class AttemptRecipe:
    """Conversation-bound policy choices; no shared controller callbacks.

    history_state is consumed by the shell to construct HistoryRollback;
    AttemptRunner does not read it.
    """

    stall_exhaustion: StallExhaustionPolicy
    stall_error: Callable[[float], StreamStall]
    retry_boundary: RetryBoundaryPolicy
    blocking_call_timing: BlockingCallTiming
    history_state: Callable[[], dict[str, Any]]
    before_attempt: Callable[[], None] | None


@dataclass(frozen=True, slots=True)
class WireRecipe:
    """The main pass snapshot or child live-reader wire-policy recipe."""

    max_retries: int | Callable[[], int]
    stall_timeout_seconds: float
    stall_max_retries: int | Callable[[], int]
    stall_exhausted_action: StallExhaustedAction
    backoff_schedule: tuple[int, ...] | Callable[[], tuple[int, ...]]
    interrupted: Callable[[], bool]
    interruptible_sleep: Callable[[int], Awaitable[bool]]
    publish_retry: Callable[[str, int, int, int, BaseException], Awaitable[None]]
    prepare_retry: Callable[[], None] | None
    hosted_commits_in_flight: Callable[[], tuple[str, ...]] | None
    answer_shown: Callable[[], bool] | None

    def build(self) -> WireRetryPolicyAdapter:
        return WireRetryPolicyAdapter(
            max_retries=self.max_retries,
            stall_timeout_seconds=self.stall_timeout_seconds,
            stall_max_retries=self.stall_max_retries,
            stall_exhausted_action=self.stall_exhausted_action,
            backoff_schedule=self.backoff_schedule,
            interrupted=self.interrupted,
            interruptible_sleep=self.interruptible_sleep,
            publish_retry=self.publish_retry,
            prepare_retry=self.prepare_retry,
            hosted_commits_in_flight=self.hosted_commits_in_flight,
            answer_shown=self.answer_shown,
        )


@dataclass(slots=True)
class AttemptTaskHandle:
    """Sole L0 attempt task reference; Turn flags belong to the Turn shell."""

    task: asyncio.Task[AgentResponse[Any]] | None = None

    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


class HistoryRollback:
    """Synchronous history rollback plus an opaque caller-state participant.

    Snapshot/restore callbacks run inline, without exception interception or
    await gaps. In particular a failed caller restore stops before after_restore,
    retry notification, sleep or another model attempt.
    """

    def __init__(
        self,
        session: AgentSession,
        *,
        snapshot_caller: Callable[[], object],
        restore_caller: Callable[[object], None],
        history_state: Callable[[], dict[str, Any]],
    ) -> None:
        self._session = session
        self._snapshot_caller = snapshot_caller
        self._restore_caller = restore_caller
        self._history_state = history_state

    def snapshot(self) -> HistorySnapshot:
        """Snapshot messages and compressed block count for rollback.

        Used before an ``agent.run()`` that may need retry. Captures
        ``compressed_msgs`` length alongside messages so ``restore``
        can undo cross-turn compressions (``_compress_state``) that run
        mid-loop, and each message's ``additional_properties`` so in-place
        annotations from the rolled-back attempt (exclusion flags,
        summarized-by markers) can be reverted exactly.
        """
        history_state = self._history_state()
        messages = list(history_state.get("messages", []))
        return HistorySnapshot(
            messages=messages,
            compressed_count=len(history_state.get("compressed_msgs", [])),
            service_session_id=self._session.service_session_id or "",
            message_properties=snapshot_message_properties(messages),
            pre_output_history_len=history_state.get(PRE_OUTPUT_HISTORY_LEN_STATE_KEY),
            caller_state=self._snapshot_caller(),
        )

    def restore(self, snapshot: HistorySnapshot) -> None:
        """Restore history from a snapshot, including compressed block rollback.

        Undoes ``_compress_state`` list replacements, removes orphaned
        ``CompressedBlock`` entries added during the rolled-back run, and
        restores each message's ``additional_properties`` exactly as captured
        — clearing the rolled-back attempt's compaction marks while keeping
        marks that legitimately predate the attempt.

        Used for retry rollback.
        """
        history_state = self._history_state()
        restored_messages = list(snapshot.messages)
        history_state["messages"] = restored_messages
        self._session.service_session_id = snapshot.service_session_id or None

        # Undo compressed block additions from the rolled-back run.
        compressed: list = history_state.get("compressed_msgs", [])
        if len(compressed) > snapshot.compressed_count:
            del compressed[snapshot.compressed_count :]

        if snapshot.pre_output_history_len is None:
            history_state.pop(PRE_OUTPUT_HISTORY_LEN_STATE_KEY, None)
        else:
            history_state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = snapshot.pre_output_history_len
        restore_message_properties(restored_messages, snapshot.message_properties)
        self._restore_caller(snapshot.caller_state)


class ModelRunTrace:
    """Attempt trace observer over the foundation sink port, never a writer owner.

    The caller supplies first-run causal links. The terminal is queued with
    emit_soon and deliberately never awaits the sink acknowledgement.
    """

    def __init__(
        self, *, interrupted: Callable[[], bool], service_side: Callable[[], bool], committed: Callable[[], bool]
    ) -> None:
        self.context: TrajectoryContext | None = None
        self.first_run_links: tuple[Link, ...] = ()
        self._interrupted = interrupted
        self._service_side = service_side
        self._committed = committed
        self.reset()

    def reset(self) -> None:
        self._trajectory_last_run_id = None
        self._run_attempts = 0
        self._trajectory_retry_run_id = None

    @contextlib.asynccontextmanager
    async def run(self, run_kwargs: AgentRunKwargs, *, stream: bool) -> AsyncGenerator[None]:
        """Bind one ``model.run`` operation around one ``agent.run`` attempt.

        The narrowed context rides two ways: ambiently (for tool middleware
        and side calls, bound before the attempt task is created so the
        task inherits it) and explicitly in ``client_kwargs`` for the
        kernel loop, whose lazy provider streams resolve outside this
        context.
        """
        base = self.context
        if base is None:
            yield
            return
        retry_run_id = self._trajectory_retry_run_id
        run_id = retry_run_id or new_analytics_id()
        context = base.with_run(run_id)
        raw_client_kwargs = run_kwargs.get("client_kwargs")
        run_kwargs["client_kwargs"] = {
            **(raw_client_kwargs if raw_client_kwargs is not None else {}),
            TRAJECTORY_CONTEXT_KWARG: context,
        }
        attempt_index = self._run_attempts
        self._run_attempts += 1
        previous_run_id = self._trajectory_last_run_id
        started_ns = time.monotonic_ns()
        sink = context.sink

        def _finished_draft(outcome: str) -> Any:
            return context.draft(
                TrajectoryEventType.MODEL_RUN_FINISHED,
                operation_id=run_id,
                parent_operation_id=base.innermost_model_operation_id,
                payload={
                    "outcome": outcome,
                    "duration_ms": max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
                },
                measurements={"/payload/duration_ms": measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)},
            )

        self._trajectory_last_run_id = run_id
        links = self.first_run_links if attempt_index == 0 else ()

        def _started_draft() -> Any:
            return context.draft(
                TrajectoryEventType.MODEL_RUN_STARTED,
                operation_id=run_id,
                parent_operation_id=base.innermost_model_operation_id,
                payload={
                    "attempt_index": attempt_index,
                    "stream": stream,
                    "service_side_storage": self._service_side(),
                    "previous_run_operation_id": previous_run_id,
                },
                links=links,
            )

        if retry_run_id is not None:
            self._trajectory_retry_run_id = None
            try:
                await sink.emit(
                    context.draft(
                        TrajectoryEventType.RETRY_STARTED,
                        operation_id=run_id,
                        payload={
                            "retry_mode": RetryMode.RUN,
                            "next_operation_id": run_id,
                            "previous_operation_id": previous_run_id,
                        },
                    )
                )
            except asyncio.CancelledError:
                # retry.started already names this run. Entry never reaches
                # the body, so queue its complete interrupted lifecycle before
                # preserving cancellation, without waiting on another ack.
                try:
                    sink.emit_soon(_started_draft())
                    sink.emit_soon(_finished_draft(ModelRunEndReason.INTERRUPTED))
                except Exception:
                    logger.debug("Trajectory interrupted retry settlement failed", exc_info=True)
                raise
            except Exception:
                logger.debug("Trajectory retry.started emit failed", exc_info=True)

        try:
            await sink.emit(_started_draft())
        except asyncio.CancelledError:
            # The writer commits the opening line before its shielded ack can
            # be cancelled. Context-manager entry will not reach the body
            # below, so settle the operation here before preserving cancel.
            try:
                sink.emit_soon(_finished_draft(ModelRunEndReason.INTERRUPTED))
            except Exception:
                logger.debug("Trajectory model.run.finished emit failed", exc_info=True)
            raise
        except Exception:
            logger.debug("Trajectory model.run.started emit failed", exc_info=True)
        outcome = ModelRunEndReason.COMPLETED
        with trajectory_scope(context):
            try:
                yield
            except asyncio.CancelledError:
                outcome = ModelRunEndReason.INTERRUPTED
                raise
            except BaseException:
                outcome = ModelRunEndReason.INTERRUPTED if self._interrupted() else ModelRunEndReason.FAILED
                raise
            finally:
                draft = _finished_draft(outcome)
                try:
                    # Queued, never awaited. The model has already answered by
                    # the time this runs and its task is gone, so an ack that
                    # waits out a slow writer holds the answer unpublished for
                    # that long — and a Stop pressed in that window finds
                    # nothing to cancel, marks the turn interrupted, and the
                    # finished answer is dropped. Cancellation cannot await
                    # here either: the scope is already unwinding. The line
                    # takes its sequence here, so this span is only left open
                    # when the process does not outlive the queue — and the
                    # turn still gets its terminal from the recorder's close.
                    sink.emit_soon(draft)
                except Exception:
                    logger.debug("Trajectory model.run.finished emit failed", exc_info=True)

    async def retry_scheduled(
        self, *, exc: BaseException, delay_seconds: int, fallback_to_blocking: bool = False
    ) -> None:
        """Record a service-side whole-run retry decision (``retry.scheduled``)."""
        context = self.context
        if context is None:
            return
        retry_run_id = new_analytics_id()
        self._trajectory_retry_run_id = retry_run_id
        committed = self._committed()
        try:
            await context.sink.emit(
                context.draft(
                    TrajectoryEventType.RETRY_SCHEDULED,
                    operation_id=retry_run_id,
                    payload={
                        "reason_code": _run_retry_reason(exc),
                        "delay_ms": max(0, int(delay_seconds * 1000)),
                        "retry_mode": RetryMode.RUN,
                        "previous_operation_id": self._trajectory_last_run_id,
                        "committed_work_present": committed,
                        "fallback_to_blocking": fallback_to_blocking,
                    },
                )
            )
        except Exception:
            logger.debug("Trajectory retry.scheduled emit failed", exc_info=True)


class AttemptRunner:
    """Drive one kernel pass through caller-supplied synchronous/async ports.

    Construction does not run hooks or mutate history. The shell enters this
    runner only inside its pass try, after its once-per-pass hooks and barriers.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        session: AgentSession,
        handle: AttemptTaskHandle,
        rollback: HistoryRollback,
        retry_participant: RetryParticipant | None,
        interrupt: InterruptProbe,
        trace: ModelRunTrace,
        stream_observer: Callable[[], StreamObserver | None] | None,
        content_exposed: Callable[[], bool],
        publish_retry: Callable[[str, int, int, int, BaseException], Awaitable[None]],
        interruptible_sleep: Callable[[int], Awaitable[bool]],
        max_retries: Callable[[], int],
        backoff_schedule: Callable[[], tuple[int, ...]],
        stream_timeout: Callable[[], float],
        committed_count: Callable[[], int],
        hosted_commits: Callable[[], tuple[str, ...]],
        recipe: AttemptRecipe,
    ) -> None:
        self._agent = agent
        self._session = session
        self.handle = handle
        self._rollback = rollback
        self._retry_participant = retry_participant
        self._interrupt = interrupt
        self.trace = trace
        self._stream_observer = stream_observer
        self._content_exposed = content_exposed
        self._publish_retry_notice = publish_retry
        self._interruptible_sleep = interruptible_sleep
        self._effective_max_retries = max_retries
        self._backoff_schedule = backoff_schedule
        self._stream_timeout = stream_timeout
        self._committed_count = committed_count
        self._hosted_commits_probe = hosted_commits
        self._stall_exhaustion = recipe.stall_exhaustion
        self._stall_error = recipe.stall_error
        self._retry_boundary = recipe.retry_boundary
        self._blocking_call_timing = recipe.blocking_call_timing
        self._before_attempt = recipe.before_attempt

    async def run(
        self, message: list[Message], run_kwargs: AgentRunKwargs, *, stream: bool, service_side: bool
    ) -> AgentResponse[Any]:
        if stream:
            if service_side:
                return await self._stream_with_retry(message, run_kwargs)
            if self._retry_participant is not None:
                self._retry_participant.begin_retry()
            try:
                return await self._stream_single_attempt(message, run_kwargs, watchdog=False)
            finally:
                if self._retry_participant is not None:
                    self._retry_participant.end_retry()
        return await self._run_blocking(message, run_kwargs, service_side=service_side)

    def _may_retry_attempt(self, run_kwargs: Mapping[str, object] | None = None) -> bool:
        """Whole-run retry gate: what the run already did or showed must never repeat.

        Answer text that went out cannot be taken back, so a run that showed
        any is never sent again; reasoning may stream again. Locally answered results are
        counted by the loop recorder. Provider-hosted calls (hosted MCP,
        hosted shell) execute inside the failed exchange itself and never
        reach the recorder: the validation middleware's run-scoped probe
        counts them. A live continuation token exempts the hosted gate: the
        retry then resumes the already-created background response instead
        of re-creating the request, so nothing hosted runs twice.
        """
        if self._content_exposed() or self._committed_count():
            return False
        hosted = self._hosted_commits_probe()
        if hosted and not has_live_continuation_token(run_kwargs):
            logger.warning(
                "Not retrying failed attempt: provider-hosted tool call(s) already executed (%s)",
                ", ".join(hosted),
            )
            return False
        return True

    def _restore_service_retry_inputs(self, run_kwargs: AgentRunKwargs) -> None:
        """Replay failed-call injections and discard every stale service handle."""
        raw_options = run_kwargs.get("options")
        options = raw_options if is_string_keyed_dict(raw_options) else {}
        if options.get("continuation_token") is None and self._retry_participant is not None:
            # A live token means the retry resumes the already-created
            # background response: the create that consumed the injections
            # succeeded, so the consumed transaction (and its retained-message
            # mirror for the terminal weave) must stay intact.  Replaying here
            # would strand the batch — polls skip consumption, then the
            # terminal commit would destroy the held replay.
            self._retry_participant.restore_for_retry()
        self._session.service_session_id = None
        raw_client_kwargs = run_kwargs.get("client_kwargs")
        client_kwargs = raw_client_kwargs if is_string_keyed_dict(raw_client_kwargs) else {}
        try:
            force_stateless = bool(self._agent.client.FORCES_STATELESS)
        except AttributeError:
            force_stateless = False
        resolution = resolve_storage_mode_and_handles(
            options,
            stores_by_default=True,
            client_kwargs=client_kwargs,
            force_stateless=force_stateless,
        )
        if raw_options is not None:
            run_kwargs["options"] = resolution.options_without_handles
        run_kwargs["client_kwargs"] = resolution.client_kwargs_without_handles

    def _raise_if_interrupted(self) -> None:
        """End the attempt before its task starts when an interrupt is already latched.

        An interrupt cancels the attempt task through its handle; one accepted
        while the operation was being recorded found no task to cancel. The
        kernel loop's wire policy makes the same check before each call, but a
        run with none (service-side storage) would still send the request. Called
        with no ``await`` before the task is created, so nothing can slip between.
        """
        if self._interrupt.is_interrupted:
            raise asyncio.CancelledError

    async def _run_agent(self, input: list[Message], run_kwargs: AgentRunKwargs) -> AgentResponse[Any]:
        """Run agent.run() in a child task so interrupt() can cancel it.

        Using a child task isolates cancellation: the parent task (and its
        post-processing) continues normally even when the child is cancelled.
        """

        if self._before_attempt is not None:
            self._before_attempt()

        # Keep the run call and await inside the attempt task. AgentTelemetryLayer
        # sets and resets non-streaming ContextVars inside its returned coroutine,
        # so either call-timing mode below preserves their asyncio context.
        async def _call() -> AgentResponse[Any]:
            return await self._agent.run(input, stream=False, **run_kwargs)

        async with self.trace.run(run_kwargs, stream=False):
            self._raise_if_interrupted()
            if self._blocking_call_timing is BlockingCallTiming.BEFORE_ATTEMPT_TASK:
                # Preserve the child's original call timing, including sync
                # exceptions from run(). Kernel telemetry binds its ContextVars
                # inside this coroutine, not while constructing it.
                run = self._agent.run(input, stream=False, **run_kwargs)
                self.handle.task = asyncio.create_task(cast("Coroutine[Any, Any, AgentResponse[Any]]", run))
            else:
                self.handle.task = asyncio.create_task(_call())
            try:
                return await self.handle.task
            finally:
                self.handle.task = None

    async def _run_blocking(
        self, message: list[Message], run_kwargs: AgentRunKwargs, *, service_side: bool
    ) -> AgentResponse[Any]:
        """Run locally with per-wire retry, or stored mode with outer retry."""
        if self._retry_participant is not None:
            self._retry_participant.begin_retry()
        try:
            if service_side:
                loop = StreamRetryLoop(
                    max_retries=self._effective_max_retries(),
                    backoff_schedule=self._backoff_schedule(),
                    is_retryable=is_retryable,
                    snapshot_history=self._rollback.snapshot,
                    restore_history=self._rollback.restore,
                    publish_retry_attempt=self._publish_retry_attempt,
                    is_interrupted=lambda: self._interrupt.is_interrupted,
                    interruptible_sleep=self._interruptible_sleep,
                    clean_error_message=clean_error_message,
                    after_restore=lambda: self._restore_service_retry_inputs(run_kwargs),
                    may_retry=lambda _exc: self._may_retry_attempt(run_kwargs),
                )
                result = await loop.run(lambda: self._run_agent(message, run_kwargs))
            else:
                result = await self._run_agent(message, run_kwargs)
        finally:
            if self._retry_participant is not None:
                self._retry_participant.end_retry()

        return result

    async def _stream_with_retry(
        self,
        current_input: list[Message],
        run_kwargs: AgentRunKwargs,
    ) -> AgentResponse[Any]:
        """Execute a streaming agent.run() with retry on transient errors.

        Delegates the generic retry/backoff/rollback machinery to
        :class:`StreamRetryLoop`.  On exhausted stall retries falls back
        to a non-streaming request for this turn.  ``StreamRetryLoop``
        captures its own history snapshot on entry, so callers no longer
        pre-snapshot.
        """
        loop = StreamRetryLoop(
            max_retries=self._effective_max_retries(),
            backoff_schedule=self._backoff_schedule(),
            is_retryable=is_retryable,
            snapshot_history=self._rollback.snapshot,
            restore_history=self._rollback.restore,
            publish_retry_attempt=self._publish_retry_attempt,
            is_interrupted=lambda: self._interrupt.is_interrupted,
            interruptible_sleep=self._interruptible_sleep,
            clean_error_message=clean_error_message,
            restore_on_stall_exhaustion=isinstance(self._stall_exhaustion, RestoreAndFallback),
            after_restore=lambda: self._restore_service_retry_inputs(run_kwargs),
            may_retry=lambda _exc: self._may_retry_attempt(run_kwargs),
        )

        async def _attempt() -> AgentResponse[Any]:
            await self._abandon_unlanded_stream()
            return await self._stream_single_attempt(current_input, run_kwargs)

        if self._retry_participant is not None:
            self._retry_participant.begin_retry()
        try:
            try:
                return await loop.run(_attempt)
            except StreamStallExhausted as exc:
                if isinstance(self._stall_exhaustion, KeepAndRaise):
                    raise
                # The retry loop restored failed stream state before this
                # blocking replacement, including any consumed injection.
                await self._stall_exhaustion.reject_stream("Streaming stalled; using blocking fallback")
                await self.trace.retry_scheduled(exc=exc, delay_seconds=0, fallback_to_blocking=True)
                await self._abandon_unlanded_stream()
                return await self._run_agent(current_input, run_kwargs)
        finally:
            if self._retry_participant is not None:
                self._retry_participant.end_retry()

    async def _abandon_unlanded_stream(self) -> None:
        """A fresh run replaces what an abandoned attempt streamed without landing a response."""
        observer = self._stream_observer() if self._stream_observer is not None else None
        if observer is not None:
            await observer.on_retry_boundary()

    async def _publish_retry_attempt(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
    ) -> None:
        """Record the whole-run retry before invoking the caller notice port."""
        await self.trace.retry_scheduled(exc=exc, delay_seconds=delay_seconds)
        await self._publish_retry_notice(message, attempt, max_attempts, delay_seconds, exc)

    async def _stream_single_attempt(
        self,
        current_input: list[Message],
        run_kwargs: AgentRunKwargs,
        *,
        watchdog: bool = True,
    ) -> AgentResponse[Any]:
        """Consume one stream in its owning task and finalize under the watchdog.

        Every update reaches the caller's stream observer as it arrives; a
        retry boundary reaches it before the fresh attempt's updates.
        """
        import time as _time

        if self._before_attempt is not None:
            self._before_attempt()

        # Tracks the last point at which our per-chunk watchdog (re)armed
        # its timer.  Updated after each successful ``__anext__()``, on each
        # progress report, and after the stream is fully drained (before
        # the finalize wait).  Used below to distinguish a genuine stall — our
        # timer actually elapsed — from a ``TimeoutError`` bubbling up
        # from inside the stream/transport (e.g. httpx or SDK internals),
        # which would otherwise be mis-labelled "Stream stalled" and
        # retried with the wrong error message.
        last_wait_start = _time.monotonic()

        async def _watched[T](awaitable: Awaitable[T]) -> T:
            # Idle timing: work a pull waits on before its next chunk (a
            # compaction pass and its LAST_WORDS side call) reports progress,
            # and every report restarts the stall timer.  The pull stays in
            # this task: its telemetry ContextVars are set and reset here.
            timeout = self._stream_timeout()
            if timeout <= 0:
                # wait_for stalls a non-positive timeout even when the pull
                # would finish without suspending; asyncio.timeout does not.
                return await asyncio.wait_for(awaitable, timeout=timeout)
            event_loop = asyncio.get_running_loop()
            watching = True
            async with asyncio.timeout(timeout) as deadline:

                def _on_progress() -> None:
                    nonlocal last_wait_start
                    # A task the pull spawned can report after the pull
                    # settled, or while the stall is already cancelling it.
                    if watching and not deadline.expired():
                        last_wait_start = _time.monotonic()
                        deadline.reschedule(event_loop.time() + timeout)

                try:
                    with wire_progress_scope(_on_progress):
                        return await awaitable
                finally:
                    watching = False

        async def _iterate_and_finalize() -> AgentResponse[Any]:
            nonlocal last_wait_start

            # ``agent.run(stream=True)`` is invoked INSIDE this task so the
            # OTel telemetry ContextVars (e.g.
            # ``INNER_RESPONSE_TELEMETRY_CAPTURED_FIELDS``) are set and
            # reset in the same asyncio context — the telemetry cleanup
            # hook fires when the stream is consumed, which happens here.
            # The streaming layer keeps the set-in-call / reset-in-finalizer
            # shape; same-task creation remains the correct pattern here.
            # Matches the blocking path (see ``_run_agent``).
            stream: ResponseStream[AgentResponseUpdate, AgentResponse[Any]] = self._agent.run(
                current_input, stream=True, **run_kwargs
            )
            completed = False

            try:
                observer = self._stream_observer() if self._stream_observer is not None else None

                # Per-chunk watchdog: each __anext__() gets the stall timeout
                # individually, so the timer resets whenever a chunk arrives
                # (or the work before it reports progress).
                # This measures stream-idle time (matching httpx read-timeout
                # semantics) — tool executions between chunks don't count.
                #
                # When a ``function_call`` chunk is observed, the tool loop
                # synchronously invokes the tool inside the NEXT ``__anext__()``
                # call — so that await blocks for the entire tool duration.
                # Tools (especially sub-agents) can legitimately run for
                # minutes, which would trip a short stream-idle timeout.  To
                # avoid a false "Stream stalled" during tool execution we
                # suspend the watchdog until the post-tool chunk arrives.
                # User-initiated cancellation still works — it flows through
                # the caller control port, which cancels the owned task handle.
                expecting_tool_result = False
                aiter = stream.__aiter__()
                while True:
                    try:
                        if not watchdog or expecting_tool_result:
                            update = await aiter.__anext__()
                        else:
                            update = await _watched(aiter.__anext__())
                    except StopAsyncIteration:
                        break
                    last_wait_start = _time.monotonic()

                    if is_retry_boundary_update(update):
                        if observer is not None:
                            await observer.on_retry_boundary()
                        if self._retry_boundary is RetryBoundaryPolicy.SKIP:
                            continue

                    if observer is not None:
                        await observer.on_update(update)
                        # Showing the update is not waiting on the stream.
                        last_wait_start = _time.monotonic()
                    has_function_call = False
                    has_function_result = False
                    text_chunk = ""
                    for content in update.contents or []:
                        if content.type == "text" and content.text:
                            text_chunk += content.text
                        elif content.type == "function_call" and not content.informational_only:
                            has_function_call = True
                        elif content.type == "function_result":
                            has_function_result = True
                    if has_function_result or text_chunk:
                        expecting_tool_result = False
                    if has_function_call:
                        expecting_tool_result = True

                    await asyncio.sleep(0)  # yield to event loop between chunks

                # Also guard finalize with the same per-chunk timeout — it
                # shouldn't block once the iterator exhausts, but it's still
                # an await on the same underlying stream.
                last_wait_start = _time.monotonic()
                if not watchdog:
                    response = await stream.get_final_response()
                else:
                    response = await _watched(stream.get_final_response())
                completed = True
                return response
            except TimeoutError:
                if not watchdog:
                    raise
                # ``asyncio.TimeoutError`` is the builtin ``TimeoutError`` on
                # Python 3.11+, so a ``TimeoutError`` raised from *inside*
                # ``__anext__()`` / ``get_final_response()`` (httpx or SDK
                # internals) would look identical to our own ``wait_for``
                # firing.  Only treat it as a stall when the current watchdog
                # window actually elapsed (with a small margin for scheduling
                # jitter); otherwise re-raise so ``is_retryable`` classifies
                # the original exception with its real message.
                idle = _time.monotonic() - last_wait_start
                if idle + 0.5 < self._stream_timeout():
                    raise
                raise self._stall_error(self._stream_timeout()) from None
            finally:
                # A stream left before its final response is closed here, in
                # this task: its cleanup hooks (the OTel ContextVar resets
                # among them) must run before the attempt ends, in the context
                # ``.run(stream=True)`` was called in. A pull that failed ran
                # them already and one that was cancelled closed the stream; a
                # cancel between chunks, a failing observer or a stall while
                # finalizing leaves it open, and garbage collection would close
                # only the bare generators under it, never its hooks.
                if not completed:
                    try:
                        await stream.aclose()
                    except Exception:
                        logger.debug("Failed to close an abandoned attempt stream", exc_info=True)

        # Wrap the iteration in a child task so ``interrupt()`` can cancel
        # it.  Without this, the streaming path has no task handle to
        # cancel and user-initiated interrupts cannot stop an in-flight
        # stream/tool — including long-running sub-agents.  (The blocking
        # path already does this in :meth:`_run_agent`.)
        async with self.trace.run(run_kwargs, stream=True):
            self._raise_if_interrupted()
            self.handle.task = asyncio.create_task(_iterate_and_finalize())
            try:
                return await self.handle.task
            finally:
                self.handle.task = None
