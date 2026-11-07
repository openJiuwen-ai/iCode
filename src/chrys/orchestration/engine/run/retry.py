# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retry admission and pending-retry dispatch for main-agent turns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from chrys.foundation.events.types import Error, UserRetry, Warning
from chrys.foundation.i18n import msg
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.turns import current_turn_start, is_continuation_message
from chrys.orchestration.engine.execution import (
    BARE_RETRY,
    CurrentRunInjectionWindow,
    CurrentRunScope,
    PreAdmissionPreparationEntry,
    PreAdmissionPreparationTracker,
    PromptAdmissionScope,
    RetryInput,
)
from chrys.orchestration.engine.run.active_injection import ActiveTurnInjector
from chrys.orchestration.engine.run.input_refs import format_skill_reference_reminder, parse_skill_reference
from chrys.orchestration.engine.run.prompt_content import PromptContentPreparer
from chrys.orchestration.engine.run.runtime_skills import (
    CommittedSkillRefresh,
    RuntimeSkillRefresher,
    StagedRuntimeSkillRefresh,
)
from chrys.orchestration.engine.run.turn_hooks import PromptSubmitGate
from chrys.orchestration.engine.run.turn_state import ActiveInjectionTarget
from chrys.orchestration.engine.run.working_dir import publish_working_dir_missing, refuse_while_working_dir_missing
from chrys.orchestration.engine.state.machine import EngineState, Trigger
from chrys.service.hooks.schema import HookDecision
from chrys.service.trajectory.preparation import (
    PreparationOutcome,
    PreparationScope,
    PreparationTrace,
    input_admission_wait,
)

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import RuntimeSkillDetails
    from chrys.orchestration.engine.run.bindings import TurnBindings
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.trajectory import TrajectoryRecorder
    from chrys.orchestration.invoker.kernel import KernelConversation
    from chrys.service.agent_middleware.system_reminder import (
        CurrentRunReminderScope,
        CurrentRunReminderTarget,
        SystemReminderMiddleware,
    )
    from chrys.service.hooks.operation import HookOperation
    from chrys.service.session.history import SessionHistoryManager


_RETRY_MISSING_USER_ANCHOR = msg(
    "retry.missing_user_anchor",
    fallback="Cannot retry without a real user message in history.",
)
_RETRY_SUB_AGENT_PAUSED = msg(
    "retry.sub_agent_paused",
    fallback="Resolve paused sub-agent(s) first (Retry/Abort on the card) before retrying the main run.",
)


@dataclass(frozen=True)
class _CurrentRunSideEffectTarget:
    """Captured current-run owner for non-injection retry-note side effects."""

    session_id: str | None
    session_generation: int
    build_generation: int
    current_run_scope: CurrentRunScope
    bindings: TurnBindings
    conversation: KernelConversation
    reminder_middleware: SystemReminderMiddleware
    reminder_target: CurrentRunReminderTarget


@dataclass(frozen=True)
class _RetryNoteOwner:
    """Session/build/current-run owner captured before retry-note hook awaits."""

    session_id: str | None
    session_generation: int
    build_generation: int
    current_run_scope: CurrentRunScope | None
    bindings: TurnBindings
    conversation: KernelConversation
    reminder_middleware: SystemReminderMiddleware | None


@dataclass(frozen=True)
class _PreparedRetryNoteSideEffects:
    """Retry-note reminders and staged skill refresh ready for a concrete run scope."""

    reminders: list[str]
    staged: StagedRuntimeSkillRefresh | None
    operation: HookOperation | None = None
    """The note's input operation, committed where the note's side effects are."""


class RetryPassRunner(Protocol):
    """Execute an admitted retry and save its terminal state."""

    async def __call__(
        self,
        retry: RetryInput = BARE_RETRY,
        *,
        run_scope: CurrentRunScope | None = None,
        injection_window: CurrentRunInjectionWindow | None = None,
        admission_preparation: PreparationTrace | None = None,
    ) -> None: ...


class RetryCoordinator:
    """Own retry admission, pending retry state, and retry task creation."""

    def __init__(
        self,
        *,
        turn_state: TurnRuntimeState,
        current: CurrentAgent,
        permits: LifecyclePermits,
        session: ActiveSession,
        fsm: EngineStateMachine,
        history: SessionHistoryManager,
        bus: EventBus,
        trajectory_recorder: TrajectoryRecorder,
        gate: PromptSubmitGate,
        injector: ActiveTurnInjector,
        content: PromptContentPreparer,
        skills: RuntimeSkillRefresher,
        retry_and_save: RetryPassRunner,
    ) -> None:
        self._turn_state = turn_state
        self._current = current
        self._permits = permits
        self._session = session
        self._fsm = fsm
        self._history = history
        self._bus = bus
        self._trajectory_recorder = trajectory_recorder
        self._gate = gate
        self._injector = injector
        self._content = content
        self._skills = skills
        self._retry_and_save = retry_and_save
        self._preparation_handed_off = False

    def _open_pre_turn_preparation(self) -> PreparationTrace | None:
        """Open a session-root preparation scope before retry admission awaits."""
        context = self._trajectory_recorder.context()
        if context is not None:
            context = context.with_turn(None).with_run(None)
        return PreparationTrace.open(
            scope=PreparationScope.PRE_TURN,
            phase="input_admission",
            context=context,
        )

    async def _start_pre_admission_preparation(self, tracker: PreAdmissionPreparationTracker) -> None:
        """Start and register a retry preparation against the current session."""
        preparation = self._open_pre_turn_preparation()
        if preparation is None:
            tracker.current = None
            return
        entry = PreAdmissionPreparationEntry(preparation=preparation)
        tracker.current = entry
        await preparation.started()
        self._turn_state.lease.register_pre_admission_preparation(entry)

    async def _wait_for_pre_admission_gate(
        self,
        wait_for_gate: Callable[[], Awaitable[object]],
        tracker: PreAdmissionPreparationTracker,
    ) -> None:
        """Wait at one gate and rebind a preparation swept by a session boundary."""
        entry = tracker.current
        preparation = tracker.preparation
        await input_admission_wait(wait_for_gate, preparation, entry)
        if preparation is None or not preparation.finished_state:
            return
        self._turn_state.lease.deregister_pre_admission_preparation(entry)
        await self._start_pre_admission_preparation(tracker)

    @staticmethod
    async def _finish_preparation(preparation: PreparationTrace | None, outcome: str) -> None:
        if preparation is not None:
            await preparation.finished(outcome=outcome)

    async def handle_user_retry(self, event: UserRetry) -> None:
        """Handle retry requests and route them to immediate or pending execution."""
        if await self._turn_state.lease.refuse_while_workflow_active(self._bus, self._session.session_id):
            return
        preparation_tracker = PreAdmissionPreparationTracker()
        try:
            await self._start_pre_admission_preparation(preparation_tracker)
            await self._handle_user_retry(event, preparation_tracker=preparation_tracker)
        except asyncio.CancelledError:
            preparation = preparation_tracker.preparation
            if preparation is not None:
                preparation.finished_soon(outcome=PreparationOutcome.CANCELLED)
            raise
        except BaseException:
            preparation = preparation_tracker.preparation
            if preparation is not None:
                preparation.finished_soon(outcome=PreparationOutcome.PREPARATION_FAILED)
            raise
        finally:
            self._turn_state.lease.deregister_pre_admission_preparation(preparation_tracker.current)
            preparation = preparation_tracker.preparation
            pending_owns_preparation = self._turn_state.lease.pending_retry.preparation_trace is preparation
            if (
                preparation is not None
                and not self._preparation_handed_off
                and not pending_owns_preparation
                and not preparation.finished_state
            ):
                await preparation.finished(outcome=PreparationOutcome.PREPARATION_FAILED)

    async def _handle_user_retry(
        self,
        event: UserRetry,
        *,
        preparation_tracker: PreAdmissionPreparationTracker,
    ) -> None:
        """Execute one retry admission under its pre-turn preparation scope."""
        admission: PromptAdmissionScope | None = None
        admission_released = False
        # Read by the ``finally`` below on every exit, the earliest returns included.
        operation: HookOperation | None = None
        while True:
            await self._wait_for_pre_admission_gate(self._permits.wait_for_agent_load_idle, preparation_tracker)
            preparation = preparation_tracker.preparation
            if self._current.loaded is None:
                if self._turn_state.lease.prompt_admission_closed:
                    await self._wait_for_pre_admission_gate(
                        self._turn_state.lease.wait_for_prompt_admission_open,
                        preparation_tracker,
                    )
                    continue
                await self._finish_preparation(preparation, PreparationOutcome.NOT_READY)
                return
            if self._turn_state.lease.prompt_admission_closed:
                await self._wait_for_pre_admission_gate(
                    self._turn_state.lease.wait_for_prompt_admission_open,
                    preparation_tracker,
                )
                continue
            if self._turn_state.lease.has_active_admission_kind("fresh"):
                await self._wait_for_pre_admission_gate(
                    self._turn_state.lease.wait_for_active_admissions_idle,
                    preparation_tracker,
                )
                continue
            if await refuse_while_working_dir_missing(self._bus, self._session):
                await self._finish_preparation(preparation, PreparationOutcome.REJECTED)
                return
            # A workflow run may have taken the lease while this retry was preparing.
            if await self._turn_state.lease.refuse_while_workflow_active(self._bus, self._session.session_id):
                await self._finish_preparation(preparation, PreparationOutcome.REJECTED)
                return
            if not self._has_real_user_anchor():
                await self._bus.publish(
                    Error(
                        code="retry_missing_user_anchor",
                        message="Cannot retry without a real user message in history.",
                        display_message=_RETRY_MISSING_USER_ANCHOR.bind(),
                        recoverable=True,
                        session_id=self._session.session_id,
                    )
                )
                await self._finish_preparation(preparation, PreparationOutcome.REJECTED)
                return
            admission = self._turn_state.lease.reserve_prompt_admission(
                kind="retry",
                session_generation=self._permits.session_generation,
                build_generation=self._permits.build_generation,
                preparation_trace=preparation,
            )
            if admission is None:
                await self._wait_for_pre_admission_gate(
                    self._turn_state.lease.wait_for_prompt_admission_open,
                    preparation_tracker,
                )
                continue
            self._turn_state.lease.deregister_pre_admission_preparation(preparation_tracker.current)
            break

        try:
            if self._fsm.is_awaiting_sub_agents():
                await self._bus.publish(
                    Warning(
                        code="sub_agent_paused",
                        message=(
                            "Resolve paused sub-agent(s) first (Retry/Abort on the card) before retrying the main run."
                        ),
                        display_message=_RETRY_SUB_AGENT_PAUSED.bind(),
                        session_id=self._session.session_id,
                    )
                )
                await self._finish_preparation(preparation, PreparationOutcome.CONFLICT)
                return
            decision: HookDecision | None = None
            retry_note_owner = self._capture_retry_note_owner() if event.text else None
            active_retry_target = (
                self._capture_retry_note_active_target()
                if event.text and self._current.loaded is not None and self._current.loaded.bindings.state.running
                else None
            )
            pending_retry_installed = False

            def _install_pending_retry_before_commit() -> None:
                nonlocal admission_released, pending_retry_installed
                if active_retry_target is None:
                    return
                if (
                    self._turn_state.lease.run_task is not active_retry_target.run_task
                    or active_retry_target.run_task.done()
                ):
                    return
                if not self._pending_retry_request_still_accepted():
                    return
                if not self._admission_owner_is_current(admission):
                    return
                self._turn_state.lease.upsert_pending_retry_from_admission(admission, RetryInput.of(event))
                self._preparation_handed_off = True
                self._turn_state.lease.release_prompt_admission(admission)
                admission_released = True
                transition = self._fsm.try_transition(Trigger.RETRY_REQUESTED)
                pending_retry_installed = transition is not None or self._fsm.state == EngineState.PENDING_RETRY

            if event.text:
                operation = self._gate.new_operation(event.text)
                # Tracked until the note is submitted (or the retry ends): the same lifetimes end it.
                self._turn_state.input_operations.add(operation)
                decision = await self._gate.evaluate(
                    operation,
                    injected=True,
                    target_operation_id=preparation.committed_operation_id if preparation is not None else None,
                    trajectory_context=preparation.context if preparation is not None else None,
                )
                if (
                    decision.invalidated
                    or (not self._admission_owner_is_current(admission))
                    or self._retry_note_owner_stale_after_hook(retry_note_owner, active_retry_target)
                ):
                    await self._finish_preparation(preparation, PreparationOutcome.OWNER_CHANGED)
                    self._clear_pending_retry_for_admission_or_unowned(admission)
                    return
                retry_note_session_id = retry_note_owner.session_id if retry_note_owner is not None else None
                if await self._gate.handle_decision(
                    decision,
                    refused_input="retry_note",
                    session_id=retry_note_session_id,
                ):
                    await self._finish_preparation(preparation, PreparationOutcome.REJECTED)
                    self._clear_pending_retry_for_admission_or_unowned(admission)
                    return
                if await self._reject_retry_images(event.text, session_id=retry_note_session_id):
                    await self._finish_preparation(preparation, PreparationOutcome.IMAGE_REJECTED)
                    self._clear_pending_retry_for_admission_or_unowned(admission)
                    return
            if not self._admission_owner_is_current(admission):
                await self._finish_preparation(preparation, PreparationOutcome.OWNER_CHANGED)
                return
            if await self._reject_text_only_active_history():
                await self._finish_preparation(preparation, PreparationOutcome.IMAGE_REJECTED)
                self._clear_pending_retry_for_admission_or_unowned(admission)
                return
            deferred_retry_note: _PreparedRetryNoteSideEffects | None = None
            if event.text:
                accepted, deferred_retry_note = await self._commit_retry_note_side_effects(
                    event.text,
                    decision,
                    retry_note_owner,
                    active_retry_target,
                    before_commit=_install_pending_retry_before_commit,
                    operation=operation,
                )
                if not accepted:
                    await self._finish_preparation(preparation, PreparationOutcome.PREPARATION_FAILED)
                    self._clear_pending_retry_for_admission_or_unowned(admission)
                    return
            if pending_retry_installed:
                return
            if (
                self._current.loaded is not None
                and not self._current.loaded.bindings.state.running
                and self._turn_state.lease.run_task is None
                and not self.can_retry()
            ):
                await self._finish_preparation(preparation, PreparationOutcome.CONFLICT)
                return
            if self._current.loaded is not None and self._current.loaded.bindings.state.running:
                if self._admission_owner_is_current(admission) and self._pending_retry_request_still_accepted():
                    self._turn_state.lease.upsert_pending_retry_from_admission(admission, RetryInput.of(event))
                    self._preparation_handed_off = True
                    self._turn_state.lease.release_prompt_admission(admission)
                    admission_released = True
                    self._fsm.try_transition(Trigger.RETRY_REQUESTED)
                else:
                    outcome = (
                        PreparationOutcome.CONFLICT
                        if self._admission_owner_is_current(admission)
                        else PreparationOutcome.OWNER_CHANGED
                    )
                    await self._finish_preparation(preparation, outcome)
                    self._clear_pending_retry_for_admission_or_unowned(admission)
                return
            wait_entry = PreAdmissionPreparationEntry(preparation=preparation) if preparation is not None else None
            if wait_entry is not None:
                self._turn_state.lease.register_pre_admission_preparation(wait_entry)
            try:
                await input_admission_wait(self._wait_for_existing_run_task, preparation, wait_entry)
            finally:
                self._turn_state.lease.deregister_pre_admission_preparation(wait_entry)
            if not self._admission_owner_is_current(admission):
                await self._finish_preparation(preparation, PreparationOutcome.OWNER_CHANGED)
                return
            if self._turn_state.lease.run_task is not None and not self._turn_state.lease.run_task.done():
                await self._finish_preparation(preparation, PreparationOutcome.CONFLICT)
                return
            # The run this retry waited for can end because its directory was deleted.
            if await refuse_while_working_dir_missing(self._bus, self._session):
                await self._finish_preparation(preparation, PreparationOutcome.REJECTED)
                return
            if not self._claim_immediate_retry_start():
                await self._finish_preparation(preparation, PreparationOutcome.CONFLICT)
                return
            if self._current.loaded is None:
                # No run scope takes the note's side effects here, so this start is its submit point.
                if deferred_retry_note is not None and not _commit_note(deferred_retry_note):
                    await self._finish_preparation(preparation, PreparationOutcome.CANCELLED)
                    return
                self._turn_state.lease.release_prompt_admission(admission)
                admission_released = True
                self._history.remove_trailing_markers()
                self._turn_state.lease.run_task = asyncio.create_task(
                    self._retry_and_save(RetryInput.of(event), admission_preparation=preparation)
                )
                self._preparation_handed_off = True
                return

            self._history.remove_trailing_markers()
            promotion = self._turn_state.lease.promote_retry_admission(
                admission,
                reminder_scope=self._retry_reminder_scope_for_admission(admission),
                make_task=lambda scope, window: asyncio.create_task(
                    self._retry_and_save(
                        RetryInput.of(event),
                        run_scope=scope,
                        injection_window=window,
                        admission_preparation=preparation,
                    )
                ),
            )
            if promotion.outcome == "task":
                admission_released = True
                self._preparation_handed_off = True
                if deferred_retry_note is not None:
                    scope = self._turn_state.lease.current_run_scope
                    if scope is None or scope.owner_admission_id != admission.admission_id:
                        self._discard_promoted_retry_task(promotion.task, scope)
                        await self._finish_preparation(preparation, PreparationOutcome.OWNER_CHANGED)
                        self._clear_pending_retry_for_admission_or_unowned(admission)
                        return
                    if not await self._commit_deferred_retry_note_side_effects(deferred_retry_note, scope):
                        self._discard_promoted_retry_task(promotion.task, scope)
                        await self._finish_preparation(preparation, PreparationOutcome.PREPARATION_FAILED)
                        self._clear_pending_retry_for_admission_or_unowned(admission)
            else:
                await self._finish_preparation(
                    preparation,
                    PreparationOutcome.OWNER_CHANGED if promotion.outcome == "stale" else PreparationOutcome.CONFLICT,
                )
        finally:
            self._turn_state.input_operations.discard(operation)
            if admission is not None and not admission_released:
                self._turn_state.lease.release_prompt_admission(admission)

    def start_pending_retry_if_due(self) -> str | None:
        """Start a queued retry when post-run state allows retry dispatch.

        Return the working directory only when a due retry was dropped because
        that directory is gone, else None. The finished pass left the FSM
        RUNNING for that retry, so the caller settles it to the pass's own
        terminal state and then reports the drop
        (:meth:`report_retry_dropped_for_missing_cwd`).
        """
        if self._session.shutting_down:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return None
        if self._fsm.state != EngineState.RUNNING:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return None
        if self._turn_state.lease.pending_retry.dispatch_disabled:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return None
        pending = self._turn_state.lease.pending_retry
        scope = self._turn_state.lease.current_run_scope
        if pending.owner_admission_id is None:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return None
        if pending.session_generation != self._permits.session_generation:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return None
        if pending.run_generation == 0:
            if scope is not None:
                self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
                return None
        elif scope is None or pending.run_generation != scope.run_generation:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return None
        if self._turn_state.lease.pending_retry_dispatch_disabled_for_session_generation == pending.session_generation:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return None
        workspace = self._session.workspace
        if workspace is not None and (missing := workspace.missing_primary()) is not None:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
            return missing
        self._history.remove_trailing_markers()
        dispatched = self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.RETRY_TURN)
        self._turn_state.lease.run_task = self._create_retry_run_task(
            dispatched.retry, admission_preparation=dispatched.preparation_trace
        )
        return None

    async def report_retry_dropped_for_missing_cwd(self, path: str) -> None:
        """Tell frontends a queued retry will not run because *path* is gone.

        A frontend shows an accepted retry as running until a run ends it; no
        run will, so this error does.
        """
        await publish_working_dir_missing(self._bus, self._session.session_id, path)

    async def _wait_for_existing_run_task(self) -> None:
        """Wait for current run-task cleanup, if any."""
        task = self._turn_state.lease.run_task
        if task is not None and not task.done():
            await task

    def _reuse_or_create_retry_scope(self) -> CurrentRunScope | None:
        """Return a retry scope, preserving the current logical scope when valid."""
        scope = self._turn_state.lease.current_run_scope
        if (
            scope is not None
            and scope.session_generation == self._permits.session_generation
            and scope.build_generation == self._permits.build_generation
        ):
            return scope
        return None

    def _reuse_or_create_retry_scope_and_window(self) -> tuple[CurrentRunScope, CurrentRunInjectionWindow] | None:
        """Open a retry injection window, preserving the current logical scope when valid."""
        scope = self._reuse_or_create_retry_scope()
        if scope is not None:
            return scope, self._turn_state.lease.open_injection_admission(scope)
        return None

    def _create_retry_run_task(
        self, retry: RetryInput, *, admission_preparation: PreparationTrace | None = None
    ) -> asyncio.Task[None]:
        """Create a retry task, preserving the validated current retry scope."""
        scope_window = self._reuse_or_create_retry_scope_and_window()
        if scope_window is None:
            return asyncio.create_task(self._retry_and_save(retry, admission_preparation=admission_preparation))
        scope, window = scope_window
        return asyncio.create_task(
            self._retry_and_save(
                retry, run_scope=scope, injection_window=window, admission_preparation=admission_preparation
            )
        )

    def _discard_promoted_retry_task(
        self,
        task: asyncio.Task[None] | None,
        scope: CurrentRunScope | None,
    ) -> None:
        """Cancel a just-promoted retry task when its deferred admission side effects fail."""
        if task is not None and not task.done():
            task.cancel()
        if self._turn_state.lease.run_task is task:
            self._turn_state.lease.run_task = None
        if scope is not None:
            self._turn_state.lease.clear_current_run_scope(scope)
            if self._current.loaded is not None:
                self._current.loaded.reminder_middleware.expire_current_run_scope(scope.reminder_scope)
        self._fsm.try_transition(Trigger.RUN_FAILED)

    def _admission_owner_is_current(self, admission: PromptAdmissionScope) -> bool:
        """Return whether *admission* still belongs to the live session/build owner."""
        return (
            self._permits.session_generation == admission.session_generation
            and self._permits.build_generation == admission.build_generation
        )

    def _has_real_user_anchor(self) -> bool:
        """Return whether the marker-ignoring current logical turn has user input."""
        current_input = self._turn_state.current_input
        if current_input.kind != "continuation" and (current_input.text or current_input.contents):
            # Fresh input may not have landed in provider history yet, but it
            # is the same real anchor crash recovery would synthesize.
            return True
        messages = self._history.messages
        logical_end = len(messages)
        while logical_end:
            kind = messages[logical_end - 1].additional_properties.get(HistoryMarkerKind.KEY)
            if kind not in HistoryMarkerKind.SESSION_COUNT_EXCLUDED:
                break
            logical_end -= 1
        logical_messages = messages[:logical_end]
        start = current_turn_start(logical_messages)
        return any(
            message.role == "user" and not is_continuation_message(message) for message in logical_messages[start:]
        )

    def can_retry(self) -> bool:
        """Return whether the current state may start an immediate retry.

        Read-only: a querying frontend must not move the FSM. Admission at
        :meth:`_claim_immediate_retry_start` re-reads it, and the state a
        reader saw can have moved on by then — a retry still ends up refused
        when it is no longer valid.
        """
        if self._session.shutting_down:
            return False
        if self._fsm.state in (EngineState.INTERRUPTED, EngineState.FAILED):
            return True
        return (
            self._fsm.state is EngineState.RUNNING
            and self._turn_state.lease.run_task is not None
            and self._turn_state.lease.run_task.done()
            and self._current.loaded is not None
            and not self._current.loaded.bindings.state.running
        )

    def _claim_immediate_retry_start(self) -> bool:
        """Validate and transition the FSM for an immediate retry before task install."""
        if not self.can_retry():
            return False
        if self._fsm.state in (EngineState.INTERRUPTED, EngineState.FAILED):
            self._fsm.try_transition(Trigger.RETRY_STARTED)
            return self._fsm.state is EngineState.RUNNING
        return True

    def _retry_request_still_accepted(self) -> bool:
        """Return whether retry-note side effects may still be committed."""
        return not self._session.shutting_down and self._fsm.state in (
            EngineState.RUNNING,
            EngineState.PENDING_RETRY,
            EngineState.INTERRUPTED,
            EngineState.FAILED,
        )

    def _pending_retry_request_still_accepted(self) -> bool:
        """Return whether a retry request may be queued for the active executor."""
        return not self._session.shutting_down and self._fsm.state is EngineState.RUNNING

    def _clear_pending_retry_for_admission_or_unowned(self, admission: PromptAdmissionScope) -> None:
        """Clear retry state only when it is unowned or belongs to *admission*."""
        pending = self._turn_state.lease.pending_retry
        if pending.owner_admission_id is None or pending.updated_by_admission_id == admission.admission_id:
            self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)

    def _retry_reminder_scope_for_admission(self, admission: PromptAdmissionScope) -> CurrentRunReminderScope:
        """Return the existing logical retry reminder scope, or allocate a new one."""
        current_scope = self._turn_state.lease.current_run_scope
        if (
            current_scope is not None
            and current_scope.session_generation == admission.session_generation
            and current_scope.build_generation == admission.build_generation
        ):
            return current_scope.reminder_scope
        reminder_middleware = self._current.loaded.reminder_middleware if self._current.loaded is not None else None
        if reminder_middleware is None:
            raise RuntimeError("Cannot promote retry without reminder middleware")
        return reminder_middleware.create_current_run_scope()

    def _capture_retry_note_active_target(self) -> ActiveInjectionTarget | None:
        """Capture the active current-run owner used by retry-note side effects."""
        return self._injector.capture_target(route="fsm_active")

    def _retry_note_active_target_owner_invalid(self, target: ActiveInjectionTarget) -> bool:
        """Return whether a captured retry-note owner changed beyond executor completion."""
        return self._injector.target_owner_invalid(target)

    def _capture_retry_note_owner(self) -> _RetryNoteOwner | None:
        """Capture the retry-note owner before awaited prompt-submit hooks run."""
        executor = self._current.loaded.bindings if self._current.loaded is not None else None
        if executor is None:
            return None
        return _RetryNoteOwner(
            session_id=self._session.session_id,
            session_generation=self._permits.session_generation,
            build_generation=self._permits.build_generation,
            current_run_scope=self._turn_state.lease.current_run_scope,
            bindings=executor,
            conversation=executor.backend,
            reminder_middleware=self._current.loaded.reminder_middleware if self._current.loaded is not None else None,
        )

    def _retry_note_owner_invalid(self, owner: _RetryNoteOwner) -> bool:
        """Return whether the pre-hook retry-note owner changed."""
        return (
            self._session.session_id != owner.session_id
            or self._permits.session_generation != owner.session_generation
            or self._permits.build_generation != owner.build_generation
            or (self._current.loaded is None or self._current.loaded.bindings.backend is not owner.conversation)
            or self._current.loaded.reminder_middleware is not owner.reminder_middleware
        )

    def _retry_note_owner_scope_conflicted(self, owner: _RetryNoteOwner) -> bool:
        """Return whether a different live current-run scope replaced the pre-hook owner."""
        current_scope = self._turn_state.lease.current_run_scope
        return current_scope is not None and current_scope != owner.current_run_scope

    def _capture_current_run_side_effect_target(self, scope: CurrentRunScope) -> _CurrentRunSideEffectTarget | None:
        """Capture a current-run reminder target for scoped retry-note side effects."""
        executor = self._current.loaded.bindings if self._current.loaded is not None else None
        reminder_middleware = self._current.loaded.reminder_middleware if self._current.loaded is not None else None
        if executor is None or reminder_middleware is None:
            return None
        if (
            scope.session_generation != self._permits.session_generation
            or scope.build_generation != self._permits.build_generation
        ):
            return None
        if self._turn_state.lease.current_run_scope != scope:
            return None
        reminder_target = reminder_middleware.capture_current_run_target(scope.reminder_scope)
        if reminder_target is None:
            return None
        return _CurrentRunSideEffectTarget(
            session_id=self._session.session_id,
            session_generation=self._permits.session_generation,
            build_generation=self._permits.build_generation,
            current_run_scope=scope,
            bindings=executor,
            conversation=executor.backend,
            reminder_middleware=reminder_middleware,
            reminder_target=reminder_target,
        )

    def _current_run_side_effect_target_is_current(self, target: _CurrentRunSideEffectTarget) -> bool:
        """Return whether a scoped retry-note side-effect owner remains current."""
        return (
            self._session.session_id == target.session_id
            and self._permits.session_generation == target.session_generation
            and self._permits.build_generation == target.build_generation
            and self._turn_state.lease.current_run_scope == target.current_run_scope
            and self._current.loaded is not None
            and self._current.loaded.bindings.backend is target.conversation
            and self._current.loaded.reminder_middleware is target.reminder_middleware
        )

    def _retry_note_owner_stale_after_hook(
        self,
        owner: _RetryNoteOwner | None,
        active_retry_target: ActiveInjectionTarget | None,
    ) -> bool:
        """Return whether a retry note owner became stale while hooks awaited."""
        if owner is None or self._retry_note_owner_invalid(owner) or self._retry_note_owner_scope_conflicted(owner):
            return True
        return active_retry_target is not None and self._retry_note_active_target_owner_invalid(active_retry_target)

    async def _commit_retry_note_side_effects(
        self,
        text: str,
        decision: HookDecision | None,
        owner: _RetryNoteOwner | None,
        active_retry_target: ActiveInjectionTarget | None,
        *,
        before_commit: Callable[[], None] | None = None,
        operation: HookOperation | None = None,
    ) -> tuple[bool, _PreparedRetryNoteSideEffects | None]:
        """Commit retry-note side effects, or defer them until retry promotion creates a scope."""
        if not text:
            return True, None
        if not self._retry_request_still_accepted():
            return False, None
        if owner is None or self._retry_note_owner_invalid(owner) or self._retry_note_owner_scope_conflicted(owner):
            return False, None

        staged = await self._stage_runtime_skills()
        if (
            not self._retry_request_still_accepted()
            or self._retry_note_owner_invalid(owner)
            or self._retry_note_owner_scope_conflicted(owner)
        ):
            return False, None

        side_effects = self._prepare_retry_note_side_effects(text, decision, staged, operation)

        target: _CurrentRunSideEffectTarget | None = None
        if active_retry_target is not None:
            if self._retry_note_active_target_owner_invalid(active_retry_target):
                return False, None
            current_scope = self._turn_state.lease.current_run_scope
            if current_scope == active_retry_target.current_run_scope:
                target = _CurrentRunSideEffectTarget(
                    session_id=active_retry_target.session_id,
                    session_generation=active_retry_target.session_generation,
                    build_generation=active_retry_target.build_generation,
                    current_run_scope=active_retry_target.current_run_scope,
                    bindings=active_retry_target.bindings,
                    conversation=active_retry_target.conversation,
                    reminder_middleware=active_retry_target.reminder_middleware,
                    reminder_target=active_retry_target.reminder_target,
                )
            elif current_scope is not None:
                return False, None

        if target is None:
            scope = self._reuse_or_create_retry_scope()
            if scope is None:
                return True, side_effects
            target = self._capture_current_run_side_effect_target(scope)
            if target is None:
                return False, None

        if not await self._commit_retry_note_side_effects_to_target(
            side_effects,
            target,
            before_commit=before_commit,
        ):
            return False, None
        return True, None

    def _prepare_retry_note_side_effects(
        self,
        text: str,
        decision: HookDecision | None,
        staged: StagedRuntimeSkillRefresh | None,
        operation: HookOperation | None,
    ) -> _PreparedRetryNoteSideEffects:
        """Build retry-note side effects after hooks and skill refresh have settled."""
        reminders = PromptSubmitGate.reminder_texts(decision)
        skill_reference = self._skill_reference_reminder(
            text,
            skill_details=staged.skill_details if staged is not None else None,
        )
        if skill_reference is not None:
            reminders.append(skill_reference)
        return _PreparedRetryNoteSideEffects(reminders=reminders, staged=staged, operation=operation)

    async def _commit_retry_note_side_effects_to_target(
        self,
        side_effects: _PreparedRetryNoteSideEffects,
        target: _CurrentRunSideEffectTarget,
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> bool:
        """Commit prepared retry-note side effects to a captured current-run target."""
        if not self._retry_request_still_accepted() or not self._current_run_side_effect_target_is_current(target):
            return False
        if not target.reminder_middleware.is_current_run_target_valid(target.reminder_target):
            return False
        # The submit point, synchronous with what follows: the note its hooks checked, still wanted.
        if not _commit_note(side_effects):
            return False

        staged = side_effects.staged
        if staged is not None and not target.reminder_middleware.set_skill_catalog_for_current_run(
            target.reminder_target,
            staged.skill_catalog,
        ):
            return False
        if not target.reminder_middleware.queue_hook_reminders_for_current_run(
            target.reminder_target,
            side_effects.reminders,
        ):
            return False
        if before_commit is not None:
            before_commit()
            if not self._retry_request_still_accepted() or not self._current_run_side_effect_target_is_current(target):
                return False
            if not target.reminder_middleware.is_current_run_target_valid(target.reminder_target):
                return False

        committed = self._commit_staged_runtime_skills(staged)
        await self._publish_staged_runtime_skill_refresh(
            committed,
            session_id=target.session_id,
        )
        return True

    async def _commit_deferred_retry_note_side_effects(
        self,
        side_effects: _PreparedRetryNoteSideEffects,
        scope: CurrentRunScope,
    ) -> bool:
        """Commit deferred retry-note side effects after retry admission owns a real scope."""
        target = self._capture_current_run_side_effect_target(scope)
        if target is None:
            return False
        return await self._commit_retry_note_side_effects_to_target(side_effects, target)

    async def _reject_retry_images(self, text: str, *, session_id: str | None = None) -> bool:
        """Publish a non-fatal rejection for retry notes containing image mentions."""
        return await self._content.reject_retry_images(text, session_id=session_id)

    async def _reject_text_only_active_history(self) -> bool:
        """Publish a text-only model rejection when active history contains image content."""
        return await self._content.reject_text_only_active_history()

    async def _stage_runtime_skills(self) -> StagedRuntimeSkillRefresh | None:
        """Discover runtime skills without mutating live provider or host runtime state."""
        return await self._skills.stage_refresh()

    def _commit_staged_runtime_skills(self, staged: StagedRuntimeSkillRefresh | None) -> CommittedSkillRefresh | None:
        """Commit a staged runtime skill refresh synchronously to the captured owner."""
        return self._skills.commit_staged_refresh(staged)

    async def _publish_staged_runtime_skill_refresh(
        self,
        committed: CommittedSkillRefresh | None,
        *,
        session_id: str | None,
    ) -> None:
        """Publish warning/runtime update events for a committed staged refresh."""
        await self._skills.publish_committed_refresh(committed, session_id=session_id)

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
        return format_skill_reference_reminder(reference)


def _commit_note(side_effects: _PreparedRetryNoteSideEffects) -> bool:
    """Take the right to submit the retry note; False when its input ended or changed since its hooks passed it."""
    operation = side_effects.operation
    return operation is None or operation.commit_input(operation.input_text) is None
