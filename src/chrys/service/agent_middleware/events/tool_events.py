# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool event middleware — publishes InvocationToolCallStart/InvocationToolCallResult for each invocation."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from chrys.foundation.events.types import (
    InvocationMessage,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
)
from chrys.foundation.io.result_content import extract_result_images, extract_result_text
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.tool_call_context import resolve_tool_call_context
from chrys.foundation.tool_execution_stamp import write_execution_stamp
from chrys.foundation.tool_kinds import KIND_SHELL, KIND_SUB_AGENT, KIND_WEB_FETCH, KIND_WEB_SEARCH, get_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    tool_payload_observation,
    tool_result_metadata_failure_state,
)
from chrys.foundation.trajectory.context import bind_tool_operation, reset_tool_operation
from chrys.foundation.trajectory.event_types import ToolOutcome
from chrys.foundation.trajectory_timing import build_trajectory_timing, stamp_trajectory_timing
from chrys.kernel._result_ceiling import preview_result_ceiling
from chrys.kernel.middleware import FunctionMiddleware
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_MODIFIED_ARGS_KEY,
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
from chrys.service.tools.builtins.web.progress import web_progress_callback
from chrys.service.tools.result_metadata import tool_result_metadata
from chrys.service.trajectory.preparation import (
    PreparationOutcome,
    PreparationScope,
    PreparationTrace,
    preparation_lock,
)
from chrys.service.trajectory.tools import ToolOperationTrace, tool_operation_id, tool_outcome

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.foundation.events.bus import EventBus
    from chrys.kernel.middleware import FunctionInvocationContext
    from chrys.service.hooks.manager import HookManager
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker


@dataclass(frozen=True, slots=True)
class ToolBatchRecord:
    """Batch attribution for one dispatched tool invocation, recorded PRE-execution.

    ``provider_call_id`` + ``tool_name`` are the ONLY matching keys for
    ``persist_batch_ids`` — the persisted ``function_call.call_id`` is the
    raw provider id, so identity matching works across interruption and
    mid-pass compression where positional pairing misattributes.
    ``tool_order`` (a per-run ordinal) and ``batch_id`` context ride along
    for diagnostics/assignment; ``tool_order`` is never a match key.
    """

    provider_call_id: str
    tool_name: str
    tool_order: int
    batch_id: int


@dataclass(frozen=True, slots=True)
class ToolEventRetrySnapshot:
    """Append-only tool-event state that must survive an outer retry.

    Result metadata needs no snapshot slot: it rides each invocation's
    context into the result Content at construction, so a rolled-back
    attempt's metadata leaves with its messages.
    """

    batch_records: tuple[ToolBatchRecord, ...]
    invocation_order: int


class ToolEventMiddleware(FunctionMiddleware):
    """Publishes InvocationToolCallStart and InvocationToolCallResult events for each tool invocation.

    Wraps each function call in the agent's tool loop, firing events before
    and after execution. This lets the frontend display real-time tool activity.

    When an ``IntermediateTextBuffer`` is attached, drains pending intermediate
    text and publishes it as ``InvocationMessage(is_intermediate=True)`` **before**
    ``InvocationToolCallStart``.  This ensures correct event ordering (text → tool call).
    """

    def __init__(
        self,
        event_bus: EventBus,
        session_id: str | None = None,
        intermediate_buffer: IntermediateTextBuffer | None = None,
        mutation_tracker: MutationTracker | None = None,
        hook_manager: HookManager | None = None,
        profile_name: str = "",
        workspace_cwd: str = "",
        serialize_implicit_windows: bool = False,
        mutation_coordinator: MutationCoordinator | None = None,
        on_start_published: Callable[[str], Awaitable[None]] | None = None,
        tool_result_ceiling_tokens: int | None = None,
        *,
        origin: InvocationOrigin,
    ) -> None:
        self._origin = origin
        self._bus = event_bus
        self._session_id = session_id
        self._intermediate_buffer = intermediate_buffer
        self._mutation_tracker = mutation_tracker
        self._mutation_coordinator = mutation_coordinator
        self._hook_manager = hook_manager
        self._profile_name = profile_name
        self._workspace_cwd = workspace_cwd
        self._serialize_implicit_windows = serialize_implicit_windows
        self._on_start_published = on_start_published
        self._tool_result_ceiling_tokens = tool_result_ceiling_tokens
        self._tool_batch_records: list[ToolBatchRecord] = []
        self._tool_invocation_order = 0

    def bind_origin(self, origin: InvocationOrigin) -> None:
        """Bind the next pass; each dispatched callback captures its own origin."""
        self._origin = origin

    async def release_intermediate_text(self) -> None:
        """Publish intermediate text that no tool start has released yet.

        A call that never enters the tool pipeline (unknown tool, pre-pipeline
        argument rejection) starts no tool, so the text streamed with it stays
        buffered until the next response starts or the pass ends.
        """
        await self._release_intermediate_text(self._origin, None)

    async def finish_intermediate_text(self) -> None:
        """Release what is still buffered as the pass ends; see ``IntermediateTextBuffer.finish``."""
        if self._intermediate_buffer is not None:
            await self._intermediate_buffer.finish(self._intermediate_publisher(self._origin))

    async def _release_intermediate_text(self, origin: InvocationOrigin, preamble: PreparationTrace | None) -> None:
        if self._intermediate_buffer is not None:
            await self._intermediate_buffer.release(self._intermediate_publisher(origin), preamble)

    def _intermediate_publisher(self, origin: InvocationOrigin) -> Callable[[str], Awaitable[None]]:
        async def publish(text: str) -> None:
            await self._bus.publish(
                InvocationMessage(
                    origin=origin, text=text, is_final=False, is_intermediate=True, session_id=self._session_id
                )
            )

        return publish

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        origin = self._origin
        # The trace is bound before the first await because the kernel hands
        # the operation over the moment it dispatches the batch: from here on
        # nothing else accounts for it, and everything below — the
        # intermediate-text publish, before-tool hooks, the mutation lock —
        # can be cancelled. Binding writes nothing; the start marker is still
        # written where the call is dispatched.
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
            batch_index=self._intermediate_buffer.batch_id if self._intermediate_buffer is not None else None,
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

        # Publish the intermediate text accumulated by the result_hook BEFORE
        # InvocationToolCallStart so the TUI renders it first. Parallel tool
        # calls run this middleware concurrently (the kernel loop dispatches
        # the batch via asyncio.gather); the release holds every sibling until
        # the text is fully delivered (including stream consumers), because a
        # start that overtakes it lands the text mid-batch and the TUI splits
        # the tool group, orphaning already-rendered cards.
        await self._release_intermediate_text(origin, preamble)

        provider_call_id = get_provider_call_id(context)
        call_id = get_call_id(context) or uuid4().hex[:_SHORT_ID_LEN]
        tool_name = context.function.name
        args = context.arguments if is_mutable_tool_args(context.arguments) else {}

        # Record batch attribution for this tool call PRE-execution and
        # unconditionally — an interrupted, cancelled, or metadata-less call
        # must not lose its ``_batch_id``.  The batch_id increments once per
        # LLM response (intermediate-text boundary).  The engine drains the
        # records (``drain_batch_records``) to assign ``_batch_id`` to
        # session messages in post-processing, keyed by
        # (provider_call_id, tool_name) — never by position.
        if self._intermediate_buffer is not None:
            self._tool_batch_records.append(
                ToolBatchRecord(
                    provider_call_id=provider_call_id,
                    tool_name=tool_name,
                    tool_order=tool_order,
                    batch_id=self._intermediate_buffer.batch_id,
                )
            )
        # Stash the Chrys call_id in metadata so downstream middleware
        # (approval, sub-agent correlation) references the same id as the
        # published InvocationToolCallStart/InvocationToolCallResult events.
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

        # Serialize concurrent mutations to the same file. FunctionInvocationLayer
        # dispatches parallel tool calls via asyncio.gather(); without this
        # lock, record() for a later edit runs before record_after() for an
        # earlier one, breaking the _last_known_hash chain.
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
        """Core tool-call processing: mutation tracking, execution, event publishing."""
        is_shell = get_tool_kind(context.function) == KIND_SHELL
        is_sub_agent = get_tool_kind(context.function) == KIND_SUB_AGENT
        modified_args = (
            context.metadata.get(_APPROVAL_MODIFIED_ARGS_KEY) if isinstance(context.metadata, dict) else None
        )
        if isinstance(modified_args, dict):
            args = modified_args

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
                tool_operation_id=tool_operation_id(_meta),
            )

        tool_kind = get_tool_kind(context.function) or ""
        await self._bus.publish(
            InvocationToolCallStart(
                origin=origin,
                tool_name=tool_name,
                tool_kind=tool_kind,
                args=args,
                call_id=call_id,
                session_id=self._session_id,
            )
        )
        if self._on_start_published is not None:
            await self._on_start_published(provider_call_id)

        # Propagate parent call_id into the sub-agent's ``_invoke`` via
        # ContextVar so it can bind invocation_id ↔ parent call_id for the
        # TUI.  Reset on the way out — parallel sub-agent siblings each get
        # their own asyncio-task context, so the token would be auto-released
        # anyway, but explicit reset keeps non-task call sites clean.
        sub_agent_token = None
        sub_agent_metadata_token = None
        sub_agent_metadata = None
        if is_sub_agent:
            from chrys.foundation.util.sub_agent_context import (
                SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY,
                SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY,
                SubAgentParentResultMetadata,
                sub_agent_parent_call_id,
                sub_agent_parent_result_metadata,
            )

            raw_commit = context.metadata.get(SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY)
            sub_agent_token = sub_agent_parent_call_id.set(call_id)
            sub_agent_metadata = SubAgentParentResultMetadata(
                parent_provider_call_id=provider_call_id,
                parent_event_call_id=call_id,
                commit_interrupted_result=raw_commit if callable(raw_commit) else None,
            )
            sub_agent_metadata_token = sub_agent_parent_result_metadata.set(sub_agent_metadata)

        web_progress_token = None
        if get_tool_kind(context.function) in {KIND_WEB_SEARCH, KIND_WEB_FETCH}:

            async def _emit_web_progress(lines: list[str]) -> None:
                await self._bus.publish(
                    InvocationToolCallProgress(
                        origin=origin, tool_name=tool_name, call_id=call_id, session_id=self._session_id, lines=lines
                    )
                )

            web_progress_token = web_progress_callback.set(_emit_web_progress)

        # Set up progress streaming for shell tools
        progress_token = None
        shell_metadata_token = None
        shell_metadata: dict[str, object] | None = None
        generic_metadata: dict[str, object] = {}
        generic_metadata_token = tool_result_metadata.set(generic_metadata)
        payload_observation: dict[str, object] = {}
        payload_observation_token = tool_payload_observation.set(payload_observation)
        if is_shell:
            from chrys.service.tools.builtins.shell import shell_progress_callback, shell_result_metadata

            bus = self._bus
            sid = self._session_id

            async def _emit_progress(lines: list[str]) -> None:
                await bus.publish(
                    InvocationToolCallProgress(
                        origin=origin,
                        tool_name=tool_name,
                        call_id=call_id,
                        lines=lines,
                        session_id=sid,
                    )
                )

            progress_token = shell_progress_callback.set(_emit_progress)
            shell_metadata = {}
            shell_metadata_token = shell_result_metadata.set(shell_metadata)

        started_at = datetime.now(UTC)
        start = time.monotonic()
        error_text = ""
        errored = False
        raised_error_kind: str | None = None
        cancelled = False
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
                    batch_index=self._intermediate_buffer.batch_id if self._intermediate_buffer is not None else None,
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
            # CancelledError (BaseException) — the agent task was cancelled by
            # interrupt().  Don't publish InvocationToolCallResult; the TUI's
            # cancel_running_tools() already marks all running widgets.
            cancelled = True
            raise
        finally:
            reset_tool_operation(tool_operation_token)
            if web_progress_token is not None:
                web_progress_callback.reset(web_progress_token)
            if progress_token is not None:
                from chrys.service.tools.builtins.shell import shell_progress_callback

                shell_progress_callback.reset(progress_token)
            if shell_metadata_token is not None:
                from chrys.service.tools.builtins.shell import shell_result_metadata

                shell_result_metadata.reset(shell_metadata_token)
            tool_result_metadata.reset(generic_metadata_token)
            tool_payload_observation.reset(payload_observation_token)

            if sub_agent_token is not None:
                from chrys.foundation.util.sub_agent_context import sub_agent_parent_call_id

                sub_agent_parent_call_id.reset(sub_agent_token)
            if sub_agent_metadata_token is not None:
                from chrys.foundation.util.sub_agent_context import sub_agent_parent_result_metadata

                sub_agent_parent_result_metadata.reset(sub_agent_metadata_token)

            if not isinstance(context.metadata, dict):
                context.metadata = {}
            rejected = bool(context.metadata.get(_APPROVAL_REJECTED_KEY, False))
            finished_at = datetime.now(UTC)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            # A rejected call reports no duration to the UI — it never ran —
            # but the operation was open for the whole approval wait, and
            # that wait is the only thing that happened inside it.
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
                # The task is unwinding from CancelledError: no further
                # await is safe, so the operation closes without its ack.
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
                    args = final_tool_args(context, args)
                    if rejected and mut_ctx is not None:
                        # Rejected at approval, post-prepare: finalize is
                        # deliberately skipped (the tool never executed), so
                        # release the trace wrapper and close the observation
                        # window here.
                        abort_mutation_tracking(mut_ctx, self._mutation_coordinator)

                    # Post-execution mutation tracking
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
                    # Carry result metadata and per-call provenance (resolved from
                    # the final args, so rejected/blocked calls whose tool body
                    # never ran still get it) on this invocation's context. The
                    # kernel folds both onto the right Contents at construction —
                    # no post-run positional re-association.
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
                            tool_name=tool_name,
                            call_id=call_id,
                            result=result_text,
                            image_contents=result_images,
                            duration_ms=duration_ms,
                            session_id=self._session_id,
                            metadata=metadata,
                        )
                    )
                    if trajectory is not None:
                        # The kernel's ceiling bounds the result after this
                        # middleware returns — including whatever the hooks
                        # just appended — so measure what it will leave, or
                        # the record describes text the model never saw. The
                        # same call is what tells the observation it was cut.
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
                                cancelled=False,
                                rejected=rejected,
                                errored=errored,
                                result_metadata=metadata,
                            ),
                            duration_ms=elapsed_ms,
                            result_metadata=metadata,
                            error_kind=raised_error_kind,
                        )
                except BaseException:
                    # Interrupted while closing out a call that already ran:
                    # the operation is open, and only this path can close it.
                    if trajectory is not None:
                        trajectory.finished_soon(outcome=ToolOutcome.INTERRUPTED, duration_ms=elapsed_ms)
                    raise

    def drain_batch_records(self) -> list[ToolBatchRecord]:
        """Return and clear batch records (one per tool invocation, in dispatch order)."""
        records = list(self._tool_batch_records)
        self._tool_batch_records.clear()
        return records

    def snapshot_retry_state(self) -> ToolEventRetrySnapshot:
        """Capture records that predate the retrying Agent.run."""
        return ToolEventRetrySnapshot(
            batch_records=tuple(self._tool_batch_records),
            invocation_order=self._tool_invocation_order,
        )

    def restore_retry_state(self, snapshot: ToolEventRetrySnapshot) -> None:
        """Drop failed-attempt records while preserving the completed baseline."""
        self._tool_batch_records = list(snapshot.batch_records)
        self._tool_invocation_order = snapshot.invocation_order

    def clear_batch_records(self) -> None:
        """Clear recorded batch records (called on history rollback).

        A stale record surviving rollback carries a provider id that can
        collide with the retried attempt's calls (sequential providers
        reuse call_0-style ids per response) and would stamp wrong
        ``_batch_id``s after the retry.
        """
        self._tool_batch_records.clear()

    def reset_invocation_order(self) -> None:
        """Reset the per-run tool invocation order counter."""
        self._tool_invocation_order = 0


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
