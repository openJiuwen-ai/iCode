# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sub-agent event middleware — publishes InvocationToolCallStart/InvocationToolCallResult events."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from chrys.foundation.events.types import (
    InvocationMessage,
    InvocationPresentationAttemptAccepted,
    InvocationPresentationAttemptRejected,
    InvocationProgress,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    ProvisionalPresentation,
)
from chrys.foundation.hosted_tools import HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY, HostedToolStatus
from chrys.foundation.io.result_content import extract_result_images, extract_result_text
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.tool_call_context import resolve_tool_call_context
from chrys.foundation.tool_execution_stamp import write_execution_stamp
from chrys.foundation.tool_kinds import KIND_SHELL, KIND_SUB_AGENT, get_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    tool_payload_observation,
    tool_result_metadata_failure_state,
)
from chrys.foundation.trajectory.context import bind_tool_operation, reset_tool_operation
from chrys.foundation.trajectory.event_types import ToolOutcome
from chrys.foundation.trajectory_timing import build_trajectory_timing, stamp_trajectory_timing
from chrys.foundation.util.sub_agent_context import (
    SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY,
    SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY,
    SubAgentParentResultMetadata,
    sub_agent_parent_call_id,
    sub_agent_parent_result_metadata,
)
from chrys.kernel._result_ceiling import preview_result_ceiling
from chrys.kernel.middleware import FunctionMiddleware
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_REJECTED_KEY,
    _SHORT_ID_LEN,
    _SLEEP_INTERRUPTED_KEY,
    _SLEEP_SKIPPED_KEY,
)
from chrys.service.agent_middleware.events.hook_dispatch import (
    append_extra_context_to_result,
    apply_before_tool_hooks,
    final_tool_args,
    fire_after_tool_hooks,
    get_call_id,
    get_provider_call_id,
    get_tool_invocation_order,
    is_mutable_tool_args,
    set_call_id,
    set_tool_invocation_order,
)
from chrys.service.agent_middleware.events.hosted_tools import (
    FinalTextOp,
    HostedPresentationBridge,
    HostedToolArgsOp,
    HostedToolProgressOp,
    HostedToolResultOp,
    HostedToolStartOp,
    HostedToolStatusOp,
    IntermediateTextOp,
    PresentationAttemptAcceptedOp,
    PresentationAttemptRejectedOp,
    PresentationSinkOperation,
)
from chrys.service.agent_middleware.events.intermediate_text import IntermediateTextBuffer
from chrys.service.agent_middleware.events.rejection_metadata import rejection_result_metadata, rejection_source
from chrys.service.agent_middleware.events.result_persistence import (
    RESULT_SLEEP_INTERRUPTED_METADATA_KEY,
    RESULT_SLEEP_SKIPPED_METADATA_KEY,
    RESULT_SUB_AGENT_AUDIT_COMPLETE_METADATA_KEY,
    RESULT_SUB_AGENT_INVOCATION_ID_METADATA_KEY,
    RESULT_SUB_AGENT_LOG_FILE_METADATA_KEY,
    write_result_carriage,
)
from chrys.service.agent_middleware.events.tool_error_text import tool_card_error_text
from chrys.service.mutations.pipeline import (
    abort_mutation_tracking,
    finalize_mutation_tracking,
    prepare_mutation_tracking,
)
from chrys.service.mutations.tool_names import _FILE_TOOLS, _IMPLICIT_WRITE_TOOLS
from chrys.service.tools.result_metadata import tool_result_metadata
from chrys.service.trajectory.preparation import (
    PreparationOutcome,
    PreparationScope,
    PreparationTrace,
    preparation_lock,
)
from chrys.service.trajectory.tools import ToolOperationTrace, tool_operation_id, tool_outcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.invocations import InvocationOrigin
    from chrys.kernel import Message
    from chrys.kernel.middleware import FunctionInvocationContext
    from chrys.service.hooks.manager import HookManager
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.session.sub_agent_logs import SubAgentLogStats


class SubAgentStatsMiddleware(FunctionMiddleware):
    """Record sub-agent inner tool completions without publishing events."""

    def __init__(self, stats: SubAgentLogStats) -> None:
        self._stats = stats

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        cancelled = False
        try:
            await call_next()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                self._stats.record_tool_call()


class SubAgentEventMiddleware(FunctionMiddleware):
    """Publishes InvocationToolCallStart/InvocationToolCallResult events for sub-agent tool invocations.

    Lightweight variant of ToolEventMiddleware for sub-agents. Created fresh per ``_invoke()``
    call with a unique ``invocation_id``, then passed as per-run middleware to
    ``agent.run()``.

    A per-invocation ``IntermediateTextBuffer`` preserves the same ordering as
    the main agent: pending assistant text is delivered before the first tool
    start in its batch. Sub-agent transcripts do not persist batch IDs.

    Tracks cumulative tool call count and publishes ``InvocationProgress``
    after each tool result so the TUI can display live stats.

    When a ``mutation_tracker`` is provided, records file mutations (write_file,
    edit_file) and shell command changes so that ``/diff`` picks up sub-agent
    modifications.
    """

    def __init__(
        self,
        event_bus: EventBus,
        agent_name: str,
        invocation_id: str,
        mutation_tracker: MutationTracker | None = None,
        hook_manager: HookManager | None = None,
        profile_name: str = "",
        session_id: str | None = None,
        workspace_cwd: str = "",
        serialize_implicit_windows: bool = False,
        stats: SubAgentLogStats | None = None,
        mutation_coordinator: MutationCoordinator | None = None,
        tool_result_ceiling_tokens: int | None = None,
        intermediate_buffer: IntermediateTextBuffer | None = None,
        *,
        origin: InvocationOrigin,
    ) -> None:
        self._bus = event_bus
        self._origin = origin
        self._agent_name = agent_name
        self._invocation_id = invocation_id
        self._mutation_tracker = mutation_tracker
        self._mutation_coordinator = mutation_coordinator
        self._hook_manager = hook_manager
        self._profile_name = profile_name
        self._session_id = session_id
        self._workspace_cwd = workspace_cwd
        self._serialize_implicit_windows = serialize_implicit_windows
        self._stats = stats
        self._tool_result_ceiling_tokens = tool_result_ceiling_tokens
        self._intermediate_buffer = intermediate_buffer
        self._tool_call_count = 0
        self._tool_invocation_order = 0
        self._total_tokens = 0
        self._total_usage_tokens = 0
        self._progress_tasks: set[asyncio.Task[None]] = set()
        self._progress_publish_tail: asyncio.Task[None] | None = None
        self._hosted_run_generation = 0
        self._hosted_bridge: HostedPresentationBridge | None = None

    def bind_origin(self, origin: InvocationOrigin) -> None:
        """Bind the next pass; each tool call and hosted bridge keeps the origin it started under."""
        self._origin = origin

    def begin_hosted_pass(self) -> HostedPresentationBridge:
        """Create the presentation bridge owned by the next controller pass."""
        self._hosted_run_generation += 1
        origin = self._origin
        self._hosted_bridge = HostedPresentationBridge(
            lambda operation: self._publish_hosted_operation(operation, origin),
            run_generation=self._hosted_run_generation,
            before_response=self.release_intermediate_text,
        )
        return self._hosted_bridge

    async def reconcile_hosted_response(self, messages: Sequence[Message]) -> None:
        """Reconcile the accepted full-pass transcript through the current bridge.

        The controller calls this once after the pass completes, so no more
        tool starts can arrive: barriers for calls that never entered the
        tool pipeline (unknown tool, pre-pipeline argument rejection) must
        be released here or the hosted events behind them stay buffered
        forever. The text such a call streamed is released first, so it
        precedes what the reconcile publishes.
        """
        await self.release_intermediate_text()
        if self._hosted_bridge is not None:
            await self._hosted_bridge.reconcile_accepted(messages, final=True)

    async def release_intermediate_text(self) -> None:
        """Publish intermediate text that no tool start has released yet.

        A call that never enters the tool pipeline (unknown tool, pre-pipeline
        argument rejection) starts no tool, so the text streamed with it stays
        buffered until the next response starts or the pass ends.
        """
        await self._release_intermediate_text(None)

    async def finish_intermediate_text(self) -> None:
        """Release what is still buffered as the pass ends; see ``IntermediateTextBuffer.finish``."""
        if self._intermediate_buffer is not None:
            await self._intermediate_buffer.finish(self._publish_intermediate_text)

    async def _release_intermediate_text(self, preamble: PreparationTrace | None) -> None:
        if self._intermediate_buffer is not None:
            await self._intermediate_buffer.release(self._publish_intermediate_text, preamble)

    async def _publish_intermediate_text(self, text: str) -> None:
        await self._bus.publish(
            InvocationMessage(
                is_final=False,
                is_intermediate=True,
                origin=self._origin,
                agent_name=self._agent_name,
                text=text,
                session_id=self._session_id,
            )
        )

    async def reject_hosted_attempt(
        self,
        reason: str,
        *,
        status: str = HostedToolStatus.INTERRUPTED,
    ) -> None:
        """Terminalize running hosted cards at a controller boundary."""
        if self._hosted_bridge is not None:
            await self._hosted_bridge.attempt_rejected(reason, status=status)

    @staticmethod
    def _artifact_descriptors(operation: HostedToolResultOp) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        for artifact in operation.view.artifacts:
            # "path" must stay a real URI: consumers turn it into links, and a
            # bare hosted filename (OpenAI hosted_file) is not addressable.
            descriptor: dict[str, Any] = {
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

    async def _publish_hosted_operation(self, operation: PresentationSinkOperation, origin: InvocationOrigin) -> None:
        """Map one hosted presentation operation to sub-agent events."""
        if isinstance(operation, IntermediateTextOp):
            presentation = None
            if operation.provisional:
                segment_id = operation.segment_ids[0] if operation.segment_ids else ""
                presentation = ProvisionalPresentation(operation.attempt_id, segment_id)
            await self._bus.publish(
                InvocationMessage(
                    is_final=False,
                    is_intermediate=True,
                    origin=origin,
                    agent_name=self._agent_name,
                    text=operation.text,
                    presentation=presentation,
                    session_id=self._session_id,
                )
            )
            return
        if isinstance(operation, PresentationAttemptAcceptedOp):
            await self._bus.publish(
                InvocationPresentationAttemptAccepted(
                    origin=origin,
                    agent_name=self._agent_name,
                    attempt_id=operation.attempt_id,
                    segment_ids=tuple(
                        segment_id for segment in operation.segments for segment_id in segment.segment_ids
                    ),
                    session_id=self._session_id,
                )
            )
            return
        if isinstance(operation, PresentationAttemptRejectedOp):
            await self._bus.publish(
                InvocationPresentationAttemptRejected(
                    origin=origin,
                    agent_name=self._agent_name,
                    attempt_id=operation.attempt_id,
                    session_id=self._session_id,
                )
            )
            return
        if isinstance(operation, FinalTextOp):
            return
        view = operation.view
        common: dict[str, Any] = {
            "agent_name": self._agent_name,
            "tool_name": view.tool_name,
            "call_id": operation.presentation_id,
            "provider_hosted": True,
            "hosted_family": view.family,
            "provider": view.provider,
            "session_id": self._session_id,
        }
        if isinstance(operation, HostedToolStartOp):
            await self._bus.publish(
                InvocationToolCallStart(
                    origin=origin,
                    **common,
                    tool_kind=HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY.get(view.family, ""),
                    args=view.arguments,
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
                    # AIxCoding telemetry: workspace cwd for projectName/git/fileName.
                    workspace_cwd=self._workspace_cwd,
                )
            )
        elif isinstance(operation, HostedToolArgsOp):
            await self._bus.publish(
                InvocationToolCallArgsUpdated(
                    origin=origin,
                    **common,
                    tool_kind=HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY.get(view.family, ""),
                    args=view.arguments,
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
                )
            )
        elif isinstance(operation, HostedToolProgressOp):
            await self._bus.publish(
                InvocationToolCallProgress(
                    origin=origin,
                    **common,
                    lines=view.result_text.splitlines(),
                    image_contents=view.image_contents,
                    snapshot_metadata=view.metadata,
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
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
            await self._bus.publish(
                InvocationToolCallStatusUpdated(
                    origin=origin,
                    **common,
                    status=view.status,
                    provider_status=view.provider_status,
                    metadata=metadata,
                )
            )
        elif isinstance(operation, HostedToolResultOp):
            await self._bus.publish(
                InvocationToolCallResult(
                    origin=origin,
                    **common,
                    result=view.result_text,
                    image_contents=view.image_contents,
                    metadata=view.metadata,
                    artifacts=self._artifact_descriptors(operation),
                    provider_item_type=view.provider_item_type,
                    provider_call_id=view.provider_call_id,
                    provider_status=view.provider_status,
                    # AIxCoding telemetry: workspace cwd for projectName/git/fileName.
                    workspace_cwd=self._workspace_cwd,
                )
            )

    def record_usage(self, total_tokens: int, total_usage_tokens: int) -> None:
        """Record sub-agent usage and publish progress from the usage callback."""
        if self._stats is not None:
            self._stats.record_usage(total_tokens, total_usage_tokens)
        self._total_tokens = total_tokens
        self._total_usage_tokens = total_usage_tokens
        # ``record_usage`` is sync (called from ``UsageTrackingMiddleware.
        # _fire_callback``).  Skip the publish when there's no running loop —
        # callers driving this from a non-async context (test fixture, future
        # threadpool result hook) should not crash; the cumulative state has
        # already been recorded on ``self``.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._schedule_progress_publish()

    def _schedule_progress_publish(self) -> None:
        """Schedule a InvocationProgress publish chained behind the previous one.

        Chaining matches the parent ``_track_usage_publish`` pattern in
        ``chrys.service.usage`` — without it, two close-together publishes can be
        delivered out of order under a slow subscriber, regressing the displayed
        token/tool-call counters.  Tasks read ``self._*`` live, so a stale
        ``tool_call_count`` snapshot can never undo a fresher one.
        """
        previous = self._progress_publish_tail

        async def _publish_after_previous() -> None:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            await self._publish_progress()

        task = asyncio.create_task(_publish_after_previous())
        self._progress_publish_tail = task
        self._progress_tasks.add(task)
        task.add_done_callback(self._progress_tasks.discard)
        task.add_done_callback(self._maybe_clear_tail)
        task.add_done_callback(_observe_task_exception)

    def _maybe_clear_tail(self, task: asyncio.Task[None]) -> None:
        if self._progress_publish_tail is task:
            self._progress_publish_tail = None

    async def flush_progress(self) -> None:
        """Wait for any usage-triggered progress publishes to finish.

        Loops until no tasks remain — a ``record_usage`` that fires while the
        gather is suspended (e.g. a late streaming finalization) adds a task
        to ``_progress_tasks`` that a single snapshot would miss.
        """
        while self._progress_tasks:
            tasks = tuple(self._progress_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._progress_tasks.difference_update(tasks)

    async def _publish_progress(self) -> None:
        await self._bus.publish(
            InvocationProgress(
                origin=self._origin,
                agent_name=self._agent_name,
                tool_call_count=self._tool_call_count,
                total_tokens=self._total_tokens,
                total_usage_tokens=self._total_usage_tokens,
                session_id=self._session_id,
            )
        )

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        # Bound before the first await, for the reason spelled out in
        # ``ToolEventMiddleware.process``: the kernel hands the operation over
        # at dispatch, and everything below can be cancelled before the start
        # marker is written.
        trajectory = ToolOperationTrace.open(context.metadata if isinstance(context.metadata, dict) else {})
        preamble = (
            PreparationTrace.open(
                scope=PreparationScope.TOOL_PREAMBLE,
                phase="tool_dispatch",
                parent_operation_id=trajectory.context.innermost_model_operation_id,
                target_operation_id=trajectory.operation_id,
                context=trajectory.context,
            )
            if trajectory is not None
            else None
        )
        origin = self._origin
        try:
            if preamble is not None:
                await preamble.started()
            await self._process(context, call_next, trajectory, preamble, origin)
        except Exception:
            if preamble is not None:
                preamble.finished_soon(outcome=PreparationOutcome.FAILED)
            self._abandon_operation(context, trajectory, preamble, ToolOutcome.ERRORED)
            raise
        except BaseException:
            if preamble is not None:
                preamble.finished_soon(outcome=PreparationOutcome.INTERRUPTED)
            self._abandon_operation(context, trajectory, preamble, ToolOutcome.INTERRUPTED)
            raise

    def _abandon_operation(
        self,
        context: FunctionInvocationContext,
        trajectory: ToolOperationTrace | None,
        preamble: PreparationTrace | None,
        outcome: str,
    ) -> None:
        """Close an operation that never reached its own start marker (a no-op once it has)."""
        if trajectory is None:
            return
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        args = context.arguments if is_mutable_tool_args(context.arguments) else {}
        trajectory.abandoned_soon(
            tool_name=context.function.name,
            tool_kind=get_tool_kind(context.function) or "",
            batch_index=None,
            invocation_order=get_tool_invocation_order(context),
            arguments=args,
            invocation_metadata=metadata,
            tool_context=resolve_tool_call_context(context.function, args) or None,
            outcome=outcome,
            preamble_operation_id=(
                preamble.operation_id if preamble is not None and preamble.start_committed else None
            ),
        )

    async def _process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
        trajectory: ToolOperationTrace | None,
        preamble: PreparationTrace | None,
        origin: InvocationOrigin,
    ) -> None:
        tool_order = get_tool_invocation_order(context)
        if tool_order is None:
            tool_order = self._tool_invocation_order
        self._tool_invocation_order = max(self._tool_invocation_order, tool_order + 1)
        set_tool_invocation_order(context, tool_order)

        await self._release_intermediate_text(preamble)

        provider_call_id = get_provider_call_id(context)
        call_id = get_call_id(context) or uuid4().hex[:_SHORT_ID_LEN]
        tool_name = context.function.name
        args = context.arguments if is_mutable_tool_args(context.arguments) else {}
        # Stash the Chrys call_id in metadata so downstream middleware
        # references the same id as the published
        # InvocationToolCallStart/InvocationToolCallResult events.
        set_call_id(context, call_id)

        tool_kind = get_tool_kind(context.function) or ""
        blocked = await apply_before_tool_hooks(
            manager=self._hook_manager,
            context=context,
            session_id=self._session_id,
            profile_name=self._profile_name,
            tool_name=tool_name,
            kind=tool_kind,
            call_id=call_id,
            args=args,
            workspace_cwd=self._workspace_cwd,
            target_operation_id=trajectory.operation_id if trajectory is not None else None,
        )
        args = context.arguments if is_mutable_tool_args(context.arguments) else {}

        async def _blocked_call_next() -> None:
            return None

        # Serialize concurrent mutations to the same file (same rationale
        # as ToolEventMiddleware — framework uses asyncio.gather()).
        file_lock_path = _file_lock_path(tool_name, args, self._workspace_cwd)
        implicit_window = _uses_implicit_window(tool_name, tool_kind)
        if blocked:
            await self._process_tool_call(
                context, _blocked_call_next, call_id, provider_call_id, tool_name, args, trajectory, preamble, origin
            )
        elif self._serialize_implicit_windows and implicit_window and self._mutation_tracker is not None:
            async with preparation_lock(self._mutation_tracker.get_implicit_window_lock(), preamble):
                await self._process_tool_call(
                    context, call_next, call_id, provider_call_id, tool_name, args, trajectory, preamble, origin
                )
        elif file_lock_path and self._mutation_tracker is not None:
            async with preparation_lock(self._mutation_tracker.get_file_lock(file_lock_path), preamble):
                await self._process_tool_call(
                    context, call_next, call_id, provider_call_id, tool_name, args, trajectory, preamble, origin
                )
        else:
            await self._process_tool_call(
                context, call_next, call_id, provider_call_id, tool_name, args, trajectory, preamble, origin
            )

    async def _process_tool_call(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
        call_id: str,
        provider_call_id: str,
        tool_name: str,
        args: dict,
        trajectory: ToolOperationTrace | None,
        preamble: PreparationTrace | None,
        origin: InvocationOrigin,
    ) -> None:
        """Core tool-call processing for sub-agents."""
        tool_kind = get_tool_kind(context.function) or ""
        is_shell = tool_kind == KIND_SHELL
        is_sub_agent = tool_kind == KIND_SUB_AGENT

        # Pre-execution mutation tracking
        _meta = context.metadata if isinstance(context.metadata, dict) else {}
        pre_rejected = bool(_meta.get(_APPROVAL_REJECTED_KEY))
        mut_ctx = None
        if not pre_rejected:
            mut_ctx = await prepare_mutation_tracking(
                self._mutation_tracker,
                tool_name,
                args,
                call_id,
                is_shell,
                workspace_cwd=self._workspace_cwd,
                coordinator=self._mutation_coordinator,
            )

        await self._bus.publish(
            InvocationToolCallStart(
                origin=origin,
                agent_name=self._agent_name,
                tool_name=tool_name,
                tool_kind=tool_kind,
                args=args,
                call_id=call_id,
                session_id=self._session_id,
                # AIxCoding telemetry: workspace cwd for projectName/git/fileName.
                workspace_cwd=self._workspace_cwd,
            )
        )
        if self._hosted_bridge is not None:
            await self._hosted_bridge.local_call_start_published(provider_call_id)

        # A delegation from this invocation (a workflow node's, or a nested sub-agent's) binds its
        # child to this call the same way ToolEventMiddleware binds a chat turn's: the child reads
        # the parent call ids from these ContextVars and writes its invocation id back for the result.
        sub_agent_token = None
        sub_agent_metadata_token = None
        sub_agent_metadata: SubAgentParentResultMetadata | None = None
        if is_sub_agent:
            raw_commit = _meta.get(SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY)
            sub_agent_token = sub_agent_parent_call_id.set(call_id)
            sub_agent_metadata = SubAgentParentResultMetadata(
                parent_provider_call_id=provider_call_id,
                parent_event_call_id=call_id,
                commit_interrupted_result=raw_commit if callable(raw_commit) else None,
            )
            sub_agent_metadata_token = sub_agent_parent_result_metadata.set(sub_agent_metadata)

        started_at = datetime.now(UTC)
        start = time.monotonic()
        error_text = ""
        errored = False
        raised_error_kind: str | None = None
        cancelled = False
        shell_metadata_token = None
        shell_metadata: dict[str, object] | None = None
        generic_metadata: dict[str, object] = {}
        generic_metadata_token = tool_result_metadata.set(generic_metadata)
        payload_observation: dict[str, object] = {}
        payload_observation_token = tool_payload_observation.set(payload_observation)
        if is_shell:
            from chrys.service.tools.builtins.shell import shell_result_metadata

            shell_metadata = {}
            shell_metadata_token = shell_result_metadata.set(shell_metadata)
        tool_operation_token = bind_tool_operation(
            trajectory.operation_id if trajectory is not None else tool_operation_id(_meta)
        )
        try:
            if preamble is not None:
                await preamble.finished(outcome=PreparationOutcome.HANDOFF)
            # Inside the block that closes the operation: the start marker
            # awaits its write ack, and an interrupt landing there would
            # otherwise leave the operation open forever. Nothing between the
            # trace and here awaits, so the marker still precedes the call.
            if trajectory is not None:
                await trajectory.started(
                    tool_name=tool_name,
                    tool_kind=tool_kind,
                    batch_index=None,
                    invocation_order=get_tool_invocation_order(context),
                    arguments=args,
                    invocation_metadata=_meta,
                    # Resolved again after the call for the persisted carriage;
                    # here it describes the call as dispatched, alongside the
                    # argument fingerprint of those same arguments.
                    tool_context=resolve_tool_call_context(context.function, args) or None,
                    preamble_operation_id=(
                        preamble.operation_id if preamble is not None and preamble.start_committed else None
                    ),
                )
            await call_next()
        except Exception as exc:
            errored = True
            raised_error_kind = type(exc).__name__
            error_text = tool_card_error_text(exc)
            raise
        except BaseException:
            cancelled = True
            raise
        finally:
            reset_tool_operation(tool_operation_token)
            if shell_metadata_token is not None:
                from chrys.service.tools.builtins.shell import shell_result_metadata

                shell_result_metadata.reset(shell_metadata_token)
            tool_result_metadata.reset(generic_metadata_token)
            tool_payload_observation.reset(payload_observation_token)
            if sub_agent_token is not None:
                sub_agent_parent_call_id.reset(sub_agent_token)
            if sub_agent_metadata_token is not None:
                sub_agent_parent_result_metadata.reset(sub_agent_metadata_token)

            if not isinstance(context.metadata, dict):
                context.metadata = {}
            rejected = bool(context.metadata.get(_APPROVAL_REJECTED_KEY, False))
            finished_at = datetime.now(UTC)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            # A rejected delegation reports no duration to the UI — it never
            # ran — but the operation was open for the whole approval wait.
            duration_ms = 0 if rejected else elapsed_ms
            stamp_trajectory_timing(
                context.metadata,
                build_trajectory_timing(
                    started_at=finished_at if rejected else started_at,
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                ),
                overwrite=True,
            )

            if cancelled and trajectory is not None:
                # Unwinding from CancelledError: close without awaiting the ack.
                trajectory.finished_soon(outcome=ToolOutcome.INTERRUPTED, duration_ms=elapsed_ms)
            if cancelled and mut_ctx is not None:
                # Cancelled tasks raise CancelledError at their next await,
                # so the async finalize below is unreachable — release the
                # trace wrapper and close the coordination window
                # synchronously or the registry advertises a live command
                # to peers until shutdown. The command may have written
                # before the interrupt, so implicit detection is truncated.
                abort_mutation_tracking(mut_ctx, self._mutation_coordinator, tracker=self._mutation_tracker)
            if not cancelled:
                try:
                    result_text = error_text or extract_result_text(context.result)
                    _meta = context.metadata
                    # Refresh to the final (approval-modified) args — everything
                    # below (after-tool hooks, persisted provenance) must see the
                    # arguments the tool actually ran with, same as the main
                    # ToolEventMiddleware.
                    args = final_tool_args(context, args)
                    if rejected and mut_ctx is not None:
                        # Rejected at approval, post-prepare: finalize is
                        # deliberately skipped (the tool never executed), so
                        # release the trace wrapper and close the observation
                        # window here.
                        abort_mutation_tracking(mut_ctx, self._mutation_coordinator)
                    if self._stats is not None:
                        self._stats.record_tool_call()
                    self._tool_call_count += 1

                    # Post-execution mutation tracking (skip for rejected tools)
                    metadata: dict[str, Any] = {}
                    metadata.update(generic_metadata)
                    if shell_metadata:
                        metadata.update(shell_metadata)
                    if not rejected and mut_ctx is not None:
                        # Authorship gate for coordination claims: structured
                        # "Error: ..." results (tool_error / record_tool_failure)
                        # mean the write may not have happened — the observed
                        # delta could be a peer racing our window.  ``errored``
                        # only covers raised exceptions; the metadata failure
                        # state covers the no-raise failure paths.
                        tool_failed = errored or tool_result_metadata_failure_state(metadata) is True
                        try:
                            mut_result = await finalize_mutation_tracking(
                                self._mutation_tracker,
                                mut_ctx,
                                call_id,
                                coordinator=self._mutation_coordinator,
                                tool_errored=tool_failed,
                            )
                        except BaseException:
                            # An interrupt landing DURING finalize (git
                            # calibration, scans, and trace parsing are executor
                            # hops) bypasses both the cancel-path abort above
                            # (``cancelled`` only covers call_next) and
                            # finalize's own release — clean up synchronously.
                            # Idempotent after a partial finalize. The command
                            # ran; its aborted detection is truncated.
                            abort_mutation_tracking(mut_ctx, self._mutation_coordinator, tracker=self._mutation_tracker)
                            raise
                        if mut_result.file_snapshot is not None:
                            metadata["file_snapshot"] = mut_result.file_snapshot
                        if mut_result.file_operation is not None:
                            metadata["file_mutation_op"] = mut_result.file_operation
                        if mut_result.file_bytes_changed is not None:
                            metadata["file_bytes_changed"] = mut_result.file_bytes_changed
                        if mut_result.file_hashes is not None:
                            metadata["file_mutation_hashes"] = mut_result.file_hashes
                        if mut_result.shell_snapshots:
                            metadata["shell_file_snapshots"] = mut_result.shell_snapshots
                    if rejected:
                        metadata.update(rejection_result_metadata(_meta))
                    if isinstance(_meta, dict) and _meta.get(_SLEEP_SKIPPED_KEY, False):
                        metadata[RESULT_SLEEP_SKIPPED_METADATA_KEY] = True
                    if isinstance(_meta, dict) and _meta.get(_SLEEP_INTERRUPTED_KEY, False):
                        metadata[RESULT_SLEEP_INTERRUPTED_METADATA_KEY] = True
                    if sub_agent_metadata is not None:
                        if sub_agent_metadata.sub_agent_invocation_id:
                            metadata[RESULT_SUB_AGENT_INVOCATION_ID_METADATA_KEY] = (
                                sub_agent_metadata.sub_agent_invocation_id
                            )
                        if sub_agent_metadata.sub_agent_log_file:
                            metadata[RESULT_SUB_AGENT_LOG_FILE_METADATA_KEY] = sub_agent_metadata.sub_agent_log_file
                        if sub_agent_metadata.sub_agent_audit_complete:
                            metadata[RESULT_SUB_AGENT_AUDIT_COMPLETE_METADATA_KEY] = True
                        if sub_agent_metadata.transcript_final_text is not None:
                            metadata[SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY] = (
                                sub_agent_metadata.transcript_final_text
                            )
                    if errored:
                        metadata[TOOL_ERRORED_METADATA_KEY] = True
                    structured_failed = tool_result_metadata_failure_state(metadata) is True
                    source = rejection_source(_meta) if rejected else ""
                    # Same carriage as ToolEventMiddleware: the kernel folds the
                    # carried metadata/provenance onto the right Contents at
                    # construction — no post-run positional re-association.
                    tool_context = resolve_tool_call_context(context.function, args)
                    write_result_carriage(context, metadata=metadata, tool_context=tool_context or None)

                    hook_decision = await fire_after_tool_hooks(
                        manager=self._hook_manager,
                        session_id=self._session_id,
                        profile_name=self._profile_name,
                        tool_name=tool_name,
                        kind=tool_kind,
                        call_id=call_id,
                        args=args,
                        result_text=result_text,
                        duration_ms=duration_ms,
                        errored=errored,
                        failed=structured_failed,
                        approval_rejected=rejected,
                        rejection_source=source,
                        workspace_cwd=self._workspace_cwd,
                        target_operation_id=trajectory.operation_id if trajectory is not None else None,
                    )
                    context.result, result_text = append_extra_context_to_result(
                        context.result,
                        result_text,
                        hook_decision.extra_context,
                    )
                    structured_error_kind = metadata.get(TOOL_ERROR_KIND_METADATA_KEY)
                    if not isinstance(context.metadata, dict):
                        context.metadata = {}
                    write_execution_stamp(
                        context.metadata,
                        context.arguments,
                        outcome="error" if errored or structured_failed else "ok",
                        error_kind=(
                            raised_error_kind
                            or (structured_error_kind if isinstance(structured_error_kind, str) else None)
                        ),
                    )
                    result_images = extract_result_images(context.result)
                    await self._bus.publish(
                        InvocationToolCallResult(
                            origin=origin,
                            agent_name=self._agent_name,
                            tool_name=tool_name,
                            call_id=call_id,
                            result=result_text,
                            image_contents=result_images,
                            duration_ms=duration_ms,
                            metadata=metadata,
                            session_id=self._session_id,
                            # AIxCoding telemetry: workspace cwd for projectName/git/fileName.
                            workspace_cwd=self._workspace_cwd,
                        )
                    )
                    if trajectory is not None:
                        # Bounded the way the kernel will bound it — see the
                        # same call in ``tool_events``.
                        await trajectory.payload_observed(
                            result_text=preview_result_ceiling(
                                result_text,
                                self._tool_result_ceiling_tokens,
                                observation=payload_observation,
                            ),
                            image_count=len(result_images),
                            observation=payload_observation,
                        )
                        await trajectory.finished(
                            outcome=tool_outcome(
                                cancelled=False, rejected=rejected, errored=errored, result_metadata=metadata
                            ),
                            duration_ms=elapsed_ms,
                            result_metadata=metadata,
                            error_kind=raised_error_kind,
                        )
                    await self._publish_progress()
                except BaseException:
                    # Interrupted while closing out a call that already ran:
                    # the operation is open, and only this path can close it.
                    if trajectory is not None:
                        trajectory.finished_soon(outcome=ToolOutcome.INTERRUPTED, duration_ms=elapsed_ms)
                    raise


def _file_lock_path(tool_name: str, args: dict[str, Any], workspace_cwd: str = "") -> str:
    """Return a file path for write/edit serialization, or empty when absent."""
    if tool_name not in _FILE_TOOLS:
        return ""
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return ""
    return resolve_workspace_path(path, base_cwd=workspace_cwd or None)


def _uses_implicit_window(tool_name: str, tool_kind: str) -> bool:
    """Return whether a tool needs serialized implicit mutation observation."""
    return tool_kind == KIND_SHELL or tool_name in _IMPLICIT_WRITE_TOOLS


def _observe_task_exception(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()
