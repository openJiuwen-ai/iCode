# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The workflow shell around one agent-node activation.

One shell per activation: :meth:`WorkflowAgentShell.open` builds a private
kernel or ACP conversation for the node's agent and model binding, :meth:`run`
executes one pass per scheduler attempt, :meth:`abort` converges the in-flight
pass with a caller cause, and :meth:`close` unbinds and releases everything in
reverse. The shell is the third caller of the invoker contract after the main
turn and the sub-agent tool. Inside a pass a kernel node retries a failed
request as the chat agent does, in place on the wire or as a whole run under
service-side storage; across passes the scheduler decides from the
:class:`FailureReport` each attempt yields, and the shell resumes what the
failed pass left: a kernel pass continues its repaired history, an ACP pass
starts a new session.

Every scheduler attempt publishes under its own origin, so a presentation
never folds one attempt's late facts into another.

Kernel nodes are built like the chat agent — builtin, sub-agent, MCP and skill
tools plus the profile's auto-loaded memory — and the ``ask_user`` tool is
dropped whenever the run is headless. No workflow surface shows a per-child
card, so a failed child ends at once instead of pausing; the shell routes the
engine's approval mode and the run's abort to the node's own sub-agents.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal

from chrys.foundation.errors import ErrorKind, classify_error, clean_error_message, is_retryable
from chrys.foundation.errors.display import display_fields
from chrys.foundation.events.types import (
    InvocationAnswerWithdrawn,
    InvocationMessage,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationStarted,
)
from chrys.foundation.hosted_tools import HostedToolStatus
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.trajectory.context import TRAJECTORY_CONTEXT_KWARG, TrajectoryContext, workflow_node_actor
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id
from chrys.foundation.util.once_close import finish_close
from chrys.kernel import AgentResponse, Message
from chrys.orchestration.invoker.acp import AcpConversation, AcpInvocationCounters
from chrys.orchestration.invoker.acp_protocol import AcpUpdateTranslator
from chrys.orchestration.invoker.attempts import drop_continuation_token, has_live_continuation_token
from chrys.orchestration.invoker.child_history import active_input_message
from chrys.orchestration.invoker.content_projection import ContentProjector
from chrys.orchestration.invoker.contracts import (
    AbortCause,
    Aborted,
    ContinuationTicket,
    Failed,
    FailureCategory,
    InvocationOutcome,
    Ok,
    OverlappingRun,
    PreparedClosed,
    RunIntent,
    RunRequest,
    StaleContinuation,
    Unbind,
    UnsupportedRequest,
)
from chrys.orchestration.invoker.evidence import InvocationEvidence
from chrys.orchestration.invoker.kernel import KernelConversation
from chrys.orchestration.invoker.origin import BoundEmitter
from chrys.orchestration.invoker.resources import Conversation, PassResources, PreparedAgent
from chrys.orchestration.workflows.agent_archive import CoalescedCheckpoint
from chrys.orchestration.workflows.agent_node_build import (
    AcpNodeParts,
    AgentNodeCallbacks,
    AgentNodeResources,
    KernelNodeParts,
    build_acp_node,
    build_kernel_node,
)
from chrys.service.agent_middleware.events.hosted_tools import FinalSegment
from chrys.service.session.history import SessionHistoryManager, stamp_history_item_ids
from chrys.service.session.sub_agent_logs import SubAgentLogStats
from chrys.service.tools.background import TaskScope
from chrys.service.workflows.scheduler import ErrorClass, FailureReport
from chrys.service.workflows.store import WorkflowStorageFailed

if TYPE_CHECKING:
    from chrys.foundation.models.invocations import ContentStatus
    from chrys.kernel import ChatResponse, Content
    from chrys.orchestration.workflows.agent_archive import AgentNodeArchive
    from chrys.service.approval.policy import ApprovalMode
    from chrys.service.workflows.admission import AgentBinding

logger = logging.getLogger(__name__)

# The shell whose pass the current task runs under; every task the kernel starts for a pass inherits it.
_PASS_SHELL: ContextVar[WorkflowAgentShell | None] = ContextVar("chrys.workflow.pass_shell", default=None)

ABORT_GRACE: Final = 10.0
"""Seconds a timed-out pass may take to converge after its abort before the run task is cancelled."""
_TERMINAL_CAUSES: Final = frozenset({AbortCause.USER_CANCEL, AbortCause.RUN_TERMINAL, AbortCause.OWNER_CLOSE})
# ACP failure categories a new session can overturn; the rest end the same way again.
_RETRYABLE_ACP_CATEGORIES: Final = frozenset({FailureCategory.TRANSPORT, FailureCategory.REMOTE_ERROR})

AttemptKind = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class AgentAttemptResult:
    """How one pass ended: ``completed`` with the response text, ``failed`` with a report, or ``cancelled``."""

    kind: AttemptKind
    text: str = ""
    failure: FailureReport | None = None


class WorkflowAgentShell:
    """One agent activation: a private conversation, one pass per attempt, and the caller's abort latch.

    The shell is both the conversation's operation binding (``request_close`` /
    ``drained``) and the kernel's pass observer; both interfaces are small and
    keep the cancel story in one place.
    """

    def __init__(
        self,
        *,
        binding: AgentBinding,
        node_id: str,
        invocation_id: str,
        resources: AgentNodeResources,
        archive: AgentNodeArchive,
    ) -> None:
        self._binding = binding
        self._node_id = node_id
        self._invocation_id = invocation_id
        self._resources = resources
        self._archive = archive
        self._archive_attempt = 1
        self._checkpoint: CoalescedCheckpoint | None = None
        self._stats = SubAgentLogStats()
        # The kernel pass's only publisher of model text, and how its open segment ends.
        self._projector: ContentProjector | None = None
        self._pass_status: ContentStatus = "completed"
        self._acp_translators: list[AcpUpdateTranslator] = []
        self._acp_counters = AcpInvocationCounters()
        self._display_name = binding.agent.display_name or binding.agent.name
        self.origin = InvocationOrigin("workflow_node", resources.session_id, invocation_id, None, attempt=1)
        """The origin of the current scheduler attempt; every pass and fact of that attempt carries it."""
        self._emitter = BoundEmitter(resources.bus, self.origin)
        self._prepared = PreparedAgent()
        self._unbind: Unbind | None = None
        self._parts: KernelNodeParts | AcpNodeParts | None = None
        self._is_prepared = False
        self._abort_cause: AbortCause | None = None
        self._run_task: asyncio.Task[InvocationOutcome] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._ticket: ContinuationTicket | None = None
        self._resume = False
        """Whether the last pass left kernel history for the next attempt to continue."""
        self._withdrawn: tuple[str, ...] = ()
        """The segments of the answer the next attempt's retry input dropped, for its views to withdraw."""
        self._evidence = InvocationEvidence(invocation_id)
        self._passes = 0
        self._usage_total = 0
        self._seed_item_id = new_analytics_id()
        self._prompt = ""
        self._active_run_input: list[Any] = []
        self._pass_start_index = 0

    # ------------------------------------------------------------------ identity

    @property
    def is_prepared(self) -> bool:
        """Whether this activation opened, even if its backend retired before retry."""
        return self._is_prepared

    @property
    def _backend(self) -> KernelConversation | AcpConversation | None:
        return self._parts.backend if self._parts is not None else None

    @property
    def invocation_id(self) -> str:
        return self._invocation_id

    def abort_cause(self) -> AbortCause | None:
        """The latched caller cause; also the kernel observer's abort-cause port."""
        return self._abort_cause

    @property
    def evidence(self) -> InvocationEvidence:
        """Every converged pass of this activation, accumulated."""
        return self._evidence

    # ------------------------------------------------------------------ lifecycle

    async def open(self, prompt: str, *, attempt: int = 1) -> None:
        """Build the conversation, bind this shell as its operation, and announce the invocation."""
        self._bind_attempt(attempt)
        await self._open_backend()
        self._is_prepared = True
        await self._emitter.publish(
            InvocationStarted(
                origin=self.origin,
                agent_name=self._display_name,
                tool_name=self._node_id,
                session_id=self._session_id,
                opening_prompt=self._user_prompt(prompt),
            )
        )

    def _bind_attempt(self, attempt: int) -> None:
        """Publish everything from here on as *attempt*; the previous attempt's pass has drained."""
        if attempt != self.origin.attempt:
            self.origin = replace(self.origin, attempt=attempt)
            self._emitter = BoundEmitter(self._resources.bus, self.origin)

    async def _open_backend(self) -> None:
        conversation = await self._prepared.open(self._open_conversation)
        self._unbind = conversation.bind_operation(self)

    async def _reopen_backend(self) -> FailureReport | None:
        """Open a backend for a retry after the last one was retired; a failure fails only this attempt."""
        try:
            await self._open_backend()
        except WorkflowStorageFailed:
            raise
        except Exception as exc:
            logger.warning("workflow node %s: agent could not reopen", self._node_id, exc_info=True)
            # A partial open may have left parts over a rolled-back conversation; the next attempt reopens.
            await self._retire_backend()
            return FailureReport(ErrorClass.AGENT_NON_TRANSIENT, f"{type(exc).__name__}: {exc}")
        return None

    async def _retire_backend(self) -> None:
        """End a backend lifetime without ending the activation or its accounting.

        A teardown failure is only logged: the attempt's own result stands, and the next attempt opens anew.
        """
        unbind, self._unbind = self._unbind, None
        if unbind is not None:
            unbind()
        try:
            await self._prepared.aclose()
        except Exception:
            logger.warning("workflow node %s: retired agent did not close cleanly", self._node_id, exc_info=True)
        finally:
            self._prepared = PreparedAgent()
            self._parts = None
            self._ticket = None
            self._resume = False

    async def close(self) -> None:
        """Release the conversation, then drain the session's usage tail (including other publishers).

        Safe to call more than once. The shared drain keeps session persistence behind usage delivery.
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="chrys.workflow.agent.close")
        await finish_close(self._close_task)

    async def _close(self) -> None:
        _PASS_SHELL.set(self)
        unbind, self._unbind = self._unbind, None
        if unbind is not None:
            unbind()
        await self._flush_progress()
        try:
            await self._prepared.aclose()
            if self._checkpoint is not None:
                await self._checkpoint.close()
        finally:
            await self._resources.usage_publisher.drain()

    async def _flush_progress(self) -> None:
        if isinstance(self._parts, KernelNodeParts):
            try:
                await self._parts.events.flush_progress()
            except Exception:
                logger.debug("workflow node %s: progress flush failed", self._node_id, exc_info=True)

    async def run(
        self,
        prompt: str,
        *,
        timeout: float | None,
        trajectory_context: TrajectoryContext | None,
        attempt: int = 1,
    ) -> AgentAttemptResult:
        """Archive this attempt after its pass has drained, including cancellation."""
        self._bind_attempt(attempt)
        self._checkpoint = CoalescedCheckpoint(self._save_transcript)
        self._archive_attempt = attempt
        self._acp_translators.clear()
        result: AgentAttemptResult | None = None
        error = ""
        try:
            if self._backend is None and (failure := await self._reopen_backend()) is not None:
                result = AgentAttemptResult("failed", failure=failure)
                return result
            result = await self._run(prompt, timeout=timeout, trajectory_context=trajectory_context)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await finish_close(asyncio.create_task(self._finish_attempt(result, error)))

    async def _finish_attempt(self, result: AgentAttemptResult | None, error: str) -> None:
        # Delivery belongs to the pass boundary, even while the activation is
        # retained for outgoing-condition evaluation and possible retries.
        _PASS_SHELL.set(self)
        # The attempt's background commands end with it; a sleep of the next
        # attempt, which shares their scope, says so.
        if (tasks := self._resources.background_tasks) is not None:
            await tasks.wait_stopped(tasks.stop_scope(TaskScope.of(self.origin)))
        await self._flush_progress()
        await self._resources.usage_publisher.drain()
        await self._require_checkpoint().close()
        await self._save_transcript(
            status=result.kind if result is not None else "failed" if error else "cancelled",
            error=result.failure.message if result is not None and result.failure is not None else error,
        )
        if (
            isinstance(self._backend, AcpConversation)
            and result is not None
            and result.failure is not None
            and result.failure.error_class is ErrorClass.AGENT_TIMEOUT
        ):
            await self._retire_backend()

    async def _run(
        self, prompt: str, *, timeout: float | None, trajectory_context: TrajectoryContext | None
    ) -> AgentAttemptResult:
        """Execute one pass for *prompt*; a retry pass resumes what the failed pass left when it can."""
        backend = self._backend
        if backend is None:
            raise RuntimeError("WorkflowAgentShell.run before open")
        if self._run_task is not None and not self._run_task.done():
            raise RuntimeError("an agent pass is already running for this activation")
        if self._abort_cause is not None:
            return AgentAttemptResult("cancelled")
        self._prompt = prompt
        if trajectory_context is not None:
            trajectory_context = trajectory_context.with_actor(
                workflow_node_actor(self._resources.session_id, self._invocation_id)
            )
        if isinstance(self._parts, KernelNodeParts):
            self._parts.trace.context = trajectory_context
        if isinstance(backend, AcpConversation):
            backend.bind_trajectory(
                trajectory_context,
                boundary_operation_id=trajectory_context.innermost_model_operation_id
                if trajectory_context is not None
                else None,
            )
        if trajectory_context is not None and isinstance(self._parts, KernelNodeParts):
            self._parts.run_kwargs["client_kwargs"] = {
                **self._parts.run_kwargs.get("client_kwargs", {}),
                TRAJECTORY_CONTEXT_KWARG: trajectory_context,
            }
        if isinstance(self._parts, KernelNodeParts):
            # Once per run of the node, as its other reminders: a later attempt keeps their text, so the opener its
            # kept work follows renders as first sent. Before the deadline starts, each under its own timeout: a
            # deadline that cancelled them would cancel the run, not time the node out. The pass task inherits the
            # turn they fill.
            for warning in await self._parts.reminder.refresh_extension_context(prompt, keep=True):
                await self._resources.bus.publish(warning)
            if self._abort_cause is not None:
                return AgentAttemptResult("cancelled")
        # The pass's begin() withdraws what the retry input dropped, once its views carry the transcript on.
        request, self._withdrawn = self._next_request(backend)
        self._passes += 1
        run_task = asyncio.create_task(self._pass(backend, request))
        self._run_task = run_task
        timed_out = False
        try:
            if timeout is None:
                outcome = await run_task
            else:
                done, _pending = await asyncio.wait({run_task}, timeout=timeout)
                if run_task not in done:
                    timed_out = True
                    await self.abort(AbortCause.CALLER_TIMEOUT)
                    done, _pending = await asyncio.wait({run_task}, timeout=ABORT_GRACE)
                    if run_task not in done:
                        run_task.cancel()
                outcome = await run_task
        except asyncio.CancelledError:
            # The runner's attempt task was cancelled around us: converge the pass before leaving.
            if self._abort_cause is None:
                self._abort_cause = AbortCause.RUN_TERMINAL
            backend.latch_abort(self._abort_cause)
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            raise
        except (StaleContinuation, UnsupportedRequest, OverlappingRun, PreparedClosed) as exc:
            self._ticket = None
            return AgentAttemptResult(
                "failed", failure=FailureReport(ErrorClass.AGENT_NON_TRANSIENT, f"{type(exc).__name__}: {exc}")
            )
        finally:
            if timed_out and self._abort_cause is AbortCause.CALLER_TIMEOUT:
                self._abort_cause = None  # a timeout ends the pass, not the activation
        return await self._project(outcome, timeout=timeout)

    async def _pass(self, backend: KernelConversation | AcpConversation, request: RunRequest) -> InvocationOutcome:
        """Run the pass marked as this shell's: the kernel's tasks for it inherit the mark with the context."""
        _PASS_SHELL.set(self)
        return await backend.run(request)

    def holds(self, task: asyncio.Task[Any] | None) -> bool:
        """True when *task* is the live pass or one started under it: the run waits for all of them.

        The kernel runs the pass's attempt and each of its tool calls on tasks of its own, and the usage
        callback schedules the progress publishes on others; an event published from any of them reaches
        its subscribers on that task.
        """
        if task is None:
            return False
        return task is self._run_task or task.get_context().get(_PASS_SHELL) is self

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        """Apply the engine's launch policy to the live pass's subsequent tool calls, sub-agents included."""
        if isinstance(self._parts, KernelNodeParts):
            self._parts.approval.set_approval_mode(mode)
            if self._parts.sub_agent_tools is not None:
                self._parts.sub_agent_tools.set_approval_mode(mode)

    def _latch(self, cause: AbortCause) -> None:
        if self._abort_cause is None or self._abort_cause not in _TERMINAL_CAUSES:
            self._abort_cause = cause

    async def abort(self, cause: AbortCause) -> None:
        """Latch *cause* and converge the in-flight pass, if any, tearing its live sub-agents down first."""
        self._latch(cause)
        backend = self._backend
        if backend is None:
            return
        if isinstance(self._parts, KernelNodeParts) and self._parts.sub_agent_tools is not None:
            # Running children cancel their attempt, so long inner tool calls stop before the pass
            # task itself is cancelled.
            await self._parts.sub_agent_tools.cascade_abort_all()
        backend.latch_abort(cause)
        handle = backend.active_handle
        if handle is not None:
            await backend.abort(handle, cause)

    # ------------------------------------------------------------------ OperationBinding

    def request_close(self, cause: AbortCause) -> None:
        self._latch(cause)
        if self._backend is not None:
            self._backend.latch_abort(cause)
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()

    @property
    def drained(self) -> Awaitable[None]:
        return self._drain()

    async def _drain(self) -> None:
        task = self._run_task
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

    # ------------------------------------------------------------------ KernelPassObserver

    async def begin(self, request: RunRequest, resources: PassResources) -> None:
        parts = self._require_kernel_parts()
        withdrawn, self._withdrawn = self._withdrawn, ()
        if request.intent is not RunIntent.FRESH and (not request.messages or self._has_live_continuation_token()):
            # An admitted pass that continues retained work, or polls the response an earlier pass left
            # running, carries the previous attempt's transcript on; the first fact of the attempt, so a
            # presentation starts from that transcript. Delivered whole with the withdrawal of the answer
            # its retry input dropped: a timeout may cancel this pass, and a later attempt carries on
            # from what the views hold by then. Any other pass starts its views anew, with nothing to withdraw.
            await finish_close(asyncio.create_task(self._resume_transcript(request.origin, withdrawn)))
        # Bound before the start hooks, which open the pass's hosted presentation under this origin.
        parts.approval.bind_publisher(self._emitter)
        parts.events.bind_origin(request.origin)
        self._projector = ContentProjector(self._emitter, agent_name=self._display_name)
        parts.events.bind_text_segments(self._projector.end_segment)
        self._pass_status = "completed"
        self._pass_start_index = len(parts.history.messages())
        resources.begin()
        self._active_run_input = list(request.messages)
        await self._save_transcript()

    async def succeeded(self, response: AgentResponse[Any], /) -> None:
        if isinstance(self._parts, KernelNodeParts):
            await self._parts.events.reconcile_hosted_response(response.messages)

    async def failed(self, error: Exception, /) -> None:
        self._pass_status = "failed"
        parts = self._require_kernel_parts()
        parts.history.repair_after_failure(self._active_run_input, self._pass_start_index, self._take_unlanded_answer())
        if self._abort_cause is None:
            # An aborted pass can still end in an error; finished() settles its cards as interrupted.
            await parts.events.reject_hosted_attempt(clean_error_message(error), status=HostedToolStatus.FAILED)

    async def finished(self) -> None:
        if isinstance(self._parts, KernelNodeParts) and (
            self._abort_cause is not None or self._pass_status == "interrupted"
        ):
            # A cut-off pass saved no history: keep its completed tool work and the answer it showed, so the
            # archive shows them and a resumed attempt continues after the work instead of running it again.
            self._parts.history.repair_after_failure(
                self._active_run_input, self._pass_start_index, self._take_unlanded_answer()
            )
        # The pass's open segment ends with it, keeping the text it showed.
        if self._projector is not None:
            await self._projector.finish("interrupted" if self._abort_cause is not None else self._pass_status)
        if isinstance(self._parts, KernelNodeParts) and self._abort_cause is not None:
            # An aborted pass settles no hosted card; a resumed attempt would carry it on still running.
            await self._parts.events.reject_hosted_attempt("Execution interrupted")

    async def interrupt(self) -> None:
        pass

    def cancelled(self) -> None:
        self._pass_status = "interrupted"

    async def response_ready(self, response: ChatResponse, final: bool) -> None:
        """The kernel landed a response: the pass projector shows what it did not stream."""
        del final
        if self._projector is not None:
            shown = self._parts.events.hosted_text_shown if isinstance(self._parts, KernelNodeParts) else None
            await self._projector.response_ready(response, shown=shown)

    def _take_unlanded_answer(self) -> tuple[Content, ...]:
        return self._projector.take_unlanded_answer() if self._projector is not None else ()

    # ------------------------------------------------------------------ construction

    async def _open_conversation(self, conversation: Conversation) -> Conversation:
        callbacks = AgentNodeCallbacks(
            observer=self,
            content=lambda: self._projector,
            response_ready=self.response_ready,
            checkpoint=self._mark_dirty,
            usage=self._on_usage,
            side_call_usage=self._on_side_call_usage,
            wire_retry=self._publish_wire_retry,
            service_retry=self._publish_service_retry,
            interruptible_sleep=self._interruptible_sleep,
            emitter=lambda: self._emitter,
            acp_usage=self._on_acp_usage,
            adopt_translator=self._adopt_acp_translator,
            acp_counters=self._acp_counters,
        )
        if self._binding.agent.acp is not None:
            self._parts = build_acp_node(
                conversation,
                binding=self._binding,
                node_id=self._node_id,
                invocation_id=self._invocation_id,
                res=self._resources,
                archive=self._archive,
                callbacks=callbacks,
            )
        else:
            self._parts = await build_kernel_node(
                conversation,
                binding=self._binding,
                node_id=self._node_id,
                invocation_id=self._invocation_id,
                res=self._resources,
                archive=self._archive,
                callbacks=callbacks,
                stats=self._stats,
            )
        # Construction awaits sub-agent registration, MCP connection and skill discovery; a mode
        # change that arrived meanwhile found no parts to apply to, so the launch policy is re-read.
        self.set_approval_mode(self._resources.approval_mode())
        return conversation

    def _on_acp_usage(
        self,
        total: int,
        input_tokens: int,
        output_tokens: int,
        local_tokens: int,
        calibration_ratio: float,
        system_overhead_tokens: int,
        cache_hit_tokens: int | None,
        max_context_tokens: int | None,
        agent_profile: str,
        usage_source_id: str,
        total_usage_tokens: int,
    ) -> None:
        # ACP reports each transport once; the archive and graph summarize the whole activation.
        self._usage_total += total_usage_tokens
        self._stats.record_usage(total, self._usage_total)
        self._resources.usage_publisher.accumulate_invocation_usage(
            total,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            local_tokens=local_tokens,
            calibration_ratio=calibration_ratio,
            system_overhead_tokens=system_overhead_tokens,
            cache_hit_tokens=cache_hit_tokens,
            max_context_tokens=max_context_tokens,
            agent_profile=agent_profile,
            usage_source_id=usage_source_id,
            authoritative_total=total_usage_tokens,
        )

    async def _resume_transcript(self, origin: InvocationOrigin, withdrawn: tuple[str, ...]) -> None:
        await self._emitter.publish(
            InvocationResumed(origin=origin, agent_name=self._display_name, session_id=self._session_id)
        )
        if withdrawn:
            await self._emitter.publish(
                InvocationAnswerWithdrawn(
                    origin=origin, agent_name=self._display_name, session_id=self._session_id, segment_ids=withdrawn
                )
            )

    # ------------------------------------------------------------------ passes

    def _next_request(self, backend: KernelConversation | AcpConversation) -> tuple[RunRequest, tuple[str, ...]]:
        """Resume what the last pass left: its live ticket, its repaired history, or nothing after a success.

        Also names the segments of the answer the retry input dropped from history to ask for it again.
        """
        ticket, self._ticket = self._ticket, None
        if isinstance(backend, AcpConversation):
            # Every ACP pass runs a new session from the prompt; a ticket only names the failed one.
            intent = RunIntent.RETRY if ticket is not None else RunIntent.FRESH
            return RunRequest(self._seed_input(), intent, self.origin, ticket), ()
        history = self._require_kernel_parts().history
        if ticket is not None and backend.continuation_is_live(ticket, self.origin):
            messages, withdrawn = history.retry_input(self._seed_input)
            return RunRequest(messages, RunIntent.RETRY, self.origin, ticket), withdrawn
        if self._resume:
            messages, withdrawn = history.retry_input(self._seed_input)
            return RunRequest(messages, RunIntent.CONTINUE, self.origin, None), withdrawn
        # A completed body is run again from the prompt when a later phase of its attempt failed.
        self._drop_continuation_token()
        if self._passes:
            backend.history_state = {}  # a fresh pass starts from the prompt, not from a stale transcript
        return RunRequest(self._seed_input(), RunIntent.FRESH, self.origin, None), ()

    async def _project(self, outcome: InvocationOutcome, *, timeout: float | None) -> AgentAttemptResult:
        self._evidence = self._evidence.add(outcome.effects)
        self._resume = False
        if isinstance(outcome, Ok):
            self._ticket = None
            if isinstance(outcome.backend_payload, AgentResponse):
                # The node's value is the final segment, as a sub-agent's result is; what the agent wrote
                # between tool calls is already in the transcript.
                segment = FinalSegment.of(outcome.backend_payload)
                value, transcript = segment.result, segment.transcript
            else:
                # An ACP result is left alone, since the profile's result mode already chose its extent.
                value = transcript = _response_text(outcome)
                if isinstance(self._backend, AcpConversation) and self._backend.transcript_final_text is not None:
                    transcript = self._backend.transcript_final_text
            await self._emitter.publish(
                InvocationMessage(
                    origin=self.origin,
                    agent_name=self._display_name,
                    text=transcript,
                    is_final=True,
                    session_id=self._session_id,
                )
            )
            return AgentAttemptResult("completed", text=value)
        if isinstance(outcome, Failed):
            self._ticket = outcome.continuation
            self._resume = isinstance(self._parts, KernelNodeParts)
            transient = self._retryable(outcome)
            approved = transient and not self._repeats_hosted_work(outcome)
            error_class = ErrorClass.AGENT_TRANSIENT if transient else ErrorClass.AGENT_NON_TRANSIENT
            return AgentAttemptResult(
                "failed", failure=FailureReport(error_class, outcome.error, backend_approved=approved)
            )
        if not isinstance(outcome, Aborted):
            raise TypeError("Expected an aborted agent outcome after handling success and failure.")
        self._ticket = None
        # An aborted request is abandoned, never polled again: its retry creates a new one. A response
        # still running at the provider may yet run hosted tools that the new request would repeat.
        abandoned_response = self._has_live_continuation_token()
        self._drop_continuation_token()
        if outcome.cause is AbortCause.CALLER_TIMEOUT:
            # The pass's end repaired what the deadline cut off: the next attempt continues from it.
            self._resume = isinstance(self._parts, KernelNodeParts)
            limit = f"{timeout:g}s" if timeout is not None else "its deadline"
            return AgentAttemptResult(
                "failed",
                failure=FailureReport(
                    ErrorClass.AGENT_TIMEOUT,
                    f"no response within {limit}",
                    backend_approved=not abandoned_response and not self._repeats_hosted_work(outcome),
                ),
            )
        return AgentAttemptResult("cancelled")

    def _retryable(self, outcome: Failed) -> bool:
        """Whether repeating the failed pass can succeed: a clear refusal, quota or configuration error cannot."""
        if outcome.category is not FailureCategory.UNCLASSIFIED:
            return outcome.category in _RETRYABLE_ACP_CATEGORIES
        exception = outcome.exception
        if exception is None:
            return False
        # A stall already outlasted the in-pass retries; a later attempt may find the stream moving again.
        return is_retryable(exception) or classify_error(exception).kind is ErrorKind.STREAM_STALLED

    def _repeats_hosted_work(self, outcome: Failed | Aborted) -> bool:
        """Whether resuming would run provider-hosted tool calls a second time.

        Resumed history keeps every completed exchange, so only the last request's hosted work is at risk
        — named by the middleware's probe unless the repaired history kept the response that ran it. Under
        service-side storage the history may hold none of the pass, so any hosted work of the pass counts. A
        live continuation token makes the retry a poll of the response already created.
        """
        parts = self._parts
        if not isinstance(parts, KernelNodeParts) or self._has_live_continuation_token():
            return False
        if parts.backend.service_session_storage_enabled:
            return outcome.effects.hosted_observed.observed > 0
        return bool(parts.hosted_unkept(parts.history.messages()[self._pass_start_index :]))

    def _has_live_continuation_token(self) -> bool:
        return isinstance(self._parts, KernelNodeParts) and has_live_continuation_token(self._parts.run_kwargs)

    def _drop_continuation_token(self) -> None:
        if isinstance(self._parts, KernelNodeParts):
            drop_continuation_token(self._parts.run_kwargs)

    async def _interruptible_sleep(self, seconds: int) -> bool:
        for _ in range(max(0, seconds)):
            if self._abort_cause is not None:
                return True
            await asyncio.sleep(1)
        return self._abort_cause is not None

    # ------------------------------------------------------------------ history

    def _require_checkpoint(self) -> CoalescedCheckpoint:
        if self._checkpoint is None:
            raise RuntimeError("The agent node checkpoint has not been opened.")
        return self._checkpoint

    def _require_kernel_parts(self) -> KernelNodeParts:
        if not isinstance(self._parts, KernelNodeParts):
            raise TypeError("A kernel agent pass requires kernel node resources.")
        return self._parts

    async def _mark_dirty(self) -> None:
        self._require_checkpoint().changed()

    async def _adopt_acp_translator(self, translator: AcpUpdateTranslator) -> None:
        self._acp_translators.append(translator)
        if self._checkpoint is not None:
            translator.on_audit_changed = self._checkpoint.changed
        await self._save_transcript()

    async def _save_transcript(self, *, status: str = "running", error: str = "") -> None:
        state = None
        acp_state = None
        if isinstance(self._backend, AcpConversation):
            acp_state = {
                "prompt": self._user_prompt(self._prompt),
                "translated_updates": [
                    {**item, "attempt": translator.attempt}
                    for translator in self._acp_translators
                    for item in translator.translated_updates
                ],
                "successful_attempt": self._backend.transport_ordinal if status == "completed" else None,
            }
            self._stats.tool_call_count = max(
                (self._backend.completed_tool_calls, *(t.completed_count for t in self._acp_translators))
            )
            self._stats.usage_unreported_attempts = self._backend.usage_unreported_attempts
        elif isinstance(self._parts, KernelNodeParts):
            # Keep message identities while merging into a detached list. This
            # reuses the same recovery merge as Chat without mutating the
            # active model history or deduplicating repeated provider call IDs.
            state = dict(self._parts.history.state())
            state["messages"] = list(state.get("messages", []))
            input_message = active_input_message(self._active_run_input)
            if not state["messages"] and input_message is not None:
                state["messages"].append(input_message)
            manager = SessionHistoryManager()
            manager.bind(state)
            manager.merge_loop_messages(self._parts.history.recorder, insert_index=self._pass_start_index)
            stamp_history_item_ids(state)
        await self._archive.write(
            attempt=self._archive_attempt,
            status=status,
            error=error,
            state=state,
            acp_state=acp_state,
            stats=self._stats,
        )
        if status != "running" and isinstance(self._parts, KernelNodeParts) and self._parts.sub_agent_tools is not None:
            # Children's control records go once the attempt's terminal transcript is on disk, as after
            # a chat turn's save; a running checkpoint is best-effort and may predate a child's result.
            self._parts.sub_agent_tools.finalize_pending_cleanups()

    def _user_prompt(self, prompt: str) -> str:
        """The same user text feeds the conversation and its live presentation."""
        if self._binding.agent.acp is not None and self._binding.instructions_suffix:
            return f"{prompt}\n\n{self._binding.instructions_suffix}"
        return prompt

    def _seed_input(self) -> list[Any]:
        message = Message("user", [self._user_prompt(self._prompt)])
        ensure_analytics_item_id(message.additional_properties, item_id=self._seed_item_id)
        return [message]

    # ------------------------------------------------------------------ events

    @property
    def _session_id(self) -> str | None:
        return self._resources.session_id or None

    async def _publish_wire_retry(
        self, message: str, attempt: int, max_attempts: int, delay_seconds: int, exc: BaseException
    ) -> None:
        await self._publish_retry(message, attempt, max_attempts, delay_seconds, exc, scope="wire")

    async def _publish_service_retry(
        self, message: str, attempt: int, max_attempts: int, delay_seconds: int, exc: BaseException
    ) -> None:
        await self._publish_retry(message, attempt, max_attempts, delay_seconds, exc, scope="run")

    async def _publish_retry(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
        *,
        scope: Literal["wire", "run"],
    ) -> None:
        emitter = self._emitter
        if isinstance(self._parts, KernelNodeParts) and not self._has_live_continuation_token():
            # The retried request's hosted cards will not finish; a poll of a created response still may.
            await self._parts.events.reject_hosted_attempt(message)
        display_message, display_hint = display_fields(exc, retry_notice=True)
        await emitter.publish(
            InvocationRetryAttempt(
                scope=scope,
                origin=emitter.origin,
                agent_name=self._display_name,
                message=message,
                attempt=attempt,
                max_attempts=max_attempts,
                delay_seconds=delay_seconds,
                session_id=self._session_id,
                display_message=display_message,
                display_hint=display_hint,
            )
        )

    def _on_side_call_usage(self, usage_details: Mapping[str, Any]) -> None:
        """Charge summary generation to this node without replacing its context-window reading."""
        self._resources.usage_publisher.accumulate_side_call_usage(
            usage_details,
            agent_profile=self._binding.agent.name,
            usage_source_id=f"{self._invocation_id}:last_words",
            max_context_tokens=self._binding.model.max_context_tokens if self._binding.model is not None else None,
        )
        self._usage_total += int(usage_details.get("total_token_count") or 0)
        if isinstance(self._parts, KernelNodeParts):
            self._parts.events.record_usage(self._stats.total_tokens, self._usage_total)

    # Positional args mirror UsageTrackingMiddleware._fire_callback. New fields must be named and
    # forwarded here as well: *_rest tolerates extra arguments but does not account for their values.
    def _on_usage(
        self,
        total: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        local_tokens: int = 0,
        calibration_ratio: float = 1.0,
        system_overhead_tokens: int = 0,
        cache_hit_tokens: int | None = None,
        _calibration_initialized: bool = False,
        use_local_context_estimate: bool = False,
        *_rest: Any,
    ) -> None:
        self._usage_total += total
        if isinstance(self._parts, KernelNodeParts):
            self._parts.events.record_usage(total, self._usage_total)
        self._resources.usage_publisher.accumulate_invocation_usage(
            total,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            local_tokens=local_tokens,
            calibration_ratio=calibration_ratio,
            system_overhead_tokens=system_overhead_tokens,
            cache_hit_tokens=cache_hit_tokens,
            max_context_tokens=self._binding.model.max_context_tokens if self._binding.model is not None else None,
            agent_profile=self._binding.agent.name,
            usage_source_id=self._invocation_id,
            use_local_context_estimate=use_local_context_estimate,
        )


def _response_text(outcome: Ok) -> str:
    return "".join(
        segment.text for segment in outcome.segments if segment.type == "text" and isinstance(segment.text, str)
    ).strip()
