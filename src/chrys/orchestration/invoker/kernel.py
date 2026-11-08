# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Kernel pass backend: one L0 task owner, live tickets and typed outcomes.

The resource Conversation owns runtime/operation drainage. This backend borrows
that scope and executes passes; it neither acquires a Turn lease nor owns the
caller's post-pass writer, history projection or artificial decision loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.errors import clean_error_message
from chrys.foundation.models.invocations import InvocationOrigin, PassHandle
from chrys.foundation.retry import HistorySnapshot
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.kernel import AgentResponse, AgentSession, LoopRecorder
from chrys.service.tools.background import TaskScope, current_scope

from .attempts import AgentRunKwargs, AttemptRunner, AttemptTaskHandle
from .contracts import (
    AbortCause,
    Aborted,
    AbortResult,
    ContinuationCapability,
    ContinuationTicket,
    Failed,
    FailureDisposition,
    InvocationOutcome,
    Ok,
    OverlappingRun,
    PreparedClosed,
    RunRequest,
    StaleContinuation,
    StopCause,
    validate_request,
)
from .evidence import UNKNOWN_COUNT, Completeness, Count, PassEvidence, hosted_count
from .origin import current_invocation_origin, invocation_routing_key
from .resources import Conversation, PassResources
from .usage import PassUsageProbe

if TYPE_CHECKING:
    from chrys.service.context.compaction import UnifiedContextStrategy


class KernelPassObserver(Protocol):
    """Caller-specific presentation and reset boundaries, all inside the pass try."""

    async def begin(self, request: RunRequest, resources: PassResources) -> None: ...
    async def succeeded(self, response: AgentResponse[Any], /) -> None: ...
    async def failed(self, error: Exception, /) -> None: ...
    async def finished(self) -> None: ...
    async def interrupt(self) -> None: ...
    def cancelled(self) -> None: ...
    def abort_cause(self) -> AbortCause | None: ...


@dataclass(slots=True)
class _ActivePass:
    handle: PassHandle
    resources: PassResources
    done: asyncio.Event
    task: asyncio.Task[Any]
    cause: AbortCause | None = None
    close_cancelled: bool = False
    steerable: bool = False
    """While the attempt runs until its final answer lands: not during the observer's begin, nor after."""

    def request_close(self, cause: AbortCause) -> None:
        self.cause = cause
        self.resources.request_close(cause)
        # The L0 slot is empty during observer begin and after the attempt.
        # Wake the entire pass without acquiring a second attempt-task owner.
        if not self.close_cancelled:
            self.close_cancelled = self.task.cancel()

    @property
    def drained(self) -> Awaitable[None]:
        return self._drain()

    async def _drain(self) -> None:
        await self.done.wait()


class KernelConversation:
    """A reusable backend with no overlapping passes or serializable live tickets."""

    def __init__(
        self,
        *,
        owner: Conversation,
        session: AgentSession,
        attempts: AttemptRunner,
        attempt_handle: AttemptTaskHandle,
        observer: KernelPassObserver,
        run_kwargs: Callable[[], AgentRunKwargs],
        stream: Callable[[], bool],
        service_side: Callable[[], bool],
        recorder: LoopRecorder | None,
        compaction_strategy: UnifiedContextStrategy | None = None,
        hosted_observed: Callable[[], tuple[str, ...]] | None,
        start_hooks: tuple[Callable[[], None], ...],
        failure_disposition: FailureDisposition,
    ) -> None:
        self.owner = owner
        self.session = session
        self.compaction_strategy = compaction_strategy
        self.conversation_id = new_analytics_id()
        self._attempts = attempts
        self._attempt_handle = attempt_handle
        self._observer = observer
        self._run_kwargs = run_kwargs
        self._stream = stream
        self._service_side = service_side
        self._recorder = recorder
        self._hosted_observed = hosted_observed
        self._start_hooks = start_hooks
        self._failure_disposition = failure_disposition
        self._active: _ActivePass | None = None
        self._generation = 0
        self._ordinals: dict[str, int] = {}
        self._ticket: ContinuationTicket | None = None
        self._ticket_origin: InvocationOrigin | None = None

    @property
    def active_handle(self) -> PassHandle | None:
        return self._active.handle if self._active is not None else None

    @property
    def active_abort_cause(self) -> AbortCause | None:
        """Why the running pass was asked to stop: the last request, but a Stop leaves a steer's cause (:meth:`abort`)."""
        return self._active.cause if self._active is not None else None

    @property
    def state_generation(self) -> int:
        return self._generation

    def invalidate_continuation(self) -> None:
        """Called synchronously by a state restore/rebuild before writing history."""
        self._generation += 1
        self._ticket = None
        self._ticket_origin = None

    def validate(self, request: RunRequest) -> None:
        """Admission has no mutation, hooks, history access or process creation."""
        if self.owner.closing:
            if request.continuation is not None:
                raise StaleContinuation("Continuation owner is closed")
            raise PreparedClosed("Conversation is closing")
        if self._active is not None:
            raise OverlappingRun("Conversation already has an active pass")
        validate_request(request, ContinuationCapability.CONTINUE_HISTORY)
        if request.continuation is not None and not self.continuation_is_live(request.continuation, request.origin):
            raise StaleContinuation("Continuation no longer names this live state")

    def continuation_is_live(self, ticket: ContinuationTicket, origin: InvocationOrigin) -> bool:
        """Read ticket identity, generation and invocation without consuming any state.

        A later workflow attempt of the same invocation may redeem its predecessor's ticket.
        """
        return (
            not self.owner.closing
            and ticket is self._ticket
            and ticket.state_generation == self._generation
            and origin.same_invocation(self._ticket_origin)
        )

    def latch_abort(self, cause: AbortCause) -> None:
        """Preserve a shell's synchronous close cause before it cancels its task."""
        if self._active is not None:
            self._active.cause = cause

    def can_steer(self, handle: PassHandle) -> bool:
        """Whether *handle*'s pass is still producing and nothing asked it to stop.

        Before its attempt starts, the next model call delivers what the
        caller queued anyway; once its final answer landed (:meth:`answer_landed`)
        the answer is complete and is never dropped, though the attempt may
        still run its after-run work; a pass already aborting keeps its cause.
        """
        active = self._active
        return active is not None and active.handle is handle and active.steerable and active.cause is None

    def answer_landed(self) -> None:
        """The running pass's final answer landed, with no tool call to run: no steer drops it from now on."""
        if self._active is not None:
            self._active.steerable = False

    async def steer(self, handle: PassHandle, *, before_abort: Callable[[], Awaitable[None]]) -> AbortResult:
        """Abort *handle*'s pass for a steer, accepted only while :meth:`can_steer` holds.

        Accepting latches the cause at once, before *before_abort* (the
        caller's sub-agent cascade, which can end the pass's attempt itself)
        runs: from then on the pass ends ``Aborted(STEER)`` whatever its
        attempt does, and :meth:`abort` keeps that cause.
        """
        active = self._active
        if active is None or not self.can_steer(handle):
            return AbortResult.ALREADY_CONVERGED
        active.cause = AbortCause.STEER
        await before_abort()
        if self._active is not active:
            return AbortResult.ALREADY_CONVERGED
        return await self._interrupt(active)

    async def abort(self, handle: PassHandle, cause: AbortCause) -> AbortResult:
        """Stop *handle*'s pass for *cause*.

        A steered pass keeps its cause against a Stop: it already gave up what
        it was producing, so the Stop ends the turn once the pass settled (the
        caller's part) instead of turning it into an interrupted one. Any other
        cause (an owner closing) takes over.
        """
        active = self._active
        if active is None or active.handle is not handle:
            return AbortResult.ALREADY_CONVERGED
        if active.cause is not AbortCause.STEER or cause is not AbortCause.USER_CANCEL:
            active.cause = cause
        return await self._interrupt(active)

    async def _interrupt(self, active: _ActivePass) -> AbortResult:
        await self._observer.interrupt()
        # The await can converge this pass; never cancel a successor's L0 task.
        if self._active is active:
            self._attempt_handle.cancel()
        return AbortResult.REQUESTED

    def _effects(self, handle: PassHandle) -> PassEvidence:
        return PassEvidence(
            handle.invocation_id,
            handle.pass_id,
            UNKNOWN_COUNT,
            Count(self._recorder.committed_count, Completeness.EXACT) if self._recorder is not None else UNKNOWN_COUNT,
            hosted_count(self._hosted_observed() if self._hosted_observed is not None else None),
            False,
        )

    @property
    def service_session_storage_enabled(self) -> bool:
        return self._service_side()

    @property
    def service_session_id(self) -> str:
        return self.session.service_session_id or ""

    @service_session_id.setter
    def service_session_id(self, value: str) -> None:
        self.session.service_session_id = value or None

    @property
    def history_state(self) -> dict[str, Any]:
        """Kernel-only mutable state port; it is absent from the common protocol."""
        return self.session.state.setdefault("chrys_history", {})

    @history_state.setter
    def history_state(self, value: dict[str, Any]) -> None:
        self.invalidate_continuation()
        self.session.state["chrys_history"] = value

    def checkpoint(self) -> HistorySnapshot:
        return self._attempts._rollback.snapshot()

    def restore(self, snapshot: HistorySnapshot) -> None:
        self.invalidate_continuation()
        self._attempts._rollback.restore(snapshot)

    def export_audit(self) -> Mapping[str, object]:
        """Return a recursive snapshot; live Message/Content identity is not retained."""
        return {"backend": "kernel", "conversation_id": self.conversation_id, "state": deepcopy(self.session.state)}

    async def run(self, request: RunRequest) -> InvocationOutcome:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Running a conversation requires an asyncio task.")
        self.validate(request)
        self.invalidate_continuation()
        invocation_id = request.origin.invocation_id
        ordinal = self._ordinals.get(invocation_id, 0) + 1
        self._ordinals[invocation_id] = ordinal
        handle = PassHandle(invocation_id, f"{invocation_id}:{ordinal}")
        active = _ActivePass(handle, PassResources(self._attempt_handle, self._start_hooks), asyncio.Event(), task)
        unbind = self.owner.bind_pass(active)
        self._active = active
        origin_token = current_invocation_origin.set(request.origin)
        invocation_token = invocation_routing_key.set(invocation_id)
        # The tool calls of this pass start background commands for, and sleep on those of, this agent.
        scope_token = current_scope.set(TaskScope.of(request.origin))
        response: AgentResponse[Any] | None = None
        error: Exception | None = None
        cancellation: asyncio.CancelledError | None = None
        usage_probe = PassUsageProbe()
        try:
            try:
                try:
                    if self._recorder is not None:
                        self._recorder.reset()
                    await self._observer.begin(request, active.resources)
                    if active.cause is None:
                        run_kwargs = self._run_kwargs()
                        middleware = run_kwargs.get("middleware")
                        # Keep the kwargs identity: continuation observers are
                        # bound to this dict and retry may replace its options.
                        run_kwargs["middleware"] = [*(middleware or ()), usage_probe]
                        active.steerable = True
                        try:
                            response = await self._attempts.run(
                                list(request.messages),
                                run_kwargs,
                                stream=self._stream(),
                                service_side=self._service_side(),
                            )
                        finally:
                            active.steerable = False
                            if middleware is None:
                                run_kwargs.pop("middleware", None)
                            else:
                                run_kwargs["middleware"] = middleware
                        await self._observer.succeeded(response)
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    # Read before cancelled() updates presentation flags: an
                    # external cancellation must not manufacture a caller cause.
                    active.cause = active.cause or self._observer.abort_cause()
                    self._observer.cancelled()
                    if task.cancelling() == 0:
                        # Cancelling only the owned L0 task retains the shell's
                        # historical interrupt projection. Cancelling this run
                        # task externally must still propagate without a cause.
                        active.cause = active.cause or self._observer.abort_cause()
                except Exception as exc:
                    error = exc
                    await self._observer.failed(exc)
                finally:
                    if cancellation is None:
                        active.cause = active.cause or self._observer.abort_cause()
                    await self._observer.finished()
                    if cancellation is None:
                        active.cause = active.cause or self._observer.abort_cause()
            except asyncio.CancelledError as exc:
                # Close also wakes failed()/finished() after the L0 slot clears.
                if cancellation is None:
                    active.cause = active.cause or self._observer.abort_cause()
                cancellation = exc
                self._observer.cancelled()
            effects = self._effects(handle)
            usage = usage_probe.snapshot(response_received=response is not None)
            if active.cause is not None:
                return Aborted(
                    handle=handle,
                    usage=usage,
                    effects=effects,
                    stop=StopCause.ABORTED,
                    continuation=None,
                    cause=active.cause,
                )
            if cancellation is not None:
                raise cancellation
            if error is not None:
                self._ticket = ContinuationTicket(
                    self.conversation_id, handle.pass_id, self._generation, ContinuationCapability.CONTINUE_HISTORY
                )
                self._ticket_origin = request.origin
                return Failed(
                    handle=handle,
                    usage=usage,
                    effects=effects,
                    stop=StopCause.FAILED,
                    continuation=self._ticket,
                    disposition=self._failure_disposition,
                    error=clean_error_message(error),
                    exception=error,
                )
            if response is None:
                raise RuntimeError("A successful kernel pass did not produce a response.")
            return Ok(
                handle=handle,
                usage=usage,
                effects=effects,
                stop=StopCause.COMPLETED,
                continuation=None,
                segments=tuple(content for message in response.messages for content in message.contents),
                backend_payload=response,
            )
        finally:
            if active.close_cancelled:
                # Pair only the cancellation this pass issued, never the
                # operation shell's or an external caller's cancellation count.
                task.uncancel()
            current_invocation_origin.reset(origin_token)
            invocation_routing_key.reset(invocation_token)
            current_scope.reset(scope_token)
            unbind()
            self._active = None
            active.done.set()
