# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Event-facing turn coordination for main-agent turns."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.settings_store import SettingsHandle
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    InvocationAborted,
    InvocationAbortRequested,
    InvocationCascadeAborted,
    InvocationPaused,
    InvocationResumed,
    InvocationRetryRequested,
    UserInject,
    UserInjectCancel,
    UserInjectResult,
    UserInterrupt,
    UserMessage,
    UserRetry,
    Warning,
)
from chrys.foundation.i18n import msg
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.orchestration.engine.execution import (
    CurrentRunInjectionWindow,
    CurrentRunScope,
    PreAdmissionPreparationEntry,
    PreAdmissionPreparationTracker,
    PromptAdmissionScope,
)
from chrys.orchestration.engine.loader import AgentLoader
from chrys.orchestration.engine.run import sub_agent_coordination
from chrys.orchestration.engine.run.active_injection import ActiveTurnInjector
from chrys.orchestration.engine.run.attachments import (
    AttachmentDiscoveryResult,
    discover_image_mentions,
    discover_image_references,
    load_image_attachments,
    with_message_text,
)
from chrys.orchestration.engine.run.finalizer import TurnFinalizer, _expire_current_run_scope
from chrys.orchestration.engine.run.prompt_content import PromptContentPreparer
from chrys.orchestration.engine.run.retry import RetryCoordinator
from chrys.orchestration.engine.run.runner import TurnRunner
from chrys.orchestration.engine.run.runtime_skills import RuntimeSkillRefresher
from chrys.orchestration.engine.run.shortcut_commands import ShortcutCommands
from chrys.orchestration.engine.run.turn_hooks import PromptSubmitGate, TurnHookDispatcher
from chrys.orchestration.engine.run.turn_state import CurrentTurnInput, TurnRuntimeState
from chrys.orchestration.engine.run.working_dir import refuse_while_working_dir_missing
from chrys.orchestration.engine.state.machine import EngineState, EngineStateMachine, Trigger
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.orchestration.invoker.contracts import AbortCause
from chrys.orchestration.invoker.resources import TurnTaskBinding
from chrys.service.hooks.schema import HookDecision
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.session.history import SessionHistoryManager
from chrys.service.session.persistence import SessionPersistence
from chrys.service.trajectory.preparation import (
    PreparationOutcome,
    PreparationScope,
    PreparationTrace,
    input_admission_wait,
)

if TYPE_CHECKING:
    from chrys.foundation.trajectory.context import TrajectoryContext
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.service.hooks.operation import HookOperation


_IMAGE_COMPRESSION_TIMEOUT_SECONDS = 30.0

_COORDINATOR_ENGINE_NOT_STARTED = msg(
    "coordinator.engine_not_started",
    fallback="Engine not started",
)
_COORDINATOR_INTERRUPT_IGNORED_LOADING = msg(
    "coordinator.interrupt_ignored_loading",
    fallback="Interrupt ignored while agent infrastructure is loading.",
)
_COORDINATOR_PROMPT_SHORTCUT_BUSY = msg(
    "coordinator.prompt_shortcut_busy",
    fallback="A shortcut's prompt that runs commands is not added to a running task. Send it when the task ends.",
)
_COORDINATOR_PROMPT_ADMISSION_CONFLICT = msg(
    "coordinator.prompt_admission_conflict",
    fallback="Prompt could not be admitted because another turn started.",
)


class TurnCoordinator:
    """Event-facing facade for one main-agent turn lifecycle.

    Current queries re-read live values through current and session at each use.
    Admission captures retain identities such as ActiveInjectionTarget,
    CurrentRunScope, and RebuildControlToken across awaits and compare them
    at their validation checkpoints.
    """

    def __init__(
        self,
        *,
        turn_state: TurnRuntimeState,
        current: CurrentAgent,
        session: ActiveSession,
        permits: LifecyclePermits,
        writer: SessionWriter,
        loader: AgentLoader,
        hooks: TurnHookDispatcher,
        bus: EventBus,
        fsm: EngineStateMachine,
        history: SessionHistoryManager,
        trajectory_recorder: TrajectoryRecorder,
        workspace_change_tracker: WorkspaceChangeTracker,
        settings_handle: SettingsHandle,
        persistence: SessionPersistence,
        on_successful_turn: Callable[[], None],
        on_turn_started: Callable[[], None],
        prompt_content_preparer: PromptContentPreparer | None = None,
    ) -> None:
        self._turn_state = turn_state
        self._current = current
        self._session = session
        self._permits = permits
        self._writer = writer
        self._loader = loader
        self._hooks = hooks
        self._bus = bus
        self._fsm = fsm
        self._history = history
        self._trajectory_recorder = trajectory_recorder
        self._workspace_change_tracker = workspace_change_tracker
        self._settings_handle = settings_handle
        self._persistence = persistence
        self._on_successful_turn = on_successful_turn
        self._on_turn_started = on_turn_started
        self._gate = PromptSubmitGate(session=self._session, current=self._current, bus=self._bus, fsm=self._fsm)
        self._content = prompt_content_preparer or PromptContentPreparer(
            session=self._session,
            current=self._current,
            bus=self._bus,
            history=self._history,
            fsm=self._fsm,
            discover_mentions=discover_image_mentions,
            discover_references=discover_image_references,
            load_attachments=load_image_attachments,
            compression_timeout_seconds=_IMAGE_COMPRESSION_TIMEOUT_SECONDS,
            image_send_limit=self._image_send_limit,
        )
        self._skills = RuntimeSkillRefresher(
            current=self._current, loader=self._loader, session=self._session, bus=self._bus
        )
        self._shortcut_commands = ShortcutCommands(
            bus=self._bus,
            session=self._session,
            lease=lambda: self._turn_state.lease,
        )
        self._injector = ActiveTurnInjector(
            turn_state=self._turn_state,
            current=self._current,
            permits=self._permits,
            session=self._session,
            fsm=self._fsm,
            bus=self._bus,
            gate=self._gate,
            content=self._content,
            skills=self._skills,
        )
        self._finalizer = TurnFinalizer(
            current=self._current,
            session=self._session,
            turn_state=self._turn_state,
            writer=self._writer,
            history=self._history,
            trajectory_recorder=self._trajectory_recorder,
            fsm=self._fsm,
            workspace_change_tracker=self._workspace_change_tracker,
            settings_handle=self._settings_handle,
            bus=self._bus,
            persistence=self._persistence,
            on_successful_turn=self._on_successful_turn,
            hooks=self._hooks,
        )

    def _image_send_limit(self) -> int:
        """Read the send limit as the settings stand now: a reload may have changed it."""
        return self._settings_handle.settings.image_send_limit_bytes()

    def _runner(self) -> TurnRunner:
        """Create the service for one execution attempt."""
        return TurnRunner(
            current=self._current,
            session=self._session,
            turn_state=self._turn_state,
            history=self._history,
            workspace_change_tracker=self._workspace_change_tracker,
            settings_handle=self._settings_handle,
            trajectory_recorder=self._trajectory_recorder,
            bus=self._bus,
            fsm=self._fsm,
            on_turn_started=self._on_turn_started,
            finalizer=self._finalizer,
            hooks=self._hooks,
            skills=self._skills,
            content=self._content,
            retry_factory=self._retry,
        )

    def _retry(self) -> RetryCoordinator:
        """Create the service for one execution attempt."""
        return RetryCoordinator(
            turn_state=self._turn_state,
            current=self._current,
            permits=self._permits,
            session=self._session,
            fsm=self._fsm,
            history=self._history,
            bus=self._bus,
            trajectory_recorder=self._trajectory_recorder,
            gate=self._gate,
            injector=self._injector,
            content=self._content,
            skills=self._skills,
            retry_and_save=self.retry_and_save,
        )

    @property
    def turn_state(self) -> TurnRuntimeState:
        """The runtime state shared with lifecycle operations."""
        return self._turn_state

    @property
    def run_task(self) -> asyncio.Task[None] | None:
        """Current run task, including a retry task installed during finalization."""
        return self._turn_state.lease.run_task

    @property
    def current_input(self) -> CurrentTurnInput:
        """Current prompt data used by crash-recovery checkpoint writes."""
        return self._turn_state.current_input

    @property
    def is_turn_active(self) -> bool:
        """Return whether the FSM currently admits active-turn injection."""
        return self._fsm.is_running()

    @property
    def is_turn_lifecycle_active(self) -> bool:
        """Return whether execution or post-run finalization is still in flight."""
        return self._turn_state.lease.execution_busy()

    def was_run_task_finally_saved(self, task: asyncio.Task[None]) -> bool:
        """Return whether *task* completed the durable final session save."""
        return self._turn_state.lease.was_run_task_finally_saved(task)

    def clear_run_task(self) -> None:
        """Clear the visible run task after shutdown has drained or cancelled it."""
        self._turn_state.lease.release_run_task()

    async def wait_for_run_task(self) -> None:
        """Wait for the active run-task chain, preserving task failure semantics."""
        await self._turn_state.lease.observe_run_task_chain(propagate_inner_cancel=True)
        await self._turn_state.lease.settle_notifications()

    @property
    def can_retry(self) -> bool:
        """Return whether the engine may start an immediate retry right now."""
        return self._retry().can_retry()

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
        user_message_id: str | None = None,
        typed_text: str | None = None,
    ) -> None:
        """Execute a fresh agent pass against the current engine state."""

        await self._runner().run_fresh(
            text,
            created_at=created_at,
            contents=contents,
            run_scope=run_scope,
            injection_window=injection_window,
            admission_preparation=admission_preparation,
            binding_failure=binding_failure,
            user_message_id=user_message_id,
            typed_text=typed_text,
        )

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
        """Execute a retry/resume pass against the current engine state."""

        await self._runner().run_retry(
            additional_text,
            created_at=created_at,
            run_scope=run_scope,
            injection_window=injection_window,
            admission_preparation=admission_preparation,
            binding_failure=binding_failure,
        )

    async def finalize_current_run(self) -> None:
        """Finalize the current executor pass against the current engine state."""

        await self._runner().finalize_current_run()

    async def on_user_message(self, event: UserMessage) -> None:
        """Handle a user message by running the executor as an async task.

        On accepted fresh turns, mutates ``event.prepared_contents`` before
        returning so sequential ``EventBus.publish`` callers can render the exact
        multimodal payload that will be sent to the model.
        """
        if await self._turn_state.lease.refuse_while_workflow_active(self._bus, self._session.session_id):
            return
        # Track injection-identified submits so a concurrent cancel knows an
        # admission is still in flight to observe its mark (no-op for None).
        turn_state = self._turn_state
        turn_state.begin_inflight_injection(event.injection_id)
        preparation_tracker = PreAdmissionPreparationTracker()
        handed_off = False
        try:
            await self._start_pre_admission_preparation(preparation_tracker)
            handed_off = await self._admit_user_message(event, preparation_tracker=preparation_tracker)
        except asyncio.CancelledError:
            preparation = preparation_tracker.preparation
            if (
                preparation is not None
                and not preparation_tracker.preparation_handed_off
                and not preparation.finished_state
            ):
                preparation.finished_soon(outcome=PreparationOutcome.CANCELLED)
            raise
        except BaseException:
            preparation = preparation_tracker.preparation
            if (
                preparation is not None
                and not preparation_tracker.preparation_handed_off
                and not preparation.finished_state
            ):
                preparation.finished_soon(outcome=PreparationOutcome.PREPARATION_FAILED)
            raise
        finally:
            turn_state.lease.deregister_pre_admission_preparation(preparation_tracker.current)
            turn_state.finish_inflight_injection(event.injection_id)
            preparation = preparation_tracker.preparation
            if (
                preparation is not None
                and not handed_off
                and not preparation_tracker.preparation_handed_off
                and not preparation.finished_state
            ):
                await preparation.finished(outcome=PreparationOutcome.PREPARATION_FAILED)

    async def _admit_user_message(
        self,
        event: UserMessage,
        *,
        preparation_tracker: PreAdmissionPreparationTracker,
    ) -> bool:
        """Admit one message and report whether its preparation changed owners."""
        admission: PromptAdmissionScope | None = None
        admission_released = False
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
                await self._bus.publish(
                    Error(
                        code="not_ready",
                        message="Engine not started",
                        display_message=_COORDINATOR_ENGINE_NOT_STARTED.bind(),
                        session_id=self._session.session_id,
                    )
                )
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.NOT_READY)
                return False

            # A run is in-flight — FSM covers RUNNING, PENDING_RETRY, and
            # AWAITING_SUB_AGENTS (parent task pinned on a sub-agent's
            # ``pending_decision`` future). Gate on the FSM so the intent
            # travels with the state contract, not with the executor's
            # ``_running`` implementation detail — a new state that should
            # also inject only has to be added to
            # :meth:`EngineStateMachine.is_running` to be covered here.
            if self._fsm.is_running():
                if await self._refuse_shortcut_injection(event, preparation):
                    return False
                return await self._injector.inject(
                    event.text,
                    created_at=event.timestamp,
                    route="fsm_active",
                    reject_images_without_target=True,
                    injection_id=event.injection_id,
                    preparation=preparation,
                    preparation_tracker=preparation_tracker,
                )

            # If a previous run task is still cleaning up (e.g., interrupt teardown),
            # wait for it to finish before starting a new run. Without this, the
            # message would be injected into the dying run and then drained/lost.
            if self._turn_state.lease.run_task is not None and not self._turn_state.lease.run_task.done():
                if self._current.loaded.bindings.state.running:
                    # TurnBindings is actively running (not just cleaning up) — inject
                    if await self._refuse_shortcut_injection(event, preparation):
                        return False
                    return await self._injector.inject(
                        event.text,
                        created_at=event.timestamp,
                        route="executor_fallback",
                        reject_images_without_target=True,
                        injection_id=event.injection_id,
                        preparation=preparation,
                        preparation_tracker=preparation_tracker,
                    )
                # TurnBindings finished but _post_run() is still running (session
                # save, FSM transition). Wait for cleanup to complete so the
                # new run starts from a clean state.
                await self._wait_for_pre_admission_gate(self._wait_for_existing_run_task, preparation_tracker)
                preparation = preparation_tracker.preparation

            if self._turn_state.lease.prompt_admission_closed:
                await self._wait_for_pre_admission_gate(
                    self._turn_state.lease.wait_for_prompt_admission_open,
                    preparation_tracker,
                )
                continue
            if self._turn_state.lease.active_admission_count() > 0:
                await self._wait_for_pre_admission_gate(
                    self._turn_state.lease.wait_for_active_admissions_idle,
                    preparation_tracker,
                )
                continue
            if await refuse_while_working_dir_missing(self._bus, self._session):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.REJECTED)
                return False
            # A workflow run may have taken the lease while this message was preparing.
            if await self._turn_state.lease.refuse_while_workflow_active(self._bus, self._session.session_id):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.REJECTED)
                return False
            admission = self._turn_state.lease.reserve_prompt_admission(
                kind="fresh",
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
            # A locked-input submit can outlive its run and arrive here as a
            # fresh turn; honor a cancel that landed while it waited so Esc
            # never lets the withdrawn text start a new turn.
            if await self._abandon_cancelled_injection_submit(event):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.CANCELLED)
                return False
            admission_session_id = self._session.session_id
            operation = self._gate.new_operation(event.text)
            self._turn_state.input_operations.add(operation, injection_id=event.injection_id)
            decision = await self._evaluate_user_prompt_submit(
                operation,
                injected=False,
                target_operation_id=preparation.committed_operation_id if preparation is not None else None,
                trajectory_context=preparation.context if preparation is not None else None,
            )
            if decision.invalidated:
                # Withdrawn, or its session or build replaced, while its hooks checked it.
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.CANCELLED)
                return False
            if not self._admission_owner_is_current(admission):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.OWNER_CHANGED)
                return False
            if await self._gate.handle_decision(decision, refused_input="prompt"):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.REJECTED)
                return False

            if event.prepared_contents is not None:
                contents = list(event.prepared_contents)
                if await self.reject_text_only_prepared_contents(
                    event.text,
                    contents,
                    admission=admission,
                    event_session_id=admission_session_id,
                ):
                    if preparation is not None:
                        await preparation.finished(outcome=PreparationOutcome.IMAGE_REJECTED)
                    return False
            else:
                contents = await self.prepare_user_contents(
                    event.text,
                    admission=admission,
                    event_session_id=admission_session_id,
                )
                if contents is None:
                    if preparation is not None:
                        await preparation.finished(outcome=PreparationOutcome.PREPARATION_FAILED)
                    return False
                event.prepared_contents = contents
            if not self._admission_owner_is_current(admission):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.OWNER_CHANGED)
                return False
            # A prompt shortcut's commands: the last step that can refuse with no side
            # effects left behind it — the hooks and the images were checked first.
            expanded = await self._shortcut_commands.expand(
                event,
                operation,
                admission=admission,
                preparation=preparation,
                should_publish=self._publication_guard_for_admission(admission),
            )
            if expanded.text is None:
                if preparation is not None:
                    await preparation.finished(outcome=expanded.outcome)
                return False
            send_text = expanded.text
            if send_text != event.text:
                # Only a message whose commands ran says something else; the rest keep their contents.
                contents = with_message_text(contents, send_text)
                event.prepared_contents = contents
            if not self._admission_owner_is_current(admission):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.OWNER_CHANGED)
                return False

            # Final cancel check after all awaited preparation; task creation
            # below is synchronous, so a cancel can no longer interleave.
            if await self._abandon_cancelled_injection_submit(event):
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.CANCELLED)
                return False
            if not self._fresh_prompt_fsm_accepts():
                await self._publish_prompt_admission_conflict()
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.CONFLICT)
                return False
            if self._turn_state.lease.run_task is not None and not self._turn_state.lease.run_task.done():
                await self._publish_prompt_admission_conflict()
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.CONFLICT)
                return False
            # The submit point: nothing below awaits before the run task exists, so the input
            # the hooks checked is the one this turn takes.
            if operation.commit_input(event.text) is not None:
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.CANCELLED)
                return False
            _expire_current_run_scope(self._turn_state, self._current, self._turn_state.lease.current_run_scope)

            # After a failed/interrupted run, remove status markers (interrupted,
            # error, turn_marker) so the new message appends cleanly. Then remove
            # any orphaned user message that got no model response (e.g. immediate
            # 401 error) — this matches the live UX where the failed message
            # disappeared when the user typed a new one. If the model DID produce
            # output (tool calls, partial text) the user message is preserved.
            #
            # Also check the history itself for trailing error markers — after a
            # session restore the FSM is reset to IDLE, but the saved history
            # still carries the error state from the previous run.
            if (
                self._fsm.state in (EngineState.INTERRUPTED, EngineState.FAILED)
                or self._history.has_trailing_error_markers()
            ):
                self._history.remove_trailing_markers()
                self._history.remove_orphaned_user_message()

            reminder_middleware = self._current.loaded.reminder_middleware if self._current.loaded is not None else None
            if reminder_middleware is None:
                self._turn_state.lease.release_prompt_admission(admission)
                admission_released = True
                self._fsm.try_transition(Trigger.USER_MESSAGE)
                self._turn_state.lease.run_task = asyncio.create_task(
                    self.run_and_save(
                        send_text,
                        created_at=event.timestamp,
                        contents=contents,
                        admission_preparation=preparation,
                        user_message_id=event.client_message_id,
                        typed_text=event.text,
                    )
                )
                # Record the opener synchronously with its task: a Stop followed by
                # Retry must find it even before preparation has populated history.
                self._turn_state.set_current_input(send_text, contents, event.timestamp)
                self._queue_prompt_hook_reminders(decision, injected=False)
                event.started_turn = True
                return True

            reminder_scope = reminder_middleware.create_current_run_scope()
            promotion = self._turn_state.lease.promote_fresh_admission_to_run(
                admission,
                reminder_scope=reminder_scope,
                make_task=lambda scope, window: asyncio.create_task(
                    self.run_and_save(
                        send_text,
                        created_at=event.timestamp,
                        contents=contents,
                        run_scope=scope,
                        injection_window=window,
                        admission_preparation=preparation,
                        user_message_id=event.client_message_id,
                        typed_text=event.text,
                    )
                ),
            )
            if not promotion.promoted:
                reminder_middleware.expire_current_run_scope(reminder_scope)
                if promotion.conflict:
                    await self._publish_prompt_admission_conflict()
                if preparation is not None:
                    await preparation.finished(
                        outcome=PreparationOutcome.CONFLICT if promotion.conflict else PreparationOutcome.OWNER_CHANGED
                    )
                return False
            admission_released = True
            # Record the opener synchronously with its task: a Stop followed by
            # Retry must find it even before preparation has populated history.
            self._turn_state.set_current_input(send_text, contents, event.timestamp)
            self._fsm.try_transition(Trigger.USER_MESSAGE)
            self._queue_prompt_hook_reminders(decision, injected=False)
            event.started_turn = True
            return True
        finally:
            self._turn_state.input_operations.discard(operation)
            if admission is not None and not admission_released:
                self._turn_state.lease.release_prompt_admission(admission)
            if admission is not None:
                # After the turn started (or the message was refused): the execution
                # goes from the commands straight to the turn, never through idle.
                self._turn_state.lease.end_prompt_commands(admission)

    async def on_user_interrupt(self, _event: UserInterrupt) -> None:
        """Handle user interrupt.

        Cascades to every live sub-agent controller first so paused
        sub-agents resolve via the cascade-abort branch (otherwise their
        ``pending_decision`` future would keep their ``_invoke``
        coroutine pinned forever and the subsequent task cancel below
        would leak). Then sets the interrupt flag / cancels the parent
        task. It then either binds cancellation to the exact pre-executor
        run task or interrupts the active executor; ``run_and_save``
        detects ``was_interrupted`` and rolls back history.

        An input still being checked has no run task yet: the interrupt ends
        it, and any confirmation its hooks wait on, before anything else.
        """
        self._turn_state.input_operations.invalidate_all("interrupted")
        if self._permits.agent_loading:
            await self._bus.publish(
                Warning(
                    code="agent_loading_interrupt_ignored",
                    message="Interrupt ignored while agent infrastructure is loading.",
                    display_message=_COORDINATOR_INTERRUPT_IGNORED_LOADING.bind(),
                    session_id=self._session.session_id,
                )
            )
            return
        executor = self._current.loaded.bindings if self._current.loaded is not None else None
        interrupt_task = self._turn_state.lease.run_task
        self._trajectory_recorder.interrupt_requested_soon()
        if executor is not None and not executor.state.running:
            self._turn_state.lease.request_pre_executor_interrupt(interrupt_task)
        if self._current.loaded is not None and self._current.loaded.sub_agent_tools is not None:
            await self._current.loaded.sub_agent_tools.cascade_abort_all()
        if executor is not None and interrupt_task is not None and self._turn_state.lease.run_task is interrupt_task:
            if executor.state.running:
                handle = executor.backend.active_handle
                if handle is not None:
                    await executor.backend.abort(handle, AbortCause.USER_CANCEL)
            else:
                self._turn_state.lease.request_pre_executor_interrupt(interrupt_task)
        self.schedule_user_interrupt_hook()

    async def on_user_retry(self, event: UserRetry) -> None:
        """Handle retry requests and route them to immediate or pending execution."""
        await self._retry().handle_user_retry(event)

    async def on_user_inject(self, event: UserInject) -> None:
        """Handle user injection (prompt inserted before next model call)."""
        self._turn_state.begin_inflight_injection(event.injection_id)
        preparation_tracker = PreAdmissionPreparationTracker()
        handed_off = False
        try:
            await self._start_pre_admission_preparation(preparation_tracker)
            await self._wait_for_pre_admission_gate(self._permits.wait_for_agent_load_idle, preparation_tracker)
            preparation = preparation_tracker.preparation
            if self._current.loaded is None:
                if preparation is not None:
                    await preparation.finished(outcome=PreparationOutcome.NOT_READY)
                return
            handed_off = await self._injector.inject(
                event.text,
                created_at=event.timestamp,
                route="fsm_active",
                reject_images_without_target=False,
                injection_id=event.injection_id,
                preparation=preparation,
                preparation_tracker=preparation_tracker,
            )
        except asyncio.CancelledError:
            preparation = preparation_tracker.preparation
            if (
                preparation is not None
                and not preparation_tracker.preparation_handed_off
                and not preparation.finished_state
            ):
                preparation.finished_soon(outcome=PreparationOutcome.CANCELLED)
            raise
        except BaseException:
            preparation = preparation_tracker.preparation
            if (
                preparation is not None
                and not preparation_tracker.preparation_handed_off
                and not preparation.finished_state
            ):
                preparation.finished_soon(outcome=PreparationOutcome.PREPARATION_FAILED)
            raise
        finally:
            self._turn_state.lease.deregister_pre_admission_preparation(preparation_tracker.current)
            self._turn_state.finish_inflight_injection(event.injection_id)
            preparation = preparation_tracker.preparation
            if (
                preparation is not None
                and not handed_off
                and not preparation_tracker.preparation_handed_off
                and not preparation.finished_state
            ):
                await preparation.finished(outcome=PreparationOutcome.PREPARATION_FAILED)

    async def on_user_inject_cancel(self, event: UserInjectCancel) -> None:
        """Withdraw a queued mid-run injection before the model sees it.

        Covers both pending phases: an injection already queued on the
        middleware is removed here directly (including its pre-appended
        approval judge context), and an injection still in awaited admission
        aborts via a cancel mark it observes before committing. Everything
        below is synchronous, so the injection cannot move between phases
        mid-cancel. A cancel that matches neither phase is a no-op — the
        injection already resolved (for a consumed one, the ``consumed=True``
        result tells frontends the text reached the model) and recording a
        mark would leave it dangling for the rest of the session.
        """
        injection_id = event.injection_id
        if not injection_id:
            return
        executor = self._current.loaded.bindings if self._current.loaded is not None else None
        if executor is None:
            if self._turn_state.is_injection_inflight(injection_id):
                self._turn_state.mark_injection_cancelled(injection_id)
            return
        removed = executor.cancel_injection(injection_id)
        if removed is None:
            if self._turn_state.is_injection_inflight(injection_id):
                self._turn_state.mark_injection_cancelled(injection_id)
            return
        executor.approval.remove_user_message(removed.text)
        if removed.preparation is not None:
            removed.preparation.finished_soon(
                outcome=PreparationOutcome.CANCELLED,
                target_turn_id=removed.target_turn_id,
            )
        await self._bus.publish(
            UserInjectResult(
                text=removed.text,
                consumed=False,
                created_at=removed.created_at,
                injection_id=injection_id,
                session_id=self._session.session_id,
            )
        )

    async def _refuse_shortcut_injection(self, event: UserMessage, preparation: PreparationTrace | None) -> bool:
        """Refuse a message carrying a prompt shortcut that would join the running turn; False for any other.

        Its commands run before a turn of its own, never into one that is
        already running (the frontend keeps such a draft; this covers a turn that
        started while the message was on its way).
        """
        if event.prompt_shortcut is None:
            return False
        await self._bus.publish(
            Error(
                code="prompt_shortcut_busy",
                message="A message running prompt shortcut commands cannot join a running turn",
                display_message=_COORDINATOR_PROMPT_SHORTCUT_BUSY.bind(),
                session_id=self._session.session_id,
            )
        )
        if preparation is not None:
            await preparation.finished(outcome=PreparationOutcome.REJECTED)
        return True

    async def _abandon_cancelled_injection_submit(self, event: UserMessage) -> bool:
        """Publish an abandoned result and return True for a cancelled locked submit."""
        if not self._turn_state.discard_injection_cancellation(event.injection_id):
            return False
        await self._bus.publish(
            UserInjectResult(
                text=event.text,
                consumed=False,
                created_at=event.timestamp,
                injection_id=event.injection_id,
                session_id=self._session.session_id,
            )
        )
        return True

    def check_pending_retry(self) -> None:
        """If a retry was queued while the executor was running, start it now."""
        self._retry().start_pending_retry_if_due()

    async def prepare_user_contents(
        self,
        text: str,
        *,
        discovered: AttachmentDiscoveryResult | None = None,
        admission: PromptAdmissionScope | None = None,
        event_session_id: str | None = None,
    ) -> list[Any] | None:
        """Return user-message contents or publish a recoverable attachment error."""
        result = await self._content.prepare_fresh(
            text,
            discovered=discovered,
            event_session_id=event_session_id,
            should_publish=self._publication_guard_for_admission(admission),
        )
        return None if result is None else result.contents

    async def reject_text_only_prepared_contents(
        self,
        text: str,
        contents: list[Any],
        *,
        admission: PromptAdmissionScope,
        event_session_id: str | None,
    ) -> bool:
        """Reject prebuilt image contents before a fresh prompt is promoted."""
        return await self._content.reject_text_only_prepared_contents(
            text,
            contents,
            event_session_id=event_session_id,
            should_publish=self._publication_guard_for_admission(admission),
        )

    async def _wait_for_existing_run_task(self) -> None:
        """Wait for current run-task cleanup, if any."""
        task = self._turn_state.lease.run_task
        if task is not None and not task.done():
            await task

    def _admission_owner_is_current(self, admission: PromptAdmissionScope) -> bool:
        """Return whether *admission* still belongs to the live session/build owner."""
        return (
            self._permits.session_generation == admission.session_generation
            and self._permits.build_generation == admission.build_generation
        )

    def _publication_guard_for_admission(
        self,
        admission: PromptAdmissionScope | None,
    ) -> Callable[[], bool] | None:
        """Return a prompt-preparation side-effect guard for an exact admission."""
        if admission is None:
            return None
        return lambda: self._admission_owner_is_current(admission)

    def _fresh_prompt_fsm_accepts(self) -> bool:
        """Return whether the current FSM state can start a fresh user turn."""
        return self._fsm.state in (
            EngineState.UNINITIALIZED,
            EngineState.IDLE,
            EngineState.INTERRUPTED,
            EngineState.FAILED,
        )

    async def _publish_prompt_admission_conflict(self) -> None:
        """Publish a deterministic conflict when a fresh prompt cannot promote."""
        await self._bus.publish(
            Error(
                code="prompt_admission_conflict",
                message="Prompt could not be admitted because another turn started.",
                display_message=_COORDINATOR_PROMPT_ADMISSION_CONFLICT.bind(),
                session_id=self._session.session_id,
            )
        )

    async def _evaluate_user_prompt_submit(
        self,
        operation: HookOperation,
        *,
        injected: bool | None,
        target_operation_id: str | None = None,
        trajectory_context: TrajectoryContext | None = None,
    ) -> HookDecision:
        """Run ``user_prompt_submit`` hooks on *operation*'s input without applying reminder side effects."""
        return await self._gate.evaluate(
            operation,
            injected=injected,
            target_operation_id=target_operation_id,
            trajectory_context=trajectory_context,
        )

    def _open_pre_turn_preparation(self) -> PreparationTrace | None:
        """Open a session-root preparation scope before prompt admission awaits."""
        context = self._trajectory_recorder.context()
        if context is not None:
            context = context.with_turn(None).with_run(None)
        return PreparationTrace.open(
            scope=PreparationScope.PRE_TURN,
            phase="input_admission",
            context=context,
        )

    async def _start_pre_admission_preparation(self, tracker: PreAdmissionPreparationTracker) -> None:
        """Start and register a preparation against the current session context."""
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
        wait_for_gate: Callable[[], Awaitable[Any]],
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

    def _queue_prompt_hook_reminders(
        self,
        decision: HookDecision | None,
        *,
        injected: bool | None,
    ) -> None:
        """Apply non-blocking prompt-submit hook reminders after prompt validation passes."""
        self._gate.queue_reminders(decision, injected=injected)

    def schedule_user_interrupt_hook(self) -> None:
        """Schedule the asynchronous user-interrupt lifecycle hook."""
        self._hooks.schedule_user_interrupt()

    @property
    def conversation_revision(self) -> int:
        """Monotonic fresh/retry lifecycle revision for stale projections."""
        return self._turn_state.lease.conversation_revision

    def execution(self) -> ExecutionSnapshot:
        """What the execution lease is running right now."""
        return self._turn_state.lease.execution()

    def execution_busy(self) -> bool:
        """Existing lifecycle busy predicate, including preparation and final save."""
        return self._turn_state.lease.execution_busy()

    def turn_accepts_injection(self) -> bool:
        """Existing FSM is_running predicate, including pending retry and child waits."""
        return self._fsm.is_running()

    @property
    def turn_lifecycle_task(self) -> asyncio.Task[None] | None:
        """Return the currently owned execution/finalization task.

        Callers that need a boundary for one captured turn should retain this
        exact task instead of later awaiting the replaceable run-task chain.
        """
        return self.run_task

    def was_turn_lifecycle_saved(self, task: asyncio.Task[None]) -> bool:
        """Return whether *task* completed its final session save successfully."""
        return self.was_run_task_finally_saved(task)

    async def run_and_save(
        self,
        text: str,
        created_at: datetime | str | None = None,
        contents: list[Any] | None = None,
        *,
        run_scope: CurrentRunScope | None = None,
        injection_window: CurrentRunInjectionWindow | None = None,
        admission_preparation: PreparationTrace | None = None,
        user_message_id: str | None = None,
        typed_text: str | None = None,
    ) -> None:
        """Execute the agent and auto-save the session afterward."""
        task = asyncio.current_task()
        unbind = None
        binding_failure: Exception | None = None
        try:
            if self._current.loaded is not None and task is not None:
                unbind = self._current.loaded.conversation.bind_operation(
                    TurnTaskBinding(
                        task,
                        self._current.loaded.bindings.backend.latch_abort if self._current.loaded is not None else None,
                    )
                )
        except Exception as exc:
            # Promotion already owns the Turn. Let its ordinary admission
            # failure path preserve input, finalize, save, and release it.
            binding_failure = exc
        try:
            await self.run_fresh(
                text,
                created_at=created_at,
                contents=contents,
                run_scope=run_scope,
                injection_window=injection_window,
                admission_preparation=admission_preparation,
                binding_failure=binding_failure,
                user_message_id=user_message_id,
                typed_text=typed_text,
            )
        finally:
            if unbind is not None:
                unbind()

    async def retry_and_save(
        self,
        additional_text: str = "",
        created_at: datetime | str | None = None,
        *,
        run_scope: CurrentRunScope | None = None,
        injection_window: CurrentRunInjectionWindow | None = None,
        admission_preparation: PreparationTrace | None = None,
    ) -> None:
        """Resume the agent from current state and auto-save afterward.

        When *additional_text* is non-empty, the executor uses it as the
        mid-turn continuation prompt (instead of the placeholder
        ``"continue"``) and preserves it in history as a real user turn.
        """
        task = asyncio.current_task()
        unbind = None
        binding_failure: Exception | None = None
        try:
            if self._current.loaded is not None and task is not None:
                unbind = self._current.loaded.conversation.bind_operation(
                    TurnTaskBinding(
                        task,
                        self._current.loaded.bindings.backend.latch_abort if self._current.loaded is not None else None,
                    )
                )
        except Exception as exc:
            # Promotion already owns the Turn. Let its ordinary admission
            # failure path preserve input, finalize, save, and release it.
            binding_failure = exc
        try:
            await self.run_retry(
                additional_text,
                created_at=created_at,
                run_scope=run_scope,
                injection_window=injection_window,
                admission_preparation=admission_preparation,
                binding_failure=binding_failure,
            )
        finally:
            if unbind is not None:
                unbind()

    async def on_sub_agent_retry(self, event: InvocationRetryRequested) -> None:
        """Route a user's per-card Retry click to the owning controller."""
        await sub_agent_coordination.on_sub_agent_retry(self._current, event)

    async def on_sub_agent_abort(self, event: InvocationAbortRequested) -> None:
        """Route a user's per-card Abort click to the owning controller."""
        await sub_agent_coordination.on_sub_agent_abort(self._current, event)

    async def on_sub_agent_paused(self, event: InvocationPaused) -> None:
        """Track a newly paused sub-agent and drive FSM / marker.

        Idempotent on the paused set — if the same id arrives twice
        (controller re-publishes after retry exhaustion, for example)
        the FSM transition only fires on the 0→1 edge.

        Defensive FSM guard: if the parent run has already terminated
        (e.g. the bus is dispatching a stale pause event that was queued
        before the parent's task was cancelled and ``_post_run`` ran),
        drop the event silently rather than re-inserting a marker on top
        of an already-terminal ``interrupted``/``error`` marker.  The
        parent invariant ("parent run ends only after all sub-agents
        resolve") should make this unreachable, but defense in depth
        keeps history well-formed if that invariant ever breaks.
        """
        await sub_agent_coordination.on_sub_agent_paused(
            self._turn_state, self._history, self._fsm, self._trajectory_recorder, event
        )

    async def on_sub_agent_unpaused(
        self,
        event: InvocationResumed | InvocationAborted | InvocationCascadeAborted,
    ) -> None:
        """Common handler — retry/abort/cascade all remove the invocation from the paused set.

        FSM transitions only on the N→0 edge (last paused sub-agent
        resolved).  Marker is updated or stripped accordingly.
        """
        await sub_agent_coordination.on_sub_agent_unpaused(
            self._turn_state, self._history, self._fsm, self._trajectory_recorder, event
        )
