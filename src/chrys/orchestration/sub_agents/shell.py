# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Common SubAgentTool operation, human decisions, and terminal event ownership.

The tool recipe opens quota, writer and start-hook scopes before attaching its
backend policy. The same shell remains bound through pause, terminal writer,
usage drain, end hook and live-approval cleanup. Its operation barrier is released
only by that recipe's final cleanup. A backend pass finishing does not release it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationAborted,
    InvocationAnswerWithdrawn,
    InvocationCascadeAborted,
    InvocationPaused,
    InvocationResumed,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.models.failures import FailureFacts
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.util.once_close import finish_close
from chrys.orchestration.invoker.contracts import (
    AbortCause,
    Aborted,
    ContinuationTicket,
    Failed,
    InvocationConversation,
    InvocationOutcome,
    RunRequest,
    SubAgentStatus,
)
from chrys.orchestration.invoker.evidence import InvocationEvidence
from chrys.orchestration.invoker.origin import BoundEmitter
from chrys.orchestration.invoker.resources import OperationLifetime


class SubAgentPolicy(Protocol):
    """Explicit result, persistence and cancellation policies of one backend."""

    @property
    def backend(self) -> InvocationConversation: ...
    def request(self, ticket: ContinuationTicket | None) -> RunRequest: ...
    async def project_result(self, outcome: InvocationOutcome) -> str | None: ...
    async def prepare_pause(self) -> None:
        """Settle the failed pass before its pause, or before the automatic abort that replaces one."""
        ...

    async def record_pause(self) -> None: ...
    def pause_event(self) -> InvocationPaused: ...
    def resolve_late_cascade(self, decision: asyncio.Future[str]) -> None: ...
    def check_terminal_race(self) -> None:
        """Preserve the backend's late-cascade policy after a human decision.

        Kernel may publish InvocationResumed after Retry when cascade arrives
        late, then take Aborted(CASCADE) at the next loop entry. ACP rechecks
        after the decision. This ordering difference is the existing baseline.
        """
        ...

    def prepare_retry(self) -> tuple[str, ...]:
        """Prepare the next pass's input; return the segment ids of the answer it dropped to ask again."""
        ...

    async def abort_result(self, *, by_user: bool) -> str: ...
    @property
    def last_error(self) -> str: ...
    @property
    def last_error_display(self) -> MessageRef | None:
        """What ``last_error`` means to the user, when the classifier can say more than its text."""
        ...

    @property
    def last_error_hint(self) -> MessageRef | None: ...
    @property
    def last_error_facts(self) -> FailureFacts | None:
        """What ``last_error`` is, when a classified exception ended the pass."""
        ...

    def latch_abort(self, cause: AbortCause) -> None: ...
    async def cancel_active(self) -> None: ...
    async def finalize_cancellation(self) -> None: ...
    async def before_cascade_event(self) -> None: ...
    async def run_cancelled(self) -> None: ...
    async def finish_run(self) -> None: ...


class SubAgentToolShell:
    """One logical invocation and the sole child caller OperationBinding."""

    def __init__(
        self,
        *,
        origin: InvocationOrigin,
        tool_name: str,
        agent_name: str,
        event_bus: EventBus | None,
        operation: OperationLifetime | None = None,
        human_failure_decisions: bool = True,
    ) -> None:
        self.origin = origin
        self._tool_name = tool_name
        self._agent_name = agent_name
        self._emitter = BoundEmitter(event_bus, origin)
        self._bus = event_bus
        self._policy: SubAgentPolicy | None = None
        self._operation = operation
        # Without a card that can show a pause, nobody could answer it: a failure then ends the child.
        self._human_failure_decisions = human_failure_decisions
        self._status = SubAgentStatus.IDLE
        self._pending_decision: asyncio.Future[str] | None = None
        self._owner_close_cause: AbortCause | None = None
        self._cascade_requested = False
        self._cascade_published = False
        self._cascade_publish_task: asyncio.Task[None] | None = None
        self._parent_interrupted_result_commit: Callable[[], None] | None = None
        self._parent_interrupt_commit_bound = False
        self._cancellation_finalized = False
        self._cancellation_finalize_lock = asyncio.Lock()
        self.outcome: InvocationOutcome | None = None
        self.evidence = InvocationEvidence(origin.invocation_id)

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def bus(self) -> EventBus | None:
        return self._bus

    @property
    def owner_close_cause(self) -> AbortCause | None:
        return self._owner_close_cause

    def bind_parent_interrupt_commit(self, callback: Callable[[], None]) -> None:
        """Bind the recipe's parent commit exactly once, before execution."""
        if self._parent_interrupt_commit_bound:
            raise RuntimeError("The parent interrupt commit is already bound.")
        self._parent_interrupt_commit_bound = True
        self._parent_interrupted_result_commit = callback

    @property
    def policy(self) -> SubAgentPolicy:
        if self._policy is None:
            raise RuntimeError("The sub-agent policy has not been attached.")
        return self._policy

    def attach_policy(self, policy: SubAgentPolicy) -> None:
        """Install the invocation recipe once, without changing its owner."""
        if self._policy is not None:
            raise RuntimeError("The sub-agent policy is already attached.")
        self._policy = policy
        if self._owner_close_cause is not None:
            policy.latch_abort(self._owner_close_cause)

    @property
    def invocation_id(self) -> str:
        return self.origin.invocation_id

    @property
    def status(self) -> SubAgentStatus:
        return self._status

    @property
    def is_paused(self) -> bool:
        return self._status is SubAgentStatus.PAUSED

    @property
    def cascade_requested(self) -> bool:
        return self._cascade_requested

    def set_status(self, status: SubAgentStatus) -> None:
        self._status = status

    def attach_operation(self, operation: OperationLifetime) -> None:
        if self._operation is not None:
            raise RuntimeError("The sub-agent operation is already attached.")
        self._operation = operation

    def _require_operation(self) -> OperationLifetime:
        if self._operation is None:
            raise RuntimeError("The sub-agent operation has not been attached.")
        return self._operation

    @property
    def drained(self) -> Awaitable[None]:
        return self._require_operation().drained

    def request_close(self, cause: AbortCause) -> None:
        operation = self._require_operation()
        self._owner_close_cause = cause
        self._latch_cascade()
        if self._policy is not None:
            self._policy.latch_abort(cause)
            operation.close_with(self.cascade_abort)
        else:
            operation.cancel_preparation()

    def _latch_cascade(self) -> None:
        self._cascade_requested = True
        self._commit_parent_interrupted_result()
        self._resolve_decision("cascade_abort")

    def _commit_parent_interrupted_result(self) -> None:
        callback = self._parent_interrupted_result_commit
        if callback is not None:
            self._parent_interrupted_result_commit = None
            callback()

    def _resolve_decision(self, decision: str) -> bool:
        future = self._pending_decision
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def request_retry(self) -> bool:
        return self._resolve_decision("retry")

    def request_abort(self) -> bool:
        return self._resolve_decision("abort")

    async def cascade_abort(self) -> None:
        self._latch_cascade()
        if self._policy is not None:
            self._policy.latch_abort(self._owner_close_cause or AbortCause.CASCADE)
            await self._policy.cancel_active()

    async def finalize_cancellation(self) -> None:
        self._commit_parent_interrupted_result()
        async with self._cancellation_finalize_lock:
            if self._cancellation_finalized:
                return
            if self._policy is not None:
                await self._policy.finalize_cancellation()
            self._cancellation_finalized = True

    def schedule_cascade_event(self) -> None:
        """ACP schedules this after permission cancellation and before remote cancel."""
        if self._cascade_publish_task is None:
            self._cascade_publish_task = asyncio.create_task(self.publish_cascade_event())

    async def await_cascade_publish(self, drain: Callable[[asyncio.Future[None]], Awaitable[None]]) -> None:
        """Drain the owned publication with the caller policy's cancel handling."""
        task = self._cascade_publish_task
        if task is None:
            await self.publish_cascade_event()
        else:
            await drain(task)

    async def publish_cascade_event(self) -> None:
        if self._cascade_published:
            return
        self._cascade_published = True
        self._status = SubAgentStatus.CASCADE_ABORTED
        await self.policy.before_cascade_event()
        if self._bus is not None:
            await self._emitter.publish(
                InvocationCascadeAborted(
                    origin=self.origin,
                    agent_name=self._agent_name,
                    session_id=self.origin.session_id or None,
                )
            )

    async def await_decision(self) -> str:
        """Persist first, then announce and await a decision on this invocation."""
        await self.policy.prepare_pause()
        self._status = SubAgentStatus.PAUSED
        await self.policy.record_pause()
        self._pending_decision = asyncio.get_running_loop().create_future()
        self.policy.resolve_late_cascade(self._pending_decision)
        if self._bus is not None and not self._pending_decision.done():
            await self._emitter.publish(self.policy.pause_event())
        try:
            return await self._pending_decision
        finally:
            self._pending_decision = None

    async def publish_resumed(self) -> None:
        """Announce a human Retry with this operation's captured origin."""
        await self._emitter.publish(
            InvocationResumed(
                origin=self.origin,
                agent_name=self._agent_name,
                session_id=self.origin.session_id or None,
            )
        )

    async def publish_withdrawn(self, segment_ids: tuple[str, ...]) -> None:
        """The retry dropped the answer these segments showed; delivered whole, as the drop is done."""
        event = InvocationAnswerWithdrawn(
            origin=self.origin,
            agent_name=self._agent_name,
            session_id=self.origin.session_id or None,
            segment_ids=segment_ids,
        )
        await finish_close(asyncio.create_task(self._emitter.publish(event)))

    async def _end_after_failure(self, *, by_user: bool) -> str:
        """End the failed invocation as aborted: on the user's Abort, or at once where no card could pause.

        A cascade that landed since the pass converged wins over both, exactly as at the top of the loop.
        """
        if self._cascade_requested:
            raise asyncio.CancelledError
        self._status = SubAgentStatus.ABORTED
        result = await self.policy.abort_result(by_user=by_user)
        if self._bus is not None:
            await self._emitter.publish(
                InvocationAborted(
                    origin=self.origin,
                    agent_name=self._agent_name,
                    last_error=self.policy.last_error,
                    last_error_display=self.policy.last_error_display,
                    last_error_hint=self.policy.last_error_hint,
                    last_error_facts=self.policy.last_error_facts,
                    session_id=self.origin.session_id or None,
                )
            )
        self.policy.check_terminal_race()
        return result

    async def run(self) -> str:
        """Consume backend tickets and accumulate each pass once across human Retry."""
        try:
            while True:
                if self._cascade_requested:
                    raise asyncio.CancelledError
                ticket = self.outcome.continuation if isinstance(self.outcome, Failed) else None
                self.outcome = await self.policy.backend.run(self.policy.request(ticket))
                self.evidence = self.evidence.add(self.outcome.effects)
                if isinstance(self.outcome, Aborted):
                    if self.outcome.cause in {AbortCause.CASCADE, AbortCause.OWNER_CLOSE}:
                        self._latch_cascade()
                    raise asyncio.CancelledError
                result = await self.policy.project_result(self.outcome)
                if result is not None:
                    self.policy.check_terminal_race()
                    return result
                if not self._human_failure_decisions:
                    await self.policy.prepare_pause()
                    return await self._end_after_failure(by_user=False)
                decision = await self.await_decision()
                self.policy.check_terminal_race()
                if decision == "retry":
                    # Announced before the answer is dropped: a view carries the
                    # transcript on from the resume, then withdraws that answer.
                    if self._bus is not None:
                        await self.publish_resumed()
                    withdrawn = self.policy.prepare_retry()
                    if withdrawn and self._bus is not None:
                        await self.publish_withdrawn(withdrawn)
                    self.policy.check_terminal_race()
                    continue
                if decision == "abort":
                    return await self._end_after_failure(by_user=True)
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            await self.policy.run_cancelled()
            raise
        finally:
            await self.policy.finish_run()
