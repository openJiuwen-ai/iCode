# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Post-run finalization for main-agent turns."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.settings_store import SettingsHandle
from chrys.foundation.events.types import UserInjectResult
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import EXECUTION_FAILED_MESSAGE, HistoryMarkerKind
from chrys.foundation.recovery import RecoveryPersistOutcome
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import TurnEndReason
from chrys.orchestration.engine.execution import CurrentRunScope
from chrys.orchestration.engine.run import sub_agent_coordination as sub_agents
from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
from chrys.orchestration.engine.state.machine import Trigger
from chrys.service.context.providers.history import PRE_OUTPUT_HISTORY_LEN_STATE_KEY
from chrys.service.trajectory.preparation import PreparationOutcome

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.kernel import Content, Message
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.state.session_writer import SessionWriter
    from chrys.orchestration.engine.trajectory import TrajectoryRecorder
    from chrys.service.agent_middleware.injection import QueuedInjection
    from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
    from chrys.service.session.history import SessionHistoryManager
    from chrys.service.session.persistence import SessionPersistence


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostRunOutcome:
    """Finalization result used by the runner's terminal-boundary cleanup."""

    failed: bool
    interrupted: bool
    completed_scope: CurrentRunScope | None


class TurnFinalizer:
    """Finalize one just-ended executor pass."""

    def __init__(
        self,
        *,
        current: CurrentAgent,
        session: ActiveSession,
        turn_state: TurnRuntimeState,
        writer: SessionWriter,
        history: SessionHistoryManager,
        trajectory_recorder: TrajectoryRecorder,
        fsm: EngineStateMachine,
        workspace_change_tracker: WorkspaceChangeTracker,
        settings_handle: SettingsHandle,
        bus: EventBus,
        persistence: SessionPersistence,
        on_successful_turn: Callable[[], None],
        hooks: TurnHookDispatcher,
    ) -> None:
        self._current = current
        self._session = session
        self._turn_state = turn_state
        self._writer = writer
        self._history = history
        self._trajectory_recorder = trajectory_recorder
        self._fsm = fsm
        self._workspace_change_tracker = workspace_change_tracker
        self._settings_handle = settings_handle
        self._bus = bus
        self._persistence = persistence
        self._on_successful_turn = on_successful_turn
        self._hooks = hooks

    async def finalize(self) -> PostRunOutcome:
        """Run post-execution fixup without starting pending retry dispatch."""
        completed_scope = self._turn_state.lease.current_run_scope
        loaded = self._current.require_loaded()
        failed = loaded.bindings.state.run_failed or loaded.bindings.state.was_interrupted
        interrupted = loaded.bindings.state.was_interrupted
        approval_decisions = loaded.bindings.approval.drain_decisions()
        metadata_start_index = _post_compaction_history_start_index(self._turn_state, self._current)
        # Taken once, by every turn: a later failed turn that ran no pass, such as
        # one interrupted before it started, keeps nothing of an earlier one.
        partial_answer, loaded.bindings.state.partial_answer = loaded.bindings.state.partial_answer, ()

        if failed:
            self._repair_ended_pass(approval_decisions, partial_answer, metadata_start_index)
            approval_decisions = []

        await self._drain_abandoned_injections()
        self._persist_history_metadata(
            approval_decisions=approval_decisions,
            metadata_start_index=metadata_start_index,
            failed=failed,
        )
        self._clear_parent_sub_agent_state()
        self._apply_terminal_history_state(failed=failed, interrupted=interrupted)
        turn_close_cancelled = await self._record_trajectory_turn_finished(failed=failed, interrupted=interrupted)
        response_outcome = "partial"
        drained_scopes: list[str] = []
        waited_hook_operation_ids: list[str] = []
        degraded = False
        try:
            if turn_close_cancelled:
                # The capture behind it is advisory and would hold shutdown for its
                # own timeout on an already-cancelled task, so it is skipped — and
                # skipped means there is no baseline to keep.
                self._workspace_change_tracker.capture_cancelled()
                baseline_cancelled = True
            else:
                baseline_cancelled, baseline_degraded = await self._capture_workspace_baseline()
                degraded = degraded or baseline_degraded
            if turn_close_cancelled or baseline_cancelled:
                # A cancellation (shutdown's post-run fallback) landed on the
                # advisory capture. Shutdown suppresses its own trailing save
                # after that fallback, so the critical save must still run here.
                try:
                    saved = await self._writer.save_current_session()
                    degraded = degraded or not saved
                    if saved:
                        self._turn_state.lease.record_current_run_final_save()
                    await self._record_trajectory_after_save()
                finally:
                    degraded = not self._run_success_callback(failed=failed) or degraded
                response_outcome = "cancelled"
                raise asyncio.CancelledError
            try:
                save_degraded = await self._save_and_drain_hooks(
                    failed=failed,
                    drained_scopes=drained_scopes,
                    waited_hook_operation_ids=waited_hook_operation_ids,
                )
                degraded = degraded or save_degraded
            finally:
                # After the save so callbacks observe the persisted turn — a
                # brand-new session's first save must exist before side effects
                # keyed on session.json (e.g. the session-title updater) run.
                degraded = not self._run_success_callback(failed=failed) or degraded
            response_outcome = "partial" if degraded else "settled"
        except asyncio.CancelledError:
            response_outcome = "cancelled"
            raise
        finally:
            if response_outcome == "cancelled":
                self._trajectory_recorder.turn_response_settled_soon(
                    outcome=response_outcome,
                    drained_scopes=drained_scopes,
                    waited_hook_operation_ids=waited_hook_operation_ids,
                )
            else:
                await self._trajectory_recorder.turn_response_settled(
                    outcome=response_outcome,
                    drained_scopes=drained_scopes,
                    waited_hook_operation_ids=waited_hook_operation_ids,
                )

        return PostRunOutcome(failed=failed, interrupted=interrupted, completed_scope=completed_scope)

    async def settle_steered_pass(
        self,
        inputs: Sequence[Message],
        *,
        withdraw: Callable[[tuple[str, ...]], Awaitable[None]],
    ) -> None:
        """Settle a pass a steer aborted, so the next pass of the same turn goes on from what it committed.

        History is repaired as for a failed pass, then the answer it was
        writing is dropped, as a Continue drops it: *withdraw* hears the
        segments it was shown in. The turn goes on, so nothing that ends it
        runs: messages still queued wait for the next pass, and no marker,
        state transition, save, hook or trajectory turn end follows.
        """
        loaded = self._current.require_loaded()
        recorder = loaded.loop_recorder
        committed = recorder.committed_count > 0
        # One synchronous stretch, so no recovery checkpoint sees it half done.
        # The pass's input first: its loop messages merge after it.
        self._history.restore_unsent_input(inputs)
        approval_decisions = loaded.bindings.approval.drain_decisions()
        metadata_start_index = _post_compaction_history_start_index(self._turn_state, self._current)
        partial_answer, loaded.bindings.state.partial_answer = loaded.bindings.state.partial_answer, ()
        self._repair_ended_pass(approval_decisions, partial_answer, metadata_start_index)
        self._persist_history_metadata(approval_decisions=[], metadata_start_index=metadata_start_index, failed=True)
        withdrawn = self._history.remove_trailing_answer()
        # History now holds the pass's loop messages: a checkpoint must not merge copies of them again.
        recorder.reset()
        self._clear_parent_sub_agent_state()
        if self._fsm.is_awaiting_sub_agents():
            self._fsm.try_transition(Trigger.SUB_AGENT_RESOLVED)
        # The provider state the pass left is incomplete: the next pass sends local history.
        loaded.bindings.backend.service_session_id = ""
        # The next pass's recovered messages go after this one's settled work.
        self._turn_state.history_start_index = len(self._history.messages)
        if withdrawn:
            await withdraw(withdrawn)
        if committed:
            # The recorder's own barrier no longer counts this work: persist it
            # before the next request, as that barrier would.
            outcome = await self._writer.persist_barrier()
            if outcome is not RecoveryPersistOutcome.PERSISTED:
                logger.debug("Steered pass's committed work not persisted before the next pass: %s", outcome)

    def _repair_ended_pass(
        self, approval_decisions: list[Any], partial_answer: Sequence[Content], metadata_start_index: int
    ) -> None:
        """Keep what a pass that ended early committed, and the answer it showed."""
        # The pass-start boundary keeps a retried/resumed pass's recovered
        # messages AFTER retained work from an earlier same-turn pass.
        # A failed pass can finalize only after construction installs its recorder.
        self._history.merge_loop_messages(
            self._current.require_loaded().loop_recorder,
            insert_index=metadata_start_index,
        )
        self._history.persist_approval_decisions(
            approval_decisions,
            start_index=metadata_start_index,
        )
        self._history.trim_to_last_complete_tool_results()
        # What the pass showed stays, also an answer cut off mid-stream.
        self._history.keep_partial_answer(partial_answer)

    async def _capture_workspace_baseline(self) -> tuple[bool, bool]:
        """Capture after the terminal transition and before the persisted save.

        Returns ``(cancelled, degraded)``. The capture is advisory but the
        save behind it is not, so cancellation is absorbed long enough for
        the caller to finish the critical save.
        """
        tracker = self._workspace_change_tracker
        if not self._settings_handle.settings.workspace_change_notice:
            tracker.invalidate()
            return False, False
        try:
            await asyncio.wait_for(
                asyncio.to_thread(tracker.capture_baseline, self._session.turn_number),
                timeout=15.0,
            )
        except asyncio.CancelledError:
            tracker.capture_cancelled()
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            return True, False
        except TimeoutError:
            tracker.capture_timed_out(self._session.turn_number)
            logger.debug("Workspace baseline capture exceeded its deadline")
            return False, True
        except Exception:
            tracker.invalidate()
            logger.debug("Workspace baseline capture failed", exc_info=True)
            return False, True
        return False, False

    async def _drain_abandoned_injections(self) -> None:
        """Close and publish unconsumed injections unless shutdown owns notification."""
        abandoned: list[QueuedInjection] = self._current.require_loaded().injection.drain_pending()
        for injection in abandoned:
            if injection.preparation is not None:
                injection.preparation.finished_soon(
                    outcome=PreparationOutcome.TARGET_STALE,
                    target_turn_id=injection.target_turn_id,
                )
        if self._session.shutting_down:
            return
        for injection in abandoned:
            await self._bus.publish(
                UserInjectResult(
                    text=injection.text,
                    consumed=False,
                    created_at=injection.created_at,
                    injection_id=injection.injection_id,
                    session_id=self._session.session_id,
                )
            )

    def _persist_history_metadata(
        self,
        *,
        approval_decisions: list[Any],
        metadata_start_index: int,
        failed: bool,
    ) -> None:
        """Attach post-run metadata after any failed-run history repair."""
        loaded = self._current.require_loaded()
        batch_records = loaded.bindings.tool_events.drain_batch_records()
        tool_call_count = sum(
            1
            for message in self._history.messages
            if message.role == "assistant"
            and any(content.type == "function_call" and not content.informational_only for content in message.contents)
        )
        logger.debug(
            "_post_run: batch_records=%d tool_call_msgs=%d total_msgs=%d failed=%s",
            len(batch_records),
            tool_call_count,
            len(self._history.messages),
            failed,
        )
        batch_anchors = self._history.persist_batch_ids(batch_records)
        if loaded.intermediate_texts:
            self._history.persist_intermediate_texts(dict(loaded.intermediate_texts), batch_anchors)
            loaded.intermediate_texts.clear()
        self._history.persist_approval_decisions(
            approval_decisions,
            start_index=metadata_start_index,
        )
        if loaded.consumed_injections:
            self._history.persist_consumed_injections(loaded.consumed_injections)
            loaded.consumed_injections.clear()
        self._history.backfill_missing_created_at(start_index=metadata_start_index)
        _clear_post_compaction_history_start_index(self._current)

    def _clear_parent_sub_agent_state(self) -> None:
        """Clear parent sub-agent pause state before terminal markers are inserted."""
        sub_agents.clear_parent_paused_state(self._turn_state, self._history)

    def _apply_terminal_history_state(self, *, failed: bool, interrupted: bool) -> None:
        """Apply FSM transitions and terminal history markers."""
        if failed:
            loaded = self._current.require_loaded()
            if interrupted:
                self._history.insert_interrupted_marker()
                self._fsm.try_transition(Trigger.RUN_INTERRUPTED)
            else:
                last_error = loaded.bindings.state.last_error
                if last_error:
                    self._history.insert_interrupted_marker(reason=last_error, source="error")
                else:
                    self._history.insert_interrupted_marker(
                        reason=format_message(EXECUTION_FAILED_MESSAGE.bind()),
                        source="error",
                        status_code=HistoryMarkerKind.STATUS_EXECUTION_FAILED,
                    )
                self._fsm.try_transition(Trigger.RUN_FAILED)
            # A failed/interrupted Responses turn can leave KernelConversation
            # holding a service id from an incomplete provider-side state. Drop it
            # so retry/resume replays local recovery history instead of skipping it.
            loaded.bindings.backend.service_session_id = ""
        else:
            self._history.remove_all_status_markers()
            self._fsm.try_transition(Trigger.RUN_COMPLETED)

        self._history.insert_turn_marker(self._turn_state.pass_started_at)
        if self._session.mutation_tracker is not None:
            self._session.mutation_tracker.cleanup_unused_snapshots()

    def _run_success_callback(self, *, failed: bool) -> bool:
        """Run the successful-turn callback after markers and cleanup."""
        if failed:
            return True
        try:
            self._on_successful_turn()
        except Exception:
            logger.debug("Failed to run successful turn callback", exc_info=True)
            return False
        return True

    async def _save_and_drain_hooks(
        self,
        *,
        failed: bool,
        drained_scopes: list[str],
        waited_hook_operation_ids: list[str],
    ) -> bool:
        """Save the session, fire after-turn hooks, then drain turn hooks."""
        saved = await self._writer.save_current_session()
        if saved:
            self._turn_state.lease.record_current_run_final_save()
        await self._record_trajectory_after_save()
        with trajectory_scope(self._trajectory_recorder.finished_turn_context()):
            await self._hooks.fire_after_turn(failed=failed)
        if self._session.hook_manager is not None:
            drained_scopes.append("turn")
            try:
                await self._session.hook_manager.drain_turn()
            finally:
                waited_hook_operation_ids.extend(self._session.hook_manager.active_turn_drain_operation_ids)
        return not saved

    async def _record_trajectory_turn_finished(self, *, failed: bool, interrupted: bool) -> bool:
        """Close the pass's trajectory turn right after its terminal marker landed.

        Returns True when a cancellation landed on the write acknowledgement —
        a slow backend can hold it until shutdown gives up on the run task.
        The terminal's line is committed by then, so the turn is finished in
        the log while the save behind this call is the only thing that would
        make it finished in the session. The cancellation is absorbed here
        (and the task uncancelled) exactly as the workspace capture does, so
        the caller can complete that save before completing it.
        """
        if interrupted:
            end_reason = TurnEndReason.INTERRUPTED
        elif failed:
            end_reason = TurnEndReason.ERROR
        else:
            end_reason = TurnEndReason.COMPLETED
        try:
            await self._trajectory_recorder.turn_finished(end_reason=end_reason)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            return True
        return False

    async def _record_trajectory_after_save(self) -> None:
        """Derive the turn's mutation summary from the saved state, then checkpoint the log."""
        recorder = self._trajectory_recorder
        tracker = self._session.mutation_tracker
        if tracker is not None:
            try:
                summary = tracker.get_turn_file_summary(self._session.turn_number)
            except Exception:
                logger.debug("Mutation summary unavailable for turn %d", self._session.turn_number, exc_info=True)
                summary = {}
            await recorder.mutation_summary(
                summary, checkpoint=self._persistence.checkpoint_for(self._session.session_id)
            )
        await recorder.checkpoint()


def _post_compaction_history_start_index(turn_state: TurnRuntimeState, current: CurrentAgent) -> int:
    """Return the current-run metadata floor after request-time history compression."""
    start_index = turn_state.history_start_index
    loaded = current.loaded
    if loaded is None:
        return start_index
    try:
        history_state = loaded.bindings.backend.history_state
    except AttributeError:
        return start_index
    pre_output_len = history_state.get(PRE_OUTPUT_HISTORY_LEN_STATE_KEY)
    return pre_output_len if isinstance(pre_output_len, int) else start_index


def _clear_post_compaction_history_start_index(current: CurrentAgent) -> None:
    """Clear the transient request-time compression metadata floor."""
    loaded = current.loaded
    if loaded is None:
        return
    try:
        history_state = loaded.bindings.backend.history_state
    except AttributeError:
        return
    history_state.pop(PRE_OUTPUT_HISTORY_LEN_STATE_KEY, None)


def _expire_current_run_scope(
    turn_state: TurnRuntimeState, current: CurrentAgent, scope: CurrentRunScope | None
) -> None:
    """Clear a completed current-run scope and expire its service-owned reminder scope."""
    if scope is None:
        return
    turn_state.lease.clear_current_run_scope(scope)
    loaded = current.loaded
    if loaded is not None:
        loaded.reminder_middleware.expire_current_run_scope(scope.reminder_scope)
