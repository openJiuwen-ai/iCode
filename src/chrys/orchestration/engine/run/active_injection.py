# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Active-turn user injection admission for main-agent turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from chrys.foundation.events.types import UserInjectResult
from chrys.orchestration.engine.run.input_refs import format_skill_reference_reminder, parse_skill_reference
from chrys.orchestration.engine.run.prompt_content import PromptContentPreparer
from chrys.orchestration.engine.run.runtime_skills import (
    CommittedSkillRefresh,
    RuntimeSkillRefresher,
    StagedRuntimeSkillRefresh,
)
from chrys.orchestration.engine.run.turn_hooks import PromptSubmitGate
from chrys.orchestration.engine.run.turn_state import ActiveInjectionTarget
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationTrace

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import RuntimeSkillDetails
    from chrys.orchestration.engine.execution import CurrentRunScope, PreAdmissionPreparationTracker
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.service.agent_middleware.injection import QueuedInjection
    from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
    from chrys.service.hooks.schema import HookDecision


@dataclass(slots=True)
class _PreparationOwnership:
    """Track the synchronous handoff from admission to the middleware queue."""

    trace: PreparationTrace | None
    tracker: PreAdmissionPreparationTracker | None = None
    handed_off: bool = False

    def hand_off(self, turn_state: TurnRuntimeState) -> None:
        """Transfer an open trace and remove its obsolete admission registration."""
        self.handed_off = self.trace is not None
        if self.tracker is not None:
            self.tracker.preparation_handed_off = self.handed_off
            if self.handed_off:
                turn_state.lease.deregister_pre_admission_preparation(self.tracker.current)


class ActiveTurnInjector:
    """Admit active-turn user text and commit it to the captured executor."""

    def __init__(
        self,
        *,
        turn_state: TurnRuntimeState,
        current: CurrentAgent,
        permits: LifecyclePermits,
        session: ActiveSession,
        fsm: EngineStateMachine,
        bus: EventBus,
        gate: PromptSubmitGate,
        content: PromptContentPreparer,
        skills: RuntimeSkillRefresher,
    ) -> None:
        self._turn_state = turn_state
        self._current = current
        self._permits = permits
        self._session = session
        self._fsm = fsm
        self._bus = bus
        self._gate = gate
        self._content = content
        self._skills = skills

    async def inject(
        self,
        text: str,
        *,
        created_at: datetime | str | None,
        route: Literal["fsm_active", "executor_fallback"],
        reject_images_without_target: bool,
        injection_id: str | None = None,
        preparation: PreparationTrace | None = None,
        preparation_tracker: PreAdmissionPreparationTracker | None = None,
    ) -> bool:
        """Inject *text* into the current run when the captured target remains valid.

        Return whether the preparation trace moved to the middleware queue.
        Cancel marks observed (or missed) before that handoff are dropped by
        the coordinator's in-flight watch when this handler exits.
        """
        ownership = _PreparationOwnership(preparation, tracker=preparation_tracker)
        try:
            return await self._inject(
                text,
                created_at=created_at,
                route=route,
                reject_images_without_target=reject_images_without_target,
                injection_id=injection_id,
                ownership=ownership,
            )
        except asyncio.CancelledError:
            if preparation is not None and not ownership.handed_off:
                preparation.finished_soon(outcome=PreparationOutcome.CANCELLED)
            raise
        except BaseException:
            if preparation is not None and not ownership.handed_off:
                preparation.finished_soon(outcome=PreparationOutcome.PREPARATION_FAILED)
            raise

    async def _inject(
        self,
        text: str,
        *,
        created_at: datetime | str | None,
        route: Literal["fsm_active", "executor_fallback"],
        reject_images_without_target: bool,
        injection_id: str | None,
        ownership: _PreparationOwnership,
    ) -> bool:
        preparation = ownership.trace
        target = self.capture_target(route=route)
        if target is None:
            if reject_images_without_target and await self._reject_injected_images(text):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.IMAGE_REJECTED)
                return False
            await self.publish_abandoned(target, text, created_at, injection_id=injection_id)
            if preparation is not None:
                await preparation.finished(outcome=PreparationOutcome.ABANDONED_NO_TARGET)
            return False

        decision = await self._gate.evaluate(
            text,
            injected=True,
            target_operation_id=preparation.committed_operation_id if preparation is not None else None,
            trajectory_context=preparation.context if preparation is not None else None,
        )
        if self._is_cancelled(injection_id):
            await self.publish_abandoned(target, text, created_at, injection_id=injection_id)
            if preparation is not None:
                await preparation.finished(outcome=PreparationOutcome.CANCELLED)
            return False
        if await self.abandon_if_target_stale(target, text, created_at, injection_id=injection_id):
            if preparation is not None:
                await preparation.finished(outcome=PreparationOutcome.TARGET_STALE)
            return False
        if await self._gate.handle_decision(
            decision,
            injected=True,
            session_id=target.session_id,
        ):
            if preparation is not None:
                await preparation.finished(outcome=PreparationOutcome.REJECTED)
            return False
        if await self._reject_injected_images(text, session_id=target.session_id):
            if preparation is not None:
                await preparation.finished(outcome=PreparationOutcome.IMAGE_REJECTED)
            return False
        committed = await self._commit_side_effects(
            target,
            text,
            decision,
            created_at=created_at,
            publish_abandoned=True,
            injection_id=injection_id,
            ownership=ownership,
        )
        if preparation is not None:
            if committed:
                return ownership.handed_off
            if self._is_cancelled(injection_id):
                await preparation.finished(outcome=PreparationOutcome.CANCELLED)
            else:
                await preparation.finished(outcome=PreparationOutcome.TARGET_STALE)
        return False

    def _is_cancelled(self, injection_id: str | None) -> bool:
        """Return whether the user cancelled this injection during admission."""
        return self._turn_state.is_injection_cancelled(injection_id)

    def capture_target(
        self,
        *,
        route: Literal["fsm_active", "executor_fallback"],
    ) -> ActiveInjectionTarget | None:
        """Capture the current active-injection owner before awaited admission work."""
        scope = self._turn_state.lease.current_run_scope
        task = self._turn_state.lease.run_task
        executor = self._current.loaded.bindings if self._current.loaded is not None else None
        reminder_middleware = self._current.loaded.reminder_middleware if self._current.loaded is not None else None
        if scope is None or task is None or executor is None or reminder_middleware is None:
            return None
        if (
            scope.session_generation != self._permits.session_generation
            or scope.build_generation != self._permits.build_generation
        ):
            return None
        if task.done():
            return None
        window = self._turn_state.lease.capture_current_injection_window(scope)
        if window is None:
            return None
        reminder_target = reminder_middleware.capture_current_run_target(scope.reminder_scope)
        if reminder_target is None:
            return None
        if route == "fsm_active":
            if not self._fsm.is_running():
                return None
        elif route == "executor_fallback":
            if not executor.state.running:
                return None
        else:
            return None
        return ActiveInjectionTarget(
            route=route,
            session_id=self._session.session_id,
            session_generation=self._permits.session_generation,
            build_generation=self._permits.build_generation,
            current_run_scope=scope,
            run_task=task,
            bindings=executor,
            conversation=executor.backend,
            reminder_middleware=reminder_middleware,
            reminder_target=reminder_target,
            injection_window=window,
            trajectory_turn_id=(
                executor.trajectory_context.turn_id if executor.trajectory_context is not None else None
            ),
        )

    def target_is_current(self, target: ActiveInjectionTarget) -> bool:
        """Return whether the captured active-injection owner is still current."""
        if self._session.session_id != target.session_id:
            return False
        if (
            self._permits.session_generation != target.session_generation
            or self._permits.build_generation != target.build_generation
        ):
            return False
        if self._turn_state.lease.current_run_scope != target.current_run_scope:
            return False
        if self._turn_state.lease.run_task is not target.run_task or target.run_task.done():
            return False
        if (
            self._current.loaded is None or self._current.loaded.bindings.backend is not target.conversation
        ) or self._current.loaded.reminder_middleware is not target.reminder_middleware:
            return False
        if not self._turn_state.lease.is_injection_admission_current(target.injection_window):
            return False
        if target.route == "fsm_active":
            return self._fsm.is_running()
        return target.bindings.state.running

    def target_owner_invalid(self, target: ActiveInjectionTarget) -> bool:
        """Return whether a captured current-run owner changed beyond executor completion."""
        return (
            self._session.session_id != target.session_id
            or self._permits.session_generation != target.session_generation
            or self._permits.build_generation != target.build_generation
            or (self._current.loaded is None or self._current.loaded.bindings.backend is not target.conversation)
            or self._current.loaded.reminder_middleware is not target.reminder_middleware
        )

    async def abandon_if_target_stale(
        self,
        target: ActiveInjectionTarget | None,
        text: str,
        created_at: datetime | str | None,
        *,
        injection_id: str | None = None,
    ) -> bool:
        """Publish abandonment and return True when a captured injection owner is stale."""
        if target is not None and self.target_is_current(target):
            return False
        await self.publish_abandoned(target, text, created_at, injection_id=injection_id)
        return True

    async def publish_abandoned(
        self,
        target: ActiveInjectionTarget | None,
        text: str,
        created_at: datetime | str | None,
        *,
        injection_id: str | None = None,
    ) -> None:
        """Publish a same-session abandoned injection result, suppressing stale owners."""
        if self._session.shutting_down or self._is_stale_session(target):
            return
        await self._bus.publish(
            UserInjectResult(
                text=text,
                consumed=False,
                created_at=created_at,
                injection_id=injection_id,
                session_id=target.session_id if target is not None else self._session.session_id,
            )
        )

    async def _commit_side_effects(
        self,
        target: ActiveInjectionTarget,
        text: str,
        decision: HookDecision | None,
        *,
        created_at: datetime | str | None,
        publish_abandoned: bool,
        injection_id: str | None = None,
        ownership: _PreparationOwnership,
    ) -> bool:
        """Commit scoped active-turn side effects and inject only for the captured owner."""

        async def _abandon() -> bool:
            if publish_abandoned:
                await self.publish_abandoned(target, text, created_at, injection_id=injection_id)
            return False

        if not self.target_is_current(target) or self._is_cancelled(injection_id):
            return await _abandon()

        staged = await self._stage_runtime_skills()
        skill_reference = self._skill_reference_reminder(
            text,
            skill_details=staged.skill_details if staged is not None else None,
        )
        reminders = PromptSubmitGate.reminder_texts(decision)
        if skill_reference is not None:
            reminders.append(skill_reference)

        # Re-check after the staging await; from here to ``executor.inject``
        # everything is synchronous, so a cancel cannot interleave past this
        # point before the text is queued.
        if not self.target_is_current(target) or self._is_cancelled(injection_id):
            return await _abandon()
        if not self._turn_state.lease.begin_active_injection_commit(target):
            return await _abandon()
        try:
            if not target.reminder_middleware.is_current_run_target_valid(target.reminder_target):
                return await _abandon()
            if staged is not None and not target.reminder_middleware.set_skill_catalog_for_current_run(
                target.reminder_target,
                staged.skill_catalog,
            ):
                return await _abandon()
            if not target.reminder_middleware.queue_hook_reminders_for_current_run(target.reminder_target, reminders):
                return await _abandon()
            committed = self._commit_staged_runtime_skills(staged)
            # Carry the queued reminders on the injection so a later cancel
            # can withdraw them together with the text.
            target.bindings.inject(
                text,
                created_at=created_at,
                injection_id=injection_id,
                reminders=tuple(reminders),
                preparation=ownership.trace,
                target_turn_id=target.trajectory_turn_id,
            )
            ownership.hand_off(self._turn_state)
            target.bindings.approval.append_user_message(text)
            await self._publish_staged_runtime_skill_refresh(
                committed,
                session_id=target.session_id,
            )
        finally:
            self._turn_state.lease.finish_active_injection_commit()
        return True

    def _is_stale_session(self, target: ActiveInjectionTarget | None) -> bool:
        """Return True when a stale target belongs to an abandoned session owner."""
        if target is None:
            return False
        return (
            self._session.session_id != target.session_id
            or self._permits.session_generation != target.session_generation
        )

    async def _reject_injected_images(
        self,
        text: str,
        *,
        session_id: str | None = None,
    ) -> bool:
        """Publish a non-fatal rejection for image mentions while a run is active."""
        return await self._content.reject_injected_images(text, session_id=session_id)

    async def _stage_runtime_skills(self) -> StagedRuntimeSkillRefresh | None:
        """Discover runtime skills without mutating live provider or host runtime state."""
        return await self._skills.stage_refresh()

    def _commit_staged_runtime_skills(
        self,
        staged: StagedRuntimeSkillRefresh | None,
    ) -> CommittedSkillRefresh | None:
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
        # AIxCoding telemetry: input-trigger report for a resolved slash skill reference.
        from chrys.aixcoding.telemetry.subscriber import record_skill_invocation

        record_skill_invocation(reference.skill.name, self._session.session_id)
        return format_skill_reference_reminder(reference)


def withdraw_committed_injection_reminders(
    reminder_middleware: SystemReminderMiddleware | None,
    current_run_scope: CurrentRunScope | None,
    injection: QueuedInjection,
) -> None:
    """Withdraw the commit-time hook/skill reminders of an undelivered injection.

    Valid whenever the injection's queue entry was still present (an explicit
    cancel or the finalizer's abandoned drain): no model call has consumed the
    entry since its commit, so its reminders are provably undelivered. When
    the scope is already gone or expired, the reminders die with it and there
    is nothing to withdraw.
    """
    if not injection.reminders:
        return
    if reminder_middleware is None or current_run_scope is None:
        return
    target = reminder_middleware.capture_current_run_target(current_run_scope.reminder_scope)
    if target is None:
        return
    reminder_middleware.remove_hook_reminders_for_current_run(target, list(injection.reminders))
