# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fresh and retry executor passes for main-agent turns."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.turns import current_turn_start, is_continuation_message
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.orchestration.engine import trajectory as trajectory_recorder
from chrys.orchestration.engine.rollback import capture_snapshot_writer
from chrys.orchestration.engine.run.finalizer import PostRunOutcome, TurnFinalizer, _expire_current_run_scope
from chrys.orchestration.engine.run.input_refs import format_skill_reference_reminder, parse_skill_reference
from chrys.orchestration.engine.run.prompt_content import PromptContentPreparer
from chrys.orchestration.engine.run.retry import RetryCoordinator
from chrys.orchestration.engine.run.runtime_skills import RuntimeSkillRefresher
from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
from chrys.orchestration.invoker.contracts import OverlappingRun, PreparedClosed, StaleContinuation, UnsupportedRequest
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationScope, PreparationTrace

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import SettingsHandle
    from chrys.foundation.events.types import RuntimeSkillDetails
    from chrys.orchestration.engine.execution import CurrentRunInjectionWindow, CurrentRunScope
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.trajectory import TrajectoryRecorder
    from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
    from chrys.service.session.history import SessionHistoryManager


logger = logging.getLogger(__name__)


class TurnRunner:
    """Execute fresh and retry passes while preserving turn-layer ordering."""

    def __init__(
        self,
        *,
        current: CurrentAgent,
        session: ActiveSession,
        turn_state: TurnRuntimeState,
        history: SessionHistoryManager,
        workspace_change_tracker: WorkspaceChangeTracker,
        settings_handle: SettingsHandle,
        trajectory_recorder: TrajectoryRecorder,
        fsm: EngineStateMachine,
        on_turn_started: Callable[[], None],
        finalizer: TurnFinalizer,
        hooks: TurnHookDispatcher,
        skills: RuntimeSkillRefresher,
        content: PromptContentPreparer,
        retry_factory: Callable[[], RetryCoordinator],
    ) -> None:
        self._current = current
        self._session = session
        self._turn_state = turn_state
        self._history = history
        self._workspace_change_tracker = workspace_change_tracker
        self._settings_handle = settings_handle
        self._trajectory_recorder = trajectory_recorder
        self._fsm = fsm
        self._on_turn_started = on_turn_started
        self._finalizer = finalizer
        self._hooks = hooks
        self._skills = skills
        self._content = content
        self._retry_factory = retry_factory
        # Identity of the opening message, allocated before the message exists.
        self._opening_item_id: str | None = None

    async def run_fresh(
        self,
        text: str,
        created_at: datetime | str | None = None,
        contents: list[Any] | None = None,
        *,
        run_scope: CurrentRunScope | None = None,
        injection_window: CurrentRunInjectionWindow | None = None,
        admission_preparation: PreparationTrace | None = None,
        binding_failure: Exception | None = None,
    ) -> None:
        """Execute a fresh agent turn and finalize it."""
        _ = injection_window
        self._current.require_loaded().bindings.inputs.begin_invocation()
        if contents is None:
            contents = await self._prepare_user_contents(text)
            if contents is None:
                if admission_preparation is not None:
                    await admission_preparation.finished(outcome=PreparationOutcome.PREPARATION_FAILED)
                return

        try:
            await self.pre_run(
                reset_batch_id=True,
                preparation_scope_operation_id=self._preparation_operation_id(admission_preparation),
            )
            if admission_preparation is not None:
                await admission_preparation.finished(outcome=PreparationOutcome.FRESH_TURN)
        except asyncio.CancelledError:
            if admission_preparation is not None:
                admission_preparation.finished_soon(outcome=PreparationOutcome.CANCELLED)
            raise
        except BaseException:
            if admission_preparation is not None:
                admission_preparation.finished_soon(outcome=PreparationOutcome.PREPARATION_FAILED)
            raise
        preamble = self._open_turn_preamble()
        try:
            if preamble is not None:
                await preamble.started()
                self._bind_turn_preamble(preamble)
            await self._fire_before_turn(
                text,
                target_operation_id=preamble.operation_id
                if preamble is not None and preamble.start_committed
                else None,
            )
            await self._compute_workspace_notice(is_retry=False)
            write_rollback_snapshot = capture_snapshot_writer(self._session, self._settings_handle)
            await asyncio.to_thread(write_rollback_snapshot)
            await self._refresh_runtime_skills(update_active_turn=False)
            self._queue_skill_reference_reminder(text, for_next_turn=True)
            if self._current.loaded is not None:
                if run_scope is not None:
                    self._current.require_loaded().reminder_middleware.prepare_turn(
                        reminder_scope=run_scope.reminder_scope,
                        usage=self._session.runtime_meta.last_usage_details or None,
                    )
                else:
                    self._current.require_loaded().reminder_middleware.prepare_turn(
                        usage=self._session.runtime_meta.last_usage_details or None
                    )
            self._current.require_loaded().bindings.approval.set_user_messages([text] if text else [])
            self._turn_state.set_current_input(text, contents, created_at)
            if preamble is not None:
                await preamble.finished(outcome=PreparationOutcome.HANDOFF)
        except asyncio.CancelledError:
            if preamble is not None:
                preamble.finished_soon(outcome=PreparationOutcome.INTERRUPTED)
            raise
        except BaseException:
            if preamble is not None:
                preamble.finished_soon(outcome=PreparationOutcome.FAILED)
            raise
        try:
            # Stop may arrive while pre-executor work (notably the rollback
            # snapshot) is awaiting worker I/O.  Consume only a cancellation
            # bound to this exact run task so a late Stop from an older task
            # cannot suppress the next turn.
            if binding_failure is not None:
                self._record_admission_failure(binding_failure)
            elif self._turn_state.lease.consume_pre_executor_interrupt():
                self._current.require_loaded().bindings.record_pre_run_interrupt()
            else:
                self._current.require_loaded().bindings.record_outcome(
                    await self._current.require_loaded().bindings.backend.run(
                        self._current.require_loaded().bindings.inputs.fresh_request(contents, created_at=created_at)
                    )
                )
        except (StaleContinuation, OverlappingRun, PreparedClosed, UnsupportedRequest) as exc:
            self._record_admission_failure(exc)
        finally:
            if run_scope is not None:
                self._turn_state.lease.close_injection_admission(run_scope)
            # The tracker was drained at prepare_turn; any pass ending before
            # a model request carried the notice (Stop, load/hook failure,
            # cancellation) must hand it back for the next turn.
            self._requeue_undelivered_file_change()
        if (
            self._current.require_loaded().bindings.state.run_failed
            or self._current.require_loaded().bindings.state.was_interrupted
        ):
            self._history.ensure_user_message(
                text, created_at=created_at, contents=contents, item_id=self._opening_item_id
            )
        self._tag_consumed_profile_switch()
        await self.finalize_current_run()

    async def run_retry(
        self,
        additional_text: str = "",
        created_at: datetime | str | None = None,
        *,
        run_scope: CurrentRunScope | None = None,
        injection_window: CurrentRunInjectionWindow | None = None,
        admission_preparation: PreparationTrace | None = None,
        binding_failure: Exception | None = None,
    ) -> None:
        """Resume the agent from current state and finalize it."""
        _ = injection_window
        try:
            await self.pre_run(
                reset_batch_id=False,
                is_retry=True,
                has_opening_input=bool(additional_text),
                preparation_scope_operation_id=self._preparation_operation_id(admission_preparation),
            )
            if admission_preparation is not None:
                await admission_preparation.finished(outcome=PreparationOutcome.RETRY_TURN)
        except asyncio.CancelledError:
            if admission_preparation is not None:
                admission_preparation.finished_soon(outcome=PreparationOutcome.CANCELLED)
            raise
        except BaseException:
            if admission_preparation is not None:
                admission_preparation.finished_soon(outcome=PreparationOutcome.PREPARATION_FAILED)
            raise
        preamble = self._open_turn_preamble()
        try:
            if preamble is not None:
                await preamble.started()
                self._bind_turn_preamble(preamble)
            await self._fire_before_turn(
                additional_text,
                is_retry=True,
                target_operation_id=preamble.operation_id
                if preamble is not None and preamble.start_committed
                else None,
            )
            await self._compute_workspace_notice(is_retry=True)
            if self._current.loaded is not None:
                if run_scope is not None:
                    self._current.require_loaded().reminder_middleware.prepare_turn(
                        reminder_scope=run_scope.reminder_scope,
                        usage=self._session.runtime_meta.last_usage_details or None,
                        preserve_last_words=True,
                        preserve_turn_reminders=True,
                    )
                else:
                    self._current.require_loaded().reminder_middleware.prepare_turn(
                        usage=self._session.runtime_meta.last_usage_details or None,
                        preserve_last_words=True,
                        preserve_turn_reminders=True,
                    )
            approval_context = self._retry_approval_context_messages(additional_text)
            if approval_context:
                self._current.require_loaded().bindings.approval.set_user_messages(approval_context)
            if additional_text:
                # Retry guidance is user-authored MID-TURN input: a recovery
                # checkpoint must re-create it flagged (kind-aware), or a crash
                # during a guided retry restores unflagged guidance that opens a
                # pseudo-turn on reload.
                self._turn_state.set_current_input(additional_text, None, created_at, kind="injected")
            else:
                # Empty-input continuation has no current input. The opener-replay
                # branch re-registers its popped anchor at pop time.
                self._turn_state.clear_current_input()
            if preamble is not None:
                await preamble.finished(outcome=PreparationOutcome.HANDOFF)
        except asyncio.CancelledError:
            if preamble is not None:
                preamble.finished_soon(outcome=PreparationOutcome.INTERRUPTED)
            raise
        except BaseException:
            if preamble is not None:
                preamble.finished_soon(outcome=PreparationOutcome.FAILED)
            raise
        try:
            if self._turn_state.lease.consume_pre_executor_interrupt():
                self._current.require_loaded().bindings.record_pre_run_interrupt()
                if additional_text:
                    self._history.ensure_user_message(
                        additional_text, created_at=created_at, kind="injected", item_id=self._opening_item_id
                    )
            else:
                async with self._current.require_loaded().bindings.inputs.retry_request(
                    additional_text=additional_text, created_at=created_at
                ) as request:
                    if request is not None:
                        try:
                            if binding_failure is not None:
                                self._record_admission_failure(binding_failure)
                            else:
                                self._current.require_loaded().bindings.record_outcome(
                                    await self._current.require_loaded().bindings.backend.run(request)
                                )
                        except (StaleContinuation, OverlappingRun, PreparedClosed, UnsupportedRequest) as exc:
                            # Exit normally so retry_request restores its input
                            # with the existing kind-aware post-yield fallback.
                            self._record_admission_failure(exc)
        except (StaleContinuation, OverlappingRun, PreparedClosed, UnsupportedRequest) as exc:
            self._record_admission_failure(exc)
        finally:
            if run_scope is not None:
                self._turn_state.lease.close_injection_admission(run_scope)
            self._requeue_undelivered_file_change()
        state = self._current.require_loaded().bindings.state
        if additional_text and (state.run_failed or state.was_interrupted):
            # retry_request restores guidance only after its yield. A rejection
            # at its entrance validation (owner closed between promotion and
            # bind, or any other pre-yield admission failure) never reaches
            # that fallback, so mirror the fresh path here. ensure_user_message
            # is kind-aware and scoped to the current turn, so the post-yield
            # and pre-executor-interrupt appends stay single-copy.
            self._history.ensure_user_message(
                additional_text, created_at=created_at, kind="injected", item_id=self._opening_item_id
            )
        self._tag_consumed_profile_switch()
        await self.finalize_current_run()

    def _record_admission_failure(self, error: Exception) -> None:
        """Reject once, then use the ordinary Turn finalizer/save/lease release."""
        state = self._current.require_loaded().bindings.state
        state.run_failed = True
        state.last_error = str(error)
        logger.error("Turn admission failed: %s", error)

    async def finalize_current_run(self) -> PostRunOutcome:
        """Finalize the just-ended executor pass and close the terminal boundary."""
        try:
            outcome = await self._finalizer.finalize()
            self._complete_finalized_run(outcome)
        except BaseException:
            self._clear_current_input()
            raise
        return outcome

    async def pre_run(
        self,
        *,
        reset_batch_id: bool,
        is_retry: bool = False,
        has_opening_input: bool = True,
        preparation_scope_operation_id: str | None = None,
    ) -> None:
        """Common Turn setup before submitting a request to KernelConversation.

        ``has_opening_input`` says whether this pass sends a user-authored
        message (a fresh prompt or retry guidance) — only then does the pass
        have an opening item to name in its ``turn.started`` event.
        """
        self._turn_state.lease.advance_conversation_revision()
        # Fresh and retry turns alike make this launch's surface the session's last one.
        self._session.mark_surface()
        self._fire_turn_started()
        self._current.require_loaded().consumed_injections.clear()
        self._current.require_loaded().intermediate_texts.clear()
        if not is_retry:
            self._session.turn_number += 1
        turn_counter = self._current.require_loaded().bindings.backend.history_state.get("turn_counter", 0)
        self._turn_state.history_start_index = len(self._history.messages)
        logger.debug(
            "pre_run: is_retry=%s turn_number=%d turn_counter=%d fsm=%s",
            is_retry,
            self._session.turn_number,
            turn_counter,
            self._fsm.state.name,
        )
        self._current.require_loaded().bindings.reset_counters(reset_batch_id=reset_batch_id)
        if self._current.loaded is not None and self._current.require_loaded().loop_recorder is not None:
            self._current.require_loaded().loop_recorder.reset()
        if self._session.mutation_tracker is not None:
            if is_retry and self._session.mutation_tracker.current_turn is not None:
                self._session.mutation_tracker.reset_file_cache()
            else:
                self._session.mutation_tracker.start_turn(self._session.turn_number)
        await self._record_trajectory_turn_started(
            is_retry=is_retry,
            has_opening_input=has_opening_input,
            preparation_scope_operation_id=preparation_scope_operation_id,
        )

    async def _record_trajectory_turn_started(
        self,
        *,
        is_retry: bool,
        has_opening_input: bool,
        preparation_scope_operation_id: str | None,
    ) -> None:
        """Open the pass's trajectory turn and bind the run context on the executor."""
        recorder = self._trajectory_recorder
        self._opening_item_id = new_analytics_id() if has_opening_input else None
        await recorder.turn_started(
            turn_number=self._session.turn_number,
            is_retry=is_retry,
            agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
            model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
            primary_cwd=self._session.workspace.primary_cwd if self._session.workspace is not None else "",
            history_state=self._current.require_loaded().bindings.backend.history_state,
            opening_item_id=self._opening_item_id,
            preparation_scope_operation_id=preparation_scope_operation_id,
        )
        context = recorder.context()
        if context is not None:
            context = context.with_exchange_facts(
                trajectory_recorder.exchange_facts(
                    agent_profile_name=self._session.agent_profile.name
                    if self._session.agent_profile is not None
                    else "",
                    agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
                    model_profile=self._current.manifest.active_profile,
                    model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
                )
            )
        self._current.require_loaded().bindings.trajectory_context = context
        self._current.require_loaded().bindings.inputs.set_opening_item_id(self._opening_item_id)

    @staticmethod
    def _preparation_operation_id(preparation: PreparationTrace | None) -> str | None:
        """Return a committed pre-turn operation id for the turn-start join."""
        if preparation is None or not preparation.start_committed:
            return None
        return preparation.operation_id

    async def _prepare_user_contents(self, text: str) -> list[Any] | None:
        """Return prepared user-message contents or publish a recoverable attachment error."""
        result = await self._content.prepare_fresh(text)
        return None if result is None else result.contents

    def _fire_turn_started(self) -> None:
        """Run the turn-started callback; failures never block the run."""
        try:
            self._on_turn_started()
        except Exception:
            logger.debug("Failed to run turn started callback", exc_info=True)

    def _open_turn_preamble(self) -> PreparationTrace | None:
        return PreparationTrace.open(
            scope=PreparationScope.TURN_PREAMBLE,
            phase="turn_dispatch",
            context=self._current.require_loaded().bindings.trajectory_context,
        )

    def _bind_turn_preamble(self, preamble: PreparationTrace) -> None:
        if not preamble.start_committed:
            return
        context = self._current.require_loaded().bindings.trajectory_context
        if context is not None:
            self._current.require_loaded().bindings.trajectory_context = context.with_turn_preamble(
                preamble.operation_id
            )

    async def _fire_before_turn(
        self,
        user_text: str,
        *,
        is_retry: bool = False,
        target_operation_id: str | None = None,
    ) -> None:
        """Publish a ``before_turn`` hook event."""
        await self._hooks.fire_before_turn(
            user_text,
            is_retry=is_retry,
            target_operation_id=target_operation_id,
        )

    async def _compute_workspace_notice(self, *, is_retry: bool) -> None:
        """Compute advisory boundary state before reminder preparation."""
        if not self._settings_handle.settings.workspace_change_notice:
            return
        turn_id = self._session.turn_number
        previous_turn = None if is_retry else turn_id - 1
        overlap_turn = turn_id if is_retry else turn_id - 1
        latest_agent_turn = turn_id if is_retry else turn_id - 1
        cwd = self._session.workspace.primary_cwd if self._session.workspace is not None else ""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._workspace_change_tracker.compute_turn_notice,
                    turn_id=turn_id,
                    mutation_tracker=self._session.mutation_tracker,
                    agent_turn_id=previous_turn,
                    overlap_turn_id=overlap_turn,
                    cwd=cwd,
                    max_entries=self._settings_handle.settings.workspace_change_notice_max_entries,
                    recovered=self._session.recovered_from_sidecar,
                    latest_agent_turn=latest_agent_turn,
                ),
                timeout=15.0,
            )
        except asyncio.CancelledError:
            self._workspace_change_tracker.notice_cancelled()
            raise
        except TimeoutError:
            # The turn continues to finalization, whose fresh baseline
            # capture absorbs whatever this comparison would have
            # reported — leave a persistent caveat, never absorb silently.
            self._workspace_change_tracker.notice_timed_out()
            logger.debug("Workspace notice computation exceeded its deadline")
        except Exception:
            self._workspace_change_tracker.clear_boundary_notice()
            logger.debug("Workspace notice computation failed", exc_info=True)

    def _requeue_undelivered_file_change(self) -> None:
        """Return a drained notice that no model request received."""
        if self._current.loaded is None:
            return
        notice = self._current.require_loaded().reminder_middleware.take_undelivered_file_change()
        if notice:
            self._workspace_change_tracker.requeue_notice(
                notice,
                cwd=self._session.workspace.primary_cwd if self._session.workspace is not None else None,
            )

    async def _refresh_runtime_skills(self, *, update_active_turn: bool = False) -> None:
        """Refresh runtime skills and optionally update the active reminder snapshot."""
        await self._skills.refresh(update_active_turn=update_active_turn)

    def _retry_approval_context_messages(self, additional_text: str) -> list[str]:
        """Return current-turn user texts approval should see during a retry."""
        messages = self._current_turn_user_messages()
        stripped = additional_text.strip()
        if stripped:
            messages.append(stripped)
        if messages:
            return messages
        latest = self._latest_user_message()
        return [latest] if latest else []

    def _current_turn_user_messages(self) -> list[str]:
        """Return user messages after the last turn marker.

        Feeds the approval-judge context: synthetic ``continue`` nudges are
        skipped — they are orchestration placeholders, not user input, and must
        not be presented to the judge as such.  Injections and guidance
        stay: they ARE user input.
        """
        history_messages = self._history.messages
        start = current_turn_start(history_messages)

        messages: list[str] = []
        for message in history_messages[start:]:
            if message.role == "user" and not is_continuation_message(message):
                text = (message.text or "").strip()
                if text:
                    messages.append(text)
        return messages

    def _latest_user_message(self) -> str:
        """Return the latest REAL user message in the full history.

        Fallback leg of the approval-judge context: skips synthetic
        ``continue`` nudges so a synthetic-only current region surfaces the
        last real user message (typically the interrupted turn's opener)
        instead of a fabricated ``continue`` request.
        """
        for message in reversed(self._history.messages):
            if message.role == "user" and not is_continuation_message(message):
                text = (message.text or "").strip()
                if text:
                    return text
        return ""

    def _tag_consumed_profile_switch(self) -> None:
        """Tag the last user message when the reminder middleware consumed a profile switch."""
        if self._current.loaded is None or not self._current.require_loaded().reminder_middleware.consumed_switch_to:
            return
        self._history.tag_last_user_message(
            HistoryMarkerKind.PROFILE_SWITCH_TO_KEY,
            self._current.require_loaded().reminder_middleware.consumed_switch_to,
        )

    def _queue_skill_reference_reminder(self, text: str, *, for_next_turn: bool) -> None:
        """Queue a system reminder when *text* starts with a loaded skill reference."""
        if self._current.loaded is None:
            return
        reminder = self._skill_reference_reminder(text)
        if reminder is None:
            return
        self._current.require_loaded().reminder_middleware.queue_hook_reminders(
            [reminder],
            for_next_turn=for_next_turn,
        )

    def _skill_reference_reminder(
        self,
        text: str,
        *,
        skill_details: list[RuntimeSkillDetails] | None = None,
    ) -> str | None:
        """Return the skill-reference reminder for *text*, if it names a loaded skill."""
        reference = parse_skill_reference(text, skill_details or self._current.manifest.runtime_details.skill_details)
        if reference is None:
            return None
        # AIxCoding telemetry: input-trigger report for a resolved slash skill reference.
        from chrys.aixcoding.telemetry.subscriber import record_skill_invocation

        record_skill_invocation(reference.skill.name, self._session.session_id)
        return format_skill_reference_reminder(reference)

    def _complete_finalized_run(self, outcome: PostRunOutcome) -> None:
        """Synchronously finish retry dispatch, scope expiry, and recovery cleanup."""
        task_before_pending_retry = self._turn_state.lease.run_task
        self._retry_factory().start_pending_retry_if_due()
        self._turn_state.lease.discard_pre_executor_interrupt(task_before_pending_retry)
        task_after_pending_retry = self._turn_state.lease.run_task
        retry_dispatched = (
            task_after_pending_retry is not None
            and task_after_pending_retry is not task_before_pending_retry
            and not task_after_pending_retry.done()
        )
        if not retry_dispatched and not outcome.failed:
            _expire_current_run_scope(self._turn_state, self._current, outcome.completed_scope)
        self._clear_current_input()

    def _clear_current_input(self) -> None:
        """Clear current-turn recovery input after run finalization."""
        self._turn_state.clear_current_input()
