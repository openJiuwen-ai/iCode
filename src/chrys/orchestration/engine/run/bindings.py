# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main Turn middleware, presentation callbacks, and backend construction."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from chrys.foundation.errors import clean_error_message
from chrys.foundation.errors.display import display_fields
from chrys.foundation.events.types import (
    AgentThinking,
    Error,
    InvocationMessage,
    InvocationRetryAttempt,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
)
from chrys.foundation.hosted_tools import HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY, HostedToolStatus
from chrys.foundation.retry import (
    TRANSIENT_RETRY_BACKOFF_SECONDS,
    StreamStall,
)
from chrys.foundation.trajectory.envelope import Link, LinkRelation
from chrys.foundation.util.time import parse_created_at
from chrys.kernel import (
    RESPONSE_READY_KWARG,
    Agent,
    AgentSession,
    ConsumedInjectionMessageProbe,
    LoopRecorderSnapshot,
    StallExhaustedAction,
    WireRetryPolicy,
    resolve_storage_mode_and_handles,
)
from chrys.orchestration.engine.run.resume import TurnPassState, TurnResumePolicy
from chrys.orchestration.invoker.attempts import (
    AgentRunKwargs,
    AttemptRecipe,
    AttemptRunner,
    AttemptTaskHandle,
    BlockingCallTiming,
    HistoryRollback,
    ModelRunTrace,
    RestoreAndFallback,
    RetryBoundaryPolicy,
    WireRecipe,
    continuation_token_observer_for,
    is_string_keyed_dict,
)
from chrys.orchestration.invoker.content_projection import ContentProjector
from chrys.orchestration.invoker.contracts import (
    AbortCause,
    FailureDisposition,
    InvocationOutcome,
    RunIntent,
    RunRequest,
)
from chrys.orchestration.invoker.kernel import KernelConversation
from chrys.orchestration.invoker.origin import BoundEmitter, InvocationPublishers
from chrys.orchestration.invoker.resources import Conversation, PassResources
from chrys.service.agent_middleware import (
    ApprovalMiddleware,
    AskUserMiddleware,
    InterruptMiddleware,
    ResponseBatchCounter,
    SleepMiddleware,
    ToolEventMiddleware,
)
from chrys.service.agent_middleware.control.approval import ApprovalRetrySnapshot
from chrys.service.agent_middleware.control.hook_commit import HookCommitMiddleware
from chrys.service.agent_middleware.events.hosted_tools import (
    FinalTextOp,
    HostedPresentationBridge,
    HostedToolArgsOp,
    HostedToolProgressOp,
    HostedToolResultOp,
    HostedToolStartOp,
    HostedToolStatusOp,
    PresentationSinkOperation,
    next_hosted_run_generation,
)
from chrys.service.agent_middleware.events.tool_events import ToolEventRetrySnapshot
from chrys.service.agent_middleware.response_validation import (
    ResponseValidationMiddleware,
)
from chrys.service.context.providers.history import PRE_OUTPUT_HISTORY_LEN_STATE_KEY
from chrys.service.session.message_metadata import (
    LAST_ASSISTANT_CREATED_AT_STATE_KEY,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.invocations import ContentStatus
    from chrys.foundation.trajectory.context import TrajectoryContext
    from chrys.kernel import AgentResponse, ChatResponse, LoopRecorder
    from chrys.service.agent_middleware.injection import InjectionMiddleware, QueuedInjection
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.compaction.strategy import CompactionRetrySnapshot
    from chrys.service.hooks.manager import HookManager
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.tools.background import BackgroundTasks
    from chrys.service.trajectory.preparation import PreparationTrace


logger = logging.getLogger(__name__)

_RETRY_WAIT_POLL_SECONDS = 1.0
"""Longest a retry wait goes without reading the interrupt latch."""


class _AssistantMessageEventKwargs(TypedDict, total=False):
    """Optional timestamp forwarded to ``InvocationMessage``."""

    timestamp: datetime


@dataclass(frozen=True, slots=True)
class _RetryWait:
    """The wait a retry notice announced: the user's skip or an interrupt ends it early."""

    wait_id: str
    woken: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _TurnRetryState:
    """Auxiliary per-run buffers rolled back with framework history."""

    loop_recorder: LoopRecorderSnapshot | None
    tool_events: ToolEventRetrySnapshot
    approval: ApprovalRetrySnapshot
    compaction: CompactionRetrySnapshot | None


class TurnBindings:
    """Compose main-pass callbacks and middleware; owns no execution facade.

    KernelConversation runs the pass; TurnRunner owns the user turn.
    Resource references here are borrowed from the installed PreparedAgent owner.
    """

    # Resilience: retry on transient network errors (proxy drops,
    # connection resets, timeouts).  Applies to both blocking and
    # streaming paths.  The classifier lives in :mod:`chrys.foundation.errors`
    # and the loop lives in :mod:`chrys.foundation.retry` so sub-agents share
    # the same policy without reaching back into this class.
    _MAX_RETRIES = 5
    _BACKOFF_SCHEDULE = TRANSIENT_RETRY_BACKOFF_SECONDS
    # Fallback when no per-run stream_attempt_timeout is supplied (e.g. tests).
    # Production callers pass ModelProfile.http_read_timeout so stall detection
    # matches the HTTP client's read timeout.
    _DEFAULT_STREAM_ATTEMPT_TIMEOUT = 300.0  # seconds
    # Class-level fallbacks so partially-constructed bindings (tests build
    # them via ``object.__new__``) read "no probe wired" instead of raising.
    _hosted_commits_probe: Callable[[], tuple[str, ...]] | None = None
    _hosted_commits_in_flight_probe: Callable[[], tuple[str, ...]] | None = None
    _max_retries_override: int | None = None
    _hosted_bridge: HostedPresentationBridge | None = None
    _projector: ContentProjector | None = None
    # The wait the last retry notice announced, until the sleep after it ends.
    _retry_wait: _RetryWait | None = None

    def __init__(
        self,
        *,
        conversation: Conversation,
        agent: Agent,
        session: AgentSession,
        event_bus: EventBus,
        approval_middleware: ApprovalMiddleware,
        ask_user_middleware: AskUserMiddleware,
        injection_middleware: InjectionMiddleware,
        loop_recorder: LoopRecorder | None = None,
        session_id: str | None = None,
        compaction_strategy: UnifiedContextStrategy | None = None,
        stream: bool = False,
        batch_counter: ResponseBatchCounter | None = None,
        chat_options: dict[str, Any] | None = None,
        mutation_tracker: MutationTracker | None = None,
        mutation_coordinator: MutationCoordinator | None = None,
        stream_attempt_timeout: float | None = None,
        max_transient_retries: int | None = None,
        hook_manager: HookManager | None = None,
        profile_name: str = "",
        workspace_cwd: str = "",
        serialize_implicit_windows: bool = False,
        run_cycle_start_hooks: Sequence[Callable[[], None]] = (),
        hosted_commits_probe: Callable[[], tuple[str, ...]] | None = None,
        hosted_commits_in_flight_probe: Callable[[], tuple[str, ...]] | None = None,
        response_validation_middleware: ResponseValidationMiddleware | None = None,
        invocation_publishers: InvocationPublishers | None = None,
        tool_result_ceiling_tokens: int | None = None,
        background_tasks: BackgroundTasks | None = None,
        shows_answer_text: bool = True,
    ) -> None:
        self.resource_scope = conversation
        self._agent = agent
        self._session = session
        self._bus = event_bus
        self.approval = approval_middleware
        self._ask_user = ask_user_middleware
        # A queued user message ends the main agent's sleep: the next model call delivers it.
        self._sleep = SleepMiddleware(
            event_bus,
            session_id=session_id,
            background_tasks=background_tasks,
            user_message_waiting=lambda: injection_middleware.has_pending,
        )
        self._injection = injection_middleware
        self._loop_recorder = loop_recorder
        self._session_id = session_id
        self._compaction_strategy = compaction_strategy
        self._stream = stream
        self._chat_options = chat_options
        self._stream_attempt_timeout = (
            stream_attempt_timeout if stream_attempt_timeout is not None else self._DEFAULT_STREAM_ATTEMPT_TIMEOUT
        )
        self._max_retries_override = max_transient_retries
        # Whether the frontend shows answer text as it streams; text nobody saw never stops a retry.
        self._shows_answer_text = shows_answer_text

        self._batch_counter = batch_counter
        self._response_validation = response_validation_middleware
        self._hosted_run_generation = 0
        self._hosted_bridge = None
        # The pass's only publisher of model text, made at pass start.
        self._projector = None
        self._interrupt = InterruptMiddleware()
        self._hook_commit = HookCommitMiddleware()
        # Fired once at the start of every user-initiated run cycle, before
        # the first attempt.  Components that carry state across the cycle's
        # whole-run retry attempts register here so an aborted cycle —
        # interrupt during outer backoff, unrelated exception — cannot leak
        # state into the next independent run.
        self._attempt_handle = AttemptTaskHandle()
        self.state = TurnPassState()
        # Validation-middleware probes for provider-hosted tool executions the
        # loop recorder cannot see (the rejected/aborted exchange never reaches
        # it): run-scoped for the whole-run retry gate, wire-attempt-scoped for
        # the kernel's per-wire replay veto.
        self._hosted_commits_probe = hosted_commits_probe
        self._hosted_commits_in_flight_probe = hosted_commits_in_flight_probe
        # Last continuation token announced by the kernel and not yet
        # resolved to a terminal response.  Run kwargs are rebuilt per run,
        # so without this carry-over a user retry after a gate-blocked poll
        # failure would CREATE a second background response while the
        # announced one keeps running remotely (duplicate hosted work).
        # Crash-recovery channel to the orchestration turn state, wired by the
        # engine after build (``TurnRuntimeState.set_current_input``).  The
        # bare-resume replay branch pops the anchor user message out of state
        # and must register it as recovery current input at pop time — the
        # component that destructively reads is the component that makes it
        # durable.
        # Trajectory recording for the next pass, set by the turn runner:
        # the ambient context every model run of the pass binds (None = not
        # recording) and the pre-minted item id of the message opening it.
        self._attempt_trace = ModelRunTrace(
            interrupted=lambda: self._interrupt.is_interrupted,
            service_side=lambda: self.backend.service_session_storage_enabled,
            committed=lambda: self._loop_recorder is not None and self._loop_recorder.committed_count > 0,
        )
        attempt_recipe = AttemptRecipe(
            stall_exhaustion=RestoreAndFallback(self._reject_stream),
            stall_error=lambda _timeout: StreamStall(0),
            retry_boundary=RetryBoundaryPolicy.SKIP,
            blocking_call_timing=BlockingCallTiming.IN_ATTEMPT_TASK,
            history_state=lambda: session.state.get("chrys_history", {}),
            before_attempt=None,
        )
        self._rollback = HistoryRollback(
            session,
            snapshot_caller=self._snapshot_retry_state,
            restore_caller=self._restore_retry_state,
            history_state=attempt_recipe.history_state,
        )
        self._attempts = AttemptRunner(
            agent=agent,
            session=session,
            handle=self._attempt_handle,
            rollback=self._rollback,
            retry_participant=self._injection,
            interrupt=self._interrupt,
            trace=self._attempt_trace,
            stream_observer=lambda: self._projector,
            content_exposed=lambda: self._shows_answer_text and self._projector is not None and self._projector.exposed,
            publish_retry=self._publish_retry_notice,
            interruptible_sleep=self._interruptible_sleep,
            max_retries=self._effective_max_retries,
            backoff_schedule=lambda: self._BACKOFF_SCHEDULE,
            stream_timeout=lambda: self._stream_attempt_timeout,
            committed_count=lambda: self._loop_recorder.committed_count if self._loop_recorder is not None else 0,
            hosted_commits=lambda: self._hosted_commits_probe() if self._hosted_commits_probe is not None else (),
            recipe=attempt_recipe,
        )

        self.backend = KernelConversation(
            owner=conversation,
            session=session,
            attempts=self._attempts,
            attempt_handle=self._attempt_handle,
            observer=self,
            run_kwargs=lambda: self._build_run_kwargs(
                None if self.backend.service_session_storage_enabled else self._build_wire_retry_policy()
            ),
            stream=lambda: self._stream,
            service_side=self._resolve_service_storage,
            compaction_strategy=compaction_strategy,
            recorder=loop_recorder,
            hosted_observed=hosted_commits_probe,
            start_hooks=(*run_cycle_start_hooks, self._begin_hosted_baseline),
            failure_disposition=FailureDisposition.CALLER_DECISION,
        )

        self.inputs = TurnResumePolicy(self.backend, session_id, self.state)
        self._invocation_publishers = invocation_publishers or InvocationPublishers(event_bus)
        self._bound_emitter: BoundEmitter | None = None
        self.tool_events = ToolEventMiddleware(
            event_bus,
            origin=self.inputs.origin,
            session_id=session_id,
            batch_counter=batch_counter,
            mutation_tracker=mutation_tracker,
            hook_manager=hook_manager,
            profile_name=profile_name,
            workspace_cwd=workspace_cwd,
            serialize_implicit_windows=serialize_implicit_windows,
            mutation_coordinator=mutation_coordinator,
            on_start_published=self._local_call_start_published,
            tool_result_ceiling_tokens=tool_result_ceiling_tokens,
        )

    @property
    def trajectory_context(self) -> TrajectoryContext | None:
        return self._attempt_trace.context

    @trajectory_context.setter
    def trajectory_context(self, context: TrajectoryContext | None) -> None:
        self._attempt_trace.context = context
        self._attempt_trace.first_run_links = (
            (Link(relation=LinkRelation.CAUSED_BY, target_operation_id=context.turn_preamble_operation_id),)
            if context is not None and context.turn_preamble_operation_id is not None
            else ()
        )

    def record_pre_run_interrupt(self) -> None:
        """Record a task-scoped interrupt that arrived before execution began."""
        self.state.was_interrupted = True
        self.state.run_failed = False
        self.state.last_error = ""
        # A cancelled Turn pass abandons any incomplete provider-side
        # response even when Stop prevents ``run()``/``resume()`` from
        # reaching their normal reset paths.
        self.inputs.pending_continuation_token = None
        self._interrupt.reset()

    def _begin_hosted_baseline(self) -> None:
        if self._response_validation is not None:
            self._response_validation.begin_pass_hosted_baseline(
                resumes_background_response=self.inputs.pending_continuation_token is not None
            )

    def _resolve_service_storage(self) -> bool:
        """Return whether the first request uses provider-side history."""
        try:
            stores_by_default = bool(self._agent.client.STORES_BY_DEFAULT)
        except AttributeError:
            stores_by_default = False
        try:
            force_stateless = bool(self._agent.client.FORCES_STATELESS)
        except AttributeError:
            force_stateless = False
        return resolve_storage_mode_and_handles(
            self._chat_options,
            stores_by_default=stores_by_default,
            force_stateless=force_stateless,
        ).service_side

    async def _local_call_start_published(self, provider_call_id: str) -> None:
        """Release hosted presentation ordered behind a local tool call."""
        if self._hosted_bridge is not None:
            await self._hosted_bridge.local_call_start_published(provider_call_id)

    @staticmethod
    def _artifact_descriptors(operation: HostedToolResultOp) -> list[dict[str, Any]]:
        """Build bounded JSON-safe descriptors for hosted result artifacts."""
        descriptors: list[dict[str, Any]] = []
        for artifact in operation.view.artifacts:
            # "path" must stay a real URI: consumers turn it into links, and a
            # bare hosted filename (OpenAI hosted_file) is not addressable.
            descriptor = {
                "id": artifact.file_id or artifact.vector_store_id or artifact.id or "",
                "name": artifact.name or "",
                "path": artifact.uri or "",
                "mime": artifact.media_type or "",
            }
            size = artifact.additional_properties.get("size")
            if isinstance(size, int) and not isinstance(size, bool):
                descriptor["size"] = size
            descriptors.append({key: value for key, value in descriptor.items() if value != ""})
        return descriptors

    async def _publish_hosted_operation(self, operation: PresentationSinkOperation, *, emitter: BoundEmitter) -> None:
        """Map one provider-neutral presentation operation onto EventBus events."""
        if isinstance(operation, FinalTextOp):
            await emitter.publish(
                InvocationMessage(
                    origin=emitter.origin,
                    text=operation.text,
                    is_final=True,
                    structured_output_completed=operation.structured_output_completed,
                    **self._assistant_message_event_kwargs(),
                    session_id=self._session_id,
                )
            )
            return

        view = operation.view
        if isinstance(operation, HostedToolStartOp):
            await emitter.publish(
                InvocationToolCallStart(
                    origin=emitter.origin,
                    tool_name=view.tool_name,
                    call_id=operation.presentation_id,
                    provider_hosted=True,
                    hosted_family=view.family,
                    provider=view.provider,
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
                    tool_kind=HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY.get(view.family, ""),
                    args=view.arguments,
                    session_id=self._session_id,
                )
            )

        elif isinstance(operation, HostedToolArgsOp):
            await emitter.publish(
                InvocationToolCallArgsUpdated(
                    origin=emitter.origin,
                    tool_name=view.tool_name,
                    call_id=operation.presentation_id,
                    provider_hosted=True,
                    hosted_family=view.family,
                    provider=view.provider,
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
                    tool_kind=HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY.get(view.family, ""),
                    args=view.arguments,
                    session_id=self._session_id,
                )
            )
        elif isinstance(operation, HostedToolProgressOp):
            await emitter.publish(
                InvocationToolCallProgress(
                    origin=emitter.origin,
                    tool_name=view.tool_name,
                    call_id=operation.presentation_id,
                    provider_hosted=True,
                    hosted_family=view.family,
                    provider=view.provider,
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
                    lines=view.result_text.splitlines(),
                    image_contents=view.image_contents,
                    snapshot_metadata=view.metadata,
                    session_id=self._session_id,
                )
            )
        elif isinstance(operation, HostedToolStatusOp):
            metadata = dict(view.metadata)
            if view.result_text:
                metadata["result_text"] = view.result_text
            if view.provider_item_type:
                metadata["provider_item_type"] = view.provider_item_type
            if view.provider_call_id:
                metadata["provider_call_id"] = view.provider_call_id
            await emitter.publish(
                InvocationToolCallStatusUpdated(
                    origin=emitter.origin,
                    tool_name=view.tool_name,
                    call_id=operation.presentation_id,
                    provider_hosted=True,
                    hosted_family=view.family,
                    provider=view.provider,
                    status=view.status,
                    provider_status=view.provider_status,
                    metadata=metadata,
                    session_id=self._session_id,
                )
            )
        elif isinstance(operation, HostedToolResultOp):
            await emitter.publish(
                InvocationToolCallResult(
                    origin=emitter.origin,
                    tool_name=view.tool_name,
                    call_id=operation.presentation_id,
                    provider_hosted=True,
                    hosted_family=view.family,
                    provider=view.provider,
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
                    result=view.result_text,
                    image_contents=view.image_contents,
                    metadata=view.metadata,
                    artifacts=self._artifact_descriptors(operation),
                    session_id=self._session_id,
                )
            )

    def _build_run_kwargs(self, wire_retry_policy: WireRetryPolicy | None = None) -> AgentRunKwargs:
        """Build common kwargs for agent.run()."""
        # ToolEventMiddleware owns hook dispatch too, so hook-modified args
        # are reflected consistently in UI events, mutation tracking, and
        # approval requests.
        # Per-run channel consumed by the chrys ToolLoopLayer (fresh dict per
        # call; the agent merges the session into the same mapping). A None
        # recorder is not injected — absent key and None mean the same thing
        # to the loop, and an absent key keeps the mapping honest.
        client_kwargs: dict[str, object] = {}
        if self._loop_recorder is not None:
            client_kwargs["loop_recorder"] = self._loop_recorder
        if wire_retry_policy is not None:
            client_kwargs["wire_retry_policy"] = wire_retry_policy
        client_kwargs[RESPONSE_READY_KWARG] = self._response_ready
        client_kwargs["consumed_injection_message_probe"] = ConsumedInjectionMessageProbe(
            drain_consumed_injection_messages=self._injection.drain_consumed_injection_messages,
            commit_consumed_injections=self._injection.commit_logical_call,
        )
        # Chain order is pinned (first = outermost): everything sits inside tool_events,
        # so a call a middleware answers itself (ask_user, a skipped sleep) still publishes
        # InvocationToolCallStart/InvocationToolCallResult. The hook commit is innermost:
        # nothing after it can change the arguments the before_tool_call hooks checked.
        kwargs: AgentRunKwargs = {
            "session": self._session,
            "middleware": [
                self.tool_events,
                self._ask_user,
                self.approval,
                self._sleep,
                self._interrupt,
                self._hook_commit,
            ],
            "client_kwargs": client_kwargs,
        }
        if self._compaction_strategy is not None:
            kwargs["compaction_strategy"] = self._compaction_strategy
            # The raw client needs the same tokenizer to estimate live tool
            # definitions before the first calibrated provider response.  The
            # strategy remains the single owner of the tokenizer instance.
            kwargs["tokenizer"] = self._compaction_strategy.tokenizer
        if self._chat_options:
            options_copy = dict(self._chat_options)
            if is_string_keyed_dict(extra_body := options_copy.get("extra_body")):
                options_copy["extra_body"] = dict(extra_body)
            kwargs["options"] = options_copy
        if self.inputs.pending_continuation_token is not None:
            # A previous run of this turn left a background response in
            # flight (poll failed, gate blocked the auto-retry).  Resume it
            # instead of creating a duplicate; the kernel clears the token
            # on any terminal judgment.
            seeded = kwargs.get("options")
            if not is_string_keyed_dict(seeded):
                seeded = {}
                kwargs["options"] = seeded
            seeded["continuation_token"] = self.inputs.pending_continuation_token
        mirror_to_run_kwargs = continuation_token_observer_for(kwargs)

        def _observe_continuation_token(token: Any) -> None:
            self.inputs.pending_continuation_token = token
            mirror_to_run_kwargs(token)

        client_kwargs["continuation_token_observer"] = _observe_continuation_token
        return kwargs

    def _build_wire_retry_policy(self) -> WireRetryPolicy:
        max_retries = self._effective_max_retries()
        return WireRecipe(
            max_retries=max_retries,
            stall_timeout_seconds=self._stream_attempt_timeout,
            stall_max_retries=max_retries,
            stall_exhausted_action=StallExhaustedAction.BLOCKING_FALLBACK,
            backoff_schedule=self._BACKOFF_SCHEDULE,
            interrupted=lambda: self._interrupt.is_interrupted,
            interruptible_sleep=self._interruptible_sleep,
            publish_retry=self._publish_wire_retry_attempt,
            prepare_retry=self._injection.restore_for_retry,
            hosted_commits_in_flight=self._hosted_commits_in_flight_probe,
            answer_shown=None if self._shows_answer_text else lambda: False,
        ).build()

    def _effective_max_retries(self) -> int:
        """Return the injected budget or the lazily resolved class fallback."""
        if self._max_retries_override is not None:
            return self._max_retries_override
        return self._MAX_RETRIES

    def record_outcome(self, outcome: InvocationOutcome) -> None:
        self.inputs.evidence = self.inputs.evidence.add(outcome.effects)
        self.inputs.outcome = outcome

    @property
    def _emitter(self) -> BoundEmitter:
        if self._bound_emitter is None:
            raise ValueError("Cannot publish outside an active turn pass")
        return self._bound_emitter

    async def begin(self, request: RunRequest, resources: PassResources) -> None:
        """Start presentation and validation inside the backend pass try."""
        self._bound_emitter = self._invocation_publishers.bind(request.origin)
        self.approval.bind_publisher(self._emitter)
        self.tool_events.bind_origin(request.origin)
        if request.intent is RunIntent.FRESH:
            self.inputs.pending_continuation_token = None
        self.state.running = True
        self.state.was_interrupted = False
        self.state.run_failed = False
        self.state.last_error = ""
        self.state.partial_answer = ()
        self._interrupt.reset()
        self._retry_wait = None
        self._attempt_trace.reset()
        self._hosted_run_generation = next_hosted_run_generation()
        emitter = self._emitter
        self._projector = ContentProjector(emitter)
        self._hosted_bridge = HostedPresentationBridge(
            lambda operation: self._publish_hosted_operation(operation, emitter=emitter),
            run_generation=self._hosted_run_generation,
        )
        if self._response_validation is not None:
            self._response_validation.set_observation_hook(self._hosted_bridge)
        else:
            await self._hosted_bridge.begin_response(response_index=0)
        resources.begin()
        await self._emitter.publish(AgentThinking(session_id=self._session_id))
        await self._flush_carried_compressions()

    async def succeeded(self, result: AgentResponse[Any]) -> None:
        if not self._interrupt.is_interrupted:
            await self._finish_content("completed")
            await self._publish_response_text(result)

    def cancelled(self) -> None:
        self.state.was_interrupted = True

    def abort_cause(self) -> AbortCause | None:
        if self._interrupt.is_interrupted or self.state.was_interrupted:
            return AbortCause.USER_CANCEL
        return None

    async def failed(self, e: Exception) -> None:
        if self._interrupt.is_interrupted:
            self.state.was_interrupted = True
            return
        self.state.run_failed = True
        self.state.last_error = clean_error_message(e)
        tb = traceback.format_exc()
        # What the failed pass showed stays; its open segment ends before the error.
        await self._finish_content("failed")
        err_msg = self.state.last_error
        logger.error("Turn error: %s", err_msg)
        if self._hosted_bridge is not None:
            await self._hosted_bridge.attempt_rejected(err_msg)
        strategy = self._compaction_strategy
        display_message, display_hint = display_fields(
            e, max_context_tokens=strategy.max_context_tokens if strategy is not None else None
        )
        await self._emitter.publish(
            Error(
                code="executor_error",
                message=err_msg,
                recoverable=True,
                session_id=self._session_id,
                display_message=display_message,
                display_hint=display_hint,
            )
        )
        logger.debug("Turn traceback:\n%s", tb)

    async def finished(self) -> None:
        try:
            # An interrupted or cancelled pass publishes no outcome: its open
            # segment ends here, keeping the text it showed.
            interrupted = self._interrupt.is_interrupted or self.state.was_interrupted
            await self._finish_content("interrupted" if interrupted else "completed")
            if self._interrupt.is_interrupted and self._hosted_bridge is not None:
                await self._hosted_bridge.attempt_rejected("Execution interrupted", status=HostedToolStatus.INTERRUPTED)
        finally:
            # Close can cancel the awaits above. The reset must not erase an
            # interrupt that arrived while they ran.
            if self._interrupt.is_interrupted:
                self.state.was_interrupted = True
            if self._response_validation is not None:
                self._response_validation.set_observation_hook(None)
            if self._projector is not None:
                self.state.partial_answer = self._projector.take_unlanded_answer()
            self._projector = None
            self._interrupt.reset()
            self._retry_wait = None
            self.state.running = False
            if self._bound_emitter is not None:
                self._invocation_publishers.unbind(self._bound_emitter.origin)
            self._bound_emitter = None
            self.approval.bind_publisher(None)

    async def _flush_carried_compressions(self) -> None:
        """Commit a prior failed pass's queued folds before retry snapshots.

        A ``compress_context`` call can validate and queue a fold, then the
        pass can terminate before the history provider reaches its normal
        ``after_run`` flush.  The next user retry must treat that request as
        pre-existing committed work.  If it first runs inside
        ``StreamRetryLoop`` instead, a transient failure restores the
        pre-attempt history (removing the new ``CompressedBlock``) after the
        queue has already been consumed.  The live ``ContextCompressed``
        event then has no durable counterpart and disappears on replay.

        Bind the current provider state explicitly and flush before
        the L0 runner captures its attempt
        snapshot.  The history provider binds the same state again inside
        ``agent.run()``; that later bind is intentionally idempotent.
        """
        strategy = self._compaction_strategy
        if strategy is None:
            return
        history_state = self.backend.history_state
        strategy.bind_state(history_state)
        if not await strategy.flush_pending_compressions():
            return
        messages = history_state.get("messages", [])
        if isinstance(messages, list):
            # TurnRunner.pre_run captured its metadata floor before this
            # carried fold shortened history.  Finalization must start at the
            # post-fold boundary so retry output still receives approval,
            # modified-argument, and timestamp annotations.
            history_state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = len(messages)

    # ── main presentation ────────────────────────────────────────────

    async def _publish_wire_retry_attempt(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
    ) -> None:
        """The wire retry policy's notifier: UI only.

        A wire retry is recorded by the loop that performs it, as a new
        exchange under the same run (``retry.scheduled{retry_mode: wire}``).
        Recording a run-level retry here as well would count one retry twice
        and leave a scheduled marker that no ``retry.started`` ever answers.
        """
        await self._publish_retry_notice(message, attempt, max_attempts, delay_seconds, exc, scope="wire")

    async def _publish_retry_notice(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
        *,
        scope: Literal["wire", "run"] = "run",
    ) -> None:
        if self._hosted_bridge is not None and self.inputs.pending_continuation_token is None:
            await self._hosted_bridge.attempt_rejected(message)
        display_message, display_hint = display_fields(exc, retry_notice=True)
        # Armed before the notice is published: a skip sent as soon as a frontend shows it still finds the wait.
        wait = _RetryWait(uuid.uuid4().hex) if delay_seconds > 0 else None
        self._retry_wait = wait
        await self._emitter.publish(
            InvocationRetryAttempt(
                origin=self._emitter.origin,
                message=message,
                attempt=attempt,
                scope=scope,
                max_attempts=max_attempts,
                delay_seconds=delay_seconds,
                session_id=self._session_id,
                display_message=display_message,
                display_hint=display_hint,
                wait_id=wait.wait_id if wait is not None else "",
            )
        )

    async def _response_ready(self, response: ChatResponse) -> None:
        """The kernel landed a response: the pass projector shows what it did not stream."""
        if self._projector is not None:
            shown = self._hosted_bridge.text_shown if self._hosted_bridge is not None else None
            await self._projector.response_ready(response, shown=shown)

    async def _finish_content(self, status: ContentStatus) -> None:
        if self._projector is not None:
            await self._projector.finish(status)

    async def _publish_response_text(self, result: AgentResponse[Any]) -> None:
        """Publish the completion of a pass that answered.

        The final event is the frontend's turn-complete signal; its text is
        the last message's answer, which was already shown as it arrived. It
        is emitted even when the final text is empty.
        """
        if self._response_has_hosted_contents(result) and self._hosted_bridge is not None:
            await self._hosted_bridge.reconcile_accepted(result.messages, final=True)
            return
        final_text = self._extract_final_text(result)
        event_kwargs = self._assistant_message_event_kwargs()
        await self._emitter.publish(
            InvocationMessage(
                origin=self._emitter.origin,
                text=final_text,
                is_final=True,
                **event_kwargs,
                session_id=self._session_id,
            )
        )

    # ── control ──────────────────────────────────────────────────────

    async def interrupt(self) -> None:
        """Signal the Turn to stop and cancel any running tool.

        Sets the interrupt flag for the middleware boundary check AND
        cancels the current ``agent.run()`` child task so long-running
        tools (sub-agents, scripts) are actually terminated instead of
        completing in the background.
        """
        generation = self._hosted_run_generation
        self._interrupt.set_interrupted()
        if self._retry_wait is not None:
            self._retry_wait.woken.set()
        if self._hosted_bridge is not None:
            await self._hosted_bridge.attempt_rejected("Execution interrupted", status=HostedToolStatus.INTERRUPTED)
        if self._hosted_run_generation != generation:
            return
        if self._attempt_handle.active:
            sleep_call_ids = self._sleep.active_call_ids
            if sleep_call_ids:
                await self._interrupt_active_sleep(set(sleep_call_ids))
            # A wire retry may replace the attempt inside this pass; still
            # cancel it. A successor pass must never inherit this interrupt.
            if self._hosted_run_generation == generation:
                self._attempt_handle.cancel()

    async def _interrupt_active_sleep(self, call_ids: set[str]) -> None:
        """Let an active sleep publish its interrupted InvocationToolCallResult before cancellation."""
        if not call_ids:
            return
        pending = set(call_ids)
        loop = asyncio.get_running_loop()
        completed: asyncio.Future[None] = loop.create_future()

        origin = self._emitter.origin

        async def _on_tool_result(event: InvocationToolCallResult) -> None:
            if event.origin != origin:
                return
            pending.discard(event.call_id)
            if not pending and not completed.done():
                completed.set_result(None)

        await self._bus.subscribe(InvocationToolCallResult, _on_tool_result)
        try:
            interrupted = set(self._sleep.interrupt_active())
            pending.intersection_update(interrupted)
            if not pending:
                return
            # Keep Esc responsive: give the interrupted sleep a small
            # writeback window, then let task cancellation win.
            await asyncio.wait_for(completed, timeout=0.5)
        except TimeoutError:
            return
        finally:
            await self._bus.unsubscribe(InvocationToolCallResult, _on_tool_result)

    async def _interruptible_sleep(self, seconds: int) -> bool:
        """Wait *seconds* before a retry, returning True if interrupted.

        The wait the last retry notice announced ends early when the user
        skips it or an interrupt arrives; the interrupt latch is also read
        every ``_RETRY_WAIT_POLL_SECONDS``.
        """
        wait = self._retry_wait
        woken = wait.woken if wait is not None else asyncio.Event()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        try:
            while not self._interrupt.is_interrupted and not woken.is_set():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(woken.wait(), timeout=min(_RETRY_WAIT_POLL_SECONDS, remaining))
        finally:
            # Only this wait is over: a skip naming it from now on does nothing.
            if self._retry_wait is wait:
                self._retry_wait = None
        return self._interrupt.is_interrupted

    def skip_retry_wait(self, wait_id: str) -> bool:
        """End the wait the retry notice with *wait_id* announced; False when that wait is not the current one."""
        wait = self._retry_wait
        if not wait_id or wait is None or wait.wait_id != wait_id:
            return False
        wait.woken.set()
        return True

    def inject(
        self,
        text: str,
        created_at: datetime | str | None = None,
        injection_id: str | None = None,
        reminders: tuple[str, ...] = (),
        preparation: PreparationTrace | None = None,
        target_turn_id: str | None = None,
    ) -> None:
        """Queue text for injection before the next model call."""
        self._injection.queue(
            text,
            created_at=created_at,
            injection_id=injection_id,
            reminders=reminders,
            preparation=preparation,
            target_turn_id=target_turn_id,
        )

    def cancel_injection(self, injection_id: str) -> QueuedInjection | None:
        """Remove a still-pending injection; returns it, or None when too late."""
        return self._injection.cancel(injection_id)

    def reset_counters(self, *, reset_batch_id: bool = True) -> None:
        """Reset per-run counters (called by engine before each run).

        Args:
            reset_batch_id: If True, reset batch_id to 0 (for fresh runs).
                On resume, pass False to continue numbering from the
                interrupted run so batch_ids don't collide.
        """
        self.approval.reset()
        self.tool_events.reset_invocation_order()
        if reset_batch_id and self._batch_counter is not None:
            self._batch_counter.batch_id = 0

    def _get_history_messages(self) -> list:
        """Return the message list from the history provider state."""
        history_state = self._session.state.get("chrys_history", {})
        return history_state.get("messages", [])

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _response_has_hosted_contents(result: AgentResponse[Any]) -> bool:
        """Return whether the accepted response contains provider-hosted content."""
        return any(content.provider_hosted for message in result.messages for content in message.contents)

    @staticmethod
    def _extract_final_text(result: AgentResponse[Any]) -> str:
        """Extract only the *last* message's text from an AgentResponse.

        ``AgentResponse.text`` concatenates ALL messages from the tool loop.
        This method returns only the final LLM output after all tool calls.
        """
        if not result.messages:
            return ""
        last_msg = result.messages[-1]
        return "".join(c.text for c in last_msg.contents if c.type == "text" and c.text)

    def _assistant_message_event_kwargs(self) -> _AssistantMessageEventKwargs:
        """Return event kwargs matching the timestamp persisted for assistant output."""
        parsed = parse_created_at(self._session.state.get(LAST_ASSISTANT_CREATED_AT_STATE_KEY))
        return {"timestamp": parsed} if parsed is not None else {}

    def _snapshot_retry_state(self) -> _TurnRetryState:
        """Capture the four caller-owned retry buffers synchronously."""
        return _TurnRetryState(
            loop_recorder=self._loop_recorder.snapshot() if self._loop_recorder is not None else None,
            tool_events=self.tool_events.snapshot_retry_state(),
            approval=self.approval.snapshot_retry_state(),
            compaction=self._compaction_strategy.snapshot_retry_state()
            if self._compaction_strategy is not None
            else None,
        )

    def _restore_retry_state(self, retry_state: object) -> None:
        """Restore caller buffers inline; a failure stops the retry transaction."""
        if not isinstance(retry_state, _TurnRetryState):
            raise TypeError("Turn retry snapshot has invalid caller state.")
        if self._loop_recorder is not None and retry_state.loop_recorder is not None:
            self._loop_recorder.restore(retry_state.loop_recorder)
        self.tool_events.restore_retry_state(retry_state.tool_events)
        self.approval.restore_retry_state(retry_state.approval)
        if self._compaction_strategy is not None and retry_state.compaction is not None:
            self._compaction_strategy.restore_retry_state(retry_state.compaction)

    async def _reject_stream(self, reason: str) -> None:
        if self._hosted_bridge is not None:
            await self._hosted_bridge.attempt_rejected(reason)
