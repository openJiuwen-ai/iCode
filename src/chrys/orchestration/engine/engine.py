# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent engine entry point, event subscriptions, and component facades.

Assembly supplies ten long-lived components: session, permits, current, writer,
usage_publisher, loader, lifecycle, rollback, turns, and controls. The coordinator
owns turn state and its execution lease; assembly shares hook dispatch between
the loader and coordinator. current.loaded owns one installed build, changes on
install or release, and is None after shutdown. current.manifest describes the
most recently installed build and retains its values after the resources close.

The engine subscribes to frontend events, routes them to its collaborators,
and exposes facades for session state, build details, and runtime controls.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.config.settings import Settings, persist_approval_mode
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.events.types import (
    AgentProfileSwitch,
    AgentRuntimeDetails,
    BackgroundTaskStop,
    Error,
    InvocationAborted,
    InvocationAbortRequested,
    InvocationCascadeAborted,
    InvocationPaused,
    InvocationResumed,
    InvocationRetryRequested,
    ProfileSwitched,
    SessionClear,
    SessionDelete,
    SessionFork,
    SessionNew,
    SessionOnlySwitch,
    SessionRestore,
    SetApprovalMode,
    SetModelProfile,
    SettingsReload,
    UserInject,
    UserInjectCancel,
    UserInterrupt,
    UserMessage,
    UserRetry,
    UserRollback,
    WorkspaceChange,
)
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.tool_kinds import KIND_SUB_AGENT
from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.orchestration.engine import trajectory as trajectory_recorder
from chrys.orchestration.engine.loader import AgentLoader
from chrys.orchestration.engine.rollback import RollbackController
from chrys.orchestration.engine.run.coordinator import TurnCoordinator
from chrys.orchestration.engine.session_lifecycle import SessionLifecycle
from chrys.orchestration.engine.side_questions import SideQuestions
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.controls import RuntimeControls
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
from chrys.orchestration.engine.state.machine import EngineState, EngineStateMachine
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.orchestration.engine.usage import UsagePublisher
from chrys.orchestration.workflows.coordinator import WorkflowCoordinator
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mutations.coordination import MutationCoordinator
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.session.history import SessionHistoryManager
from chrys.service.state.store import (
    SESSION_WRITE_LOCK_TIMEOUT_SECONDS,
    atomic_copy_file,
)
from chrys.service.todos.tracker import TodoTracker
from chrys.service.trajectory.state import TurnRecord, turn_records

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.execution import ExecutionSnapshot
    from chrys.service.hooks.confirmation import HookConfirmationService
    from chrys.service.hooks.run_log import HookRunLog
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile


# Session-keyed registry of live engines, for app features that run beside the session (the buddy).
_current_engines: dict[str, AgentEngine] = {}
_foreground_engine_session_id: str | None = None
logger = logging.getLogger(__name__)


def get_current_engine(session_id: str | None = None) -> AgentEngine | None:
    """Get an active AgentEngine instance.

    Used by buddy module to access conversation history.  A session id
    selects a specific engine; no id preserves the legacy "foreground"
    behavior for single-instance integrations.
    """
    if session_id:
        return _current_engines.get(session_id)
    if _foreground_engine_session_id is not None:
        engine = _current_engines.get(_foreground_engine_session_id)
        if engine is not None:
            return engine
    if len(_current_engines) == 1:
        return next(iter(_current_engines.values()))
    return None


def _set_current_engine(engine: AgentEngine) -> None:
    """Make an active engine findable by features that run beside its session."""
    global _foreground_engine_session_id
    if engine.session_id is None:
        return
    _current_engines[engine.session_id] = engine
    _foreground_engine_session_id = engine.session_id


def _unset_current_engine(engine: AgentEngine) -> None:
    """Take an engine out of the registry when it shuts down."""
    global _foreground_engine_session_id
    removed = [session_id for session_id, registered in _current_engines.items() if registered is engine]
    for session_id in removed:
        _current_engines.pop(session_id, None)
    if _foreground_engine_session_id in removed:
        _foreground_engine_session_id = next(reversed(_current_engines), None)


class AgentEngine:
    """Top-level orchestrator for the Chrys agent.

    Usage::

        bus = EventBus()
        engine = assemble_agent_engine(bus, settings)
        await engine.start(profile)
        # Frontend publishes UserMessage events → engine handles them
        await engine.shutdown()
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        session: ActiveSession,
        permits: LifecyclePermits,
        current: CurrentAgent,
        writer: SessionWriter,
        usage_publisher: UsagePublisher,
        loader: AgentLoader,
        lifecycle: SessionLifecycle,
        rollback: RollbackController,
        turns: TurnCoordinator,
        controls: RuntimeControls,
        settings_handle: SettingsHandle,
        model_registry: ModelProfileRegistry | None,
        fsm: EngineStateMachine,
        history: SessionHistoryManager,
        trajectory_recorder: trajectory_recorder.TrajectoryRecorder,
        workflows: WorkflowCoordinator,
        side_questions: SideQuestions,
        hook_runs: HookRunLog | None = None,
        hook_confirmations: HookConfirmationService | None = None,
    ) -> None:
        self._bus = bus
        self._session = session
        self._permits = permits
        self._current = current
        self._writer = writer
        self._usage_publisher = usage_publisher
        self._loader = loader
        self._lifecycle = lifecycle
        self._rollback = rollback
        self._turns = turns
        self._controls = controls
        self._settings_handle = settings_handle
        self._model_registry = model_registry
        self._fsm = fsm
        self._history = history
        self._trajectory_recorder = trajectory_recorder
        self._workflows = workflows
        self._side_questions = side_questions
        self._hook_runs = hook_runs
        self._hook_confirmations = hook_confirmations
        self._subscribed = False
        self._release_task: asyncio.Task[None] | None = None

    @property
    def turns(self) -> TurnCoordinator:
        """Turn orchestration and runtime state."""
        return self._turns

    @property
    def hook_runs(self) -> HookRunLog | None:
        """Every main-session hook run since the engine was built; None unless it records them."""
        return self._hook_runs

    @property
    def workflows(self) -> WorkflowCoordinator:
        """Workflow run admission and routing."""
        return self._workflows

    @property
    def lifecycle(self) -> SessionLifecycle:
        """Session orchestration and resource shutdown."""
        return self._lifecycle

    @property
    def rollback(self) -> RollbackController:
        """Session and workspace rollback operations."""
        return self._rollback

    @property
    def controls(self) -> RuntimeControls:
        """Profile, model, settings, and workspace controls."""
        return self._controls

    @property
    def loader(self) -> AgentLoader:
        """Candidate construction, installation, and build resource release."""
        return self._loader

    @property
    def usage_publisher(self) -> UsagePublisher:
        """Session usage accounting and ordered publication."""
        return self._usage_publisher

    @property
    def writer(self) -> SessionWriter:
        """Session saves and recovery checkpoint persistence."""
        return self._writer

    @property
    def current(self) -> CurrentAgent:
        """The current resources and last installed manifest."""
        return self._current

    @property
    def permits(self) -> LifecyclePermits:
        """Lifecycle admission and owner clocks."""
        return self._permits

    @property
    def session(self) -> ActiveSession:
        """The active session record."""
        return self._session

    @property
    def session_generation(self) -> int:
        """Engine-owned session generation for stale-owner detection."""
        return self._permits.session_generation

    @property
    def conversation_revision(self) -> int:
        """Monotonic fresh/retry lifecycle revision for stale projections."""
        return self._turns.conversation_revision

    @property
    def build_generation(self) -> int:
        """Engine-owned successful-build generation."""
        return self._permits.build_generation

    @property
    def workspace_primary_cwd(self) -> str:
        """Current normalized primary cwd for stale projection detection."""
        return self._session.workspace_cwd()

    @property
    def load_generation(self) -> int:
        """Engine-owned agent-load attempt generation."""
        return self._permits.load_generation

    @property
    def is_running(self) -> bool:
        return self._current.loaded is not None and self._current.loaded.bindings.state.running

    def execution_busy(self) -> bool:
        """Existing lifecycle busy predicate, including preparation and final save."""
        return self._turns.execution_busy()

    def execution(self) -> ExecutionSnapshot:
        """What the execution lease is running right now: nothing, a turn, or a workflow run."""
        return self._turns.execution()

    def turn_accepts_injection(self) -> bool:
        """Existing FSM is_running predicate, including pending retry and child waits."""
        return self._turns.turn_accepts_injection()

    @property
    def is_turn_active(self) -> bool:
        """True when a turn is in-flight and can accept an injection.

        Uses the FSM rather than the executor flag so it also covers
        ``PENDING_RETRY`` / ``AWAITING_SUB_AGENTS`` — the same predicate
        ``on_user_message`` uses to inject instead of starting a turn.
        """
        return self.turn_accepts_injection()

    @property
    def is_turn_lifecycle_active(self) -> bool:
        """True through execution, session persistence, and after-turn hooks.

        Unlike :attr:`is_turn_active`, this follows the owned run task rather
        than the FSM.  Finalization intentionally transitions the FSM to idle
        before it saves the session and drains hooks.
        """
        return self.execution_busy()

    @property
    def can_retry(self) -> bool:
        """Whether an immediate retry may start now.

        Read-only: a frontend asks this before offering retry, and the same
        predicate admits the retry when the request actually arrives. Nothing
        moves the FSM here — see :meth:`RetryCoordinator.can_retry`.
        """
        return self._turns.can_retry

    @property
    def turn_lifecycle_task(self) -> asyncio.Task[None] | None:
        """Return the currently owned execution/finalization task.

        Callers that need a boundary for one captured turn should retain this
        exact task instead of later awaiting the replaceable run-task chain.
        """
        return self._turns.turn_lifecycle_task

    def was_turn_lifecycle_saved(self, task: asyncio.Task[None]) -> bool:
        """Return whether *task* completed its final session save successfully."""
        return self._turns.was_turn_lifecycle_saved(task)

    @property
    def session_id(self) -> str | None:
        """Current canonical session id, or ``None`` before session start."""
        return self._session.session_id

    @property
    def runtime_details(self) -> AgentRuntimeDetails:
        """Runtime details for the currently built agent."""
        return copy.deepcopy(self._current.manifest.runtime_details)

    # Public read accessors for engine collaborators (buddy, session-title
    # updater, other app-layer features) so they don't reach into privates.

    @property
    def event_bus(self) -> EventBus:
        """Event bus the engine publishes on (constructor-injected)."""
        return self._bus

    @property
    def state(self) -> EngineState:
        """Current lifecycle state of the engine state machine.

        Read-only view; transitions stay engine-internal.
        """
        return self._fsm.state

    @property
    def workspace(self) -> Workspace | None:
        """Active workspace (primary cwd + working dirs), or ``None`` before start.

        Read-only view: rebinding stays with the session-lifecycle host
        contract.
        """
        return self._session.workspace

    @property
    def settings(self) -> Settings:
        """Live settings object the engine was built with."""
        return self._settings_handle.settings

    @property
    def loaded_settings(self) -> LoadedSettings:
        """Live settings together with where each value came from.

        Anything that answers "why does this session use this value" — the
        panel, ``settings/options``, the explanation for a key that was sealed
        at its default — needs the provenance that used to be discarded the
        moment a root unpacked ``.settings``.
        """
        return self._settings_handle.loaded

    @property
    def settings_handle(self) -> SettingsHandle:
        """The cell a frontend shares to stay on the same settings as this engine."""
        return self._settings_handle

    @property
    def agent_profile(self) -> AgentProfile | None:
        """Agent profile of the currently built runtime, or ``None`` before build."""
        return self._session.agent_profile

    @property
    def active_model_profile(self) -> ModelProfile | None:
        """Model profile of the currently built runtime, or ``None`` before build."""
        return copy.deepcopy(self._current.manifest.active_profile)

    @property
    def model_registry(self) -> ModelProfileRegistry | None:
        """Model profile registry the engine resolves profiles from, if any."""
        return self._model_registry

    @property
    def session_dir(self) -> Path | None:
        """Session directory path, or ``None`` if no session is active."""
        return self._session.session_dir

    def trajectory_context(self) -> TrajectoryContext | None:
        """The session's trajectory recording scope (current turn), or ``None`` when unrecorded.

        Side calls made outside a model run (title generation) bind this with
        :func:`chrys.foundation.trajectory.context.side_call_scope` so their
        exchanges are attributed to the session.
        """
        return self._trajectory_recorder.context()

    def turn_records(self) -> list[TurnRecord]:
        """The turns this session has run, in order, or an empty list before an agent exists.

        Read from the live provider state, which is what the next save writes:
        a frontend asking about the turn it just saw answered gets it here
        before anything has been persisted.
        """
        loaded = self._current.loaded
        if loaded is None:
            return []
        return turn_records(loaded.bindings.backend.history_state)

    @property
    def history_messages(self) -> list:
        """Live message list of the bound session history.

        Raises if no history is bound yet (before session start) — callers
        that can run that early should guard accordingly.
        """
        return self._history.messages

    def tool_kind_for_name(self, name: str) -> str:
        """Return the live tool kind for *name*, or an empty string when unknown."""
        kind = self._current.manifest.tool_kinds.get(name, "")
        if kind:
            return kind
        if (
            self._current.loaded is not None and self._current.loaded.sub_agent_tools is not None
        ) and name in self._current.loaded.sub_agent_tools.tool_names():
            return KIND_SUB_AGENT
        return ""

    def current_profile_snapshot(self) -> ProfileSwitched:
        """Build a no-op ``ProfileSwitched`` reflecting the live runtime.

        A frontend reselecting the already-active agent performs no backend
        switch and emits no event, so callers that still owe the client the
        standard runtime envelope read it here instead of fabricating blank
        fields. ``from``/``to`` are identical because nothing changed. Field
        sourcing mirrors the real switch event so the two cannot drift.
        """
        return self._controls.current_profile_snapshot()

    @property
    def approval_mode(self) -> ApprovalMode:
        """Live approval mode (updated by ``SetApprovalMode``)."""
        return self._session.approval_mode

    @property
    def recovered_from_sidecar(self) -> bool:
        """Whether the current session restore selected the crash-recovery sidecar."""
        return self._session.recovered_from_sidecar

    async def wait_for_run_task(self) -> None:
        """Wait for the active run task to finish post-run cleanup and saving."""
        await self._turns.wait_for_run_task()

    async def begin_rollback_projection(
        self,
        *,
        session_id: str | None,
        session_generation: int,
    ) -> str | None:
        return await self._rollback.begin_rollback_projection(
            session_id=session_id, session_generation=session_generation
        )

    def finish_rollback_projection(self, owner: str) -> None:
        self._rollback.finish_rollback_projection(owner)

    async def start(
        self,
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        await self._subscribe_event_handlers()
        await self._lifecycle.start(profile, operation=operation, staged_loaded=staged_loaded, workspace=workspace)

    async def prepare(self, fallback_profile: AgentProfile | None = None) -> None:
        """Subscribe handlers and optionally seed the profile used by legacy restores."""
        await self._subscribe_event_handlers()
        self._lifecycle.prepare(fallback_profile)

    async def _subscribe_event_handlers(self) -> None:
        """Subscribe frontend event handlers once, even if the first build fails.

        Handlers are intentionally available before the initial build completes
        so settings/profile changes can recover from startup failure.  The TUI
        blocks user actions while agent loading is active; if an external caller
        publishes recovery events during a still-running first build, those
        events may re-enter ``start()`` before ``_turn_bindings`` exists.
        """
        if self._subscribed:
            return
        self._subscribed = True
        await self._bus.subscribe(UserMessage, self._on_user_message)
        await self._bus.subscribe(UserInterrupt, self._on_user_interrupt)
        await self._bus.subscribe(UserRetry, self._on_user_retry)
        await self._bus.subscribe(UserInject, self._on_user_inject)
        await self._bus.subscribe(UserInjectCancel, self._on_user_inject_cancel)
        await self._bus.subscribe(UserRollback, self._on_user_rollback)
        await self._bus.subscribe(AgentProfileSwitch, self._on_profile_switch)
        await self._bus.subscribe(SessionNew, self._on_new_session)
        await self._bus.subscribe(SessionRestore, self.on_session_restore)
        await self._bus.subscribe(SessionDelete, self._on_session_delete)
        await self._bus.subscribe(SessionClear, self._on_session_clear)
        await self._bus.subscribe(SessionFork, self._on_session_fork)
        await self._bus.subscribe(WorkspaceChange, self._on_workspace_change)
        await self._bus.subscribe(SettingsReload, self._on_settings_reload)
        await self._bus.subscribe(SetApprovalMode, self._on_set_approval_mode)
        await self._bus.subscribe(SetModelProfile, self._on_set_model_profile)
        await self._bus.subscribe(SessionOnlySwitch, self._on_session_only_switch)
        await self._bus.subscribe(BackgroundTaskStop, self._on_background_task_stop)
        await self._bus.subscribe(InvocationRetryRequested, self._on_sub_agent_retry)
        await self._bus.subscribe(InvocationAbortRequested, self._on_sub_agent_abort)
        await self._bus.subscribe(InvocationPaused, self._on_sub_agent_paused)
        await self._bus.subscribe(InvocationResumed, self._on_sub_agent_unpaused)
        await self._bus.subscribe(InvocationAborted, self._on_sub_agent_unpaused)
        await self._bus.subscribe(InvocationCascadeAborted, self._on_sub_agent_unpaused)
        await self._workflows.subscribe()
        await self._side_questions.subscribe()
        if self._hook_confirmations is not None:
            await self._hook_confirmations.start()

    async def shutdown(self) -> None:
        # Closing is declared before the active workflow run is drained: a requester queued behind that
        # run is refused as shutting down instead of being admitted into a session that is going away.
        self._session.mark_closing()
        # An input still being admitted ends now, and whatever it waits on with it (a hook's
        # confirmation, a prompt shortcut's commands), whoever is driving the admission.
        self._turns.turn_state.input_operations.invalidate_all("shutdown")
        try:
            await self._workflows.shutdown()
        except BaseException:
            # The release, which closes the hook confirmations, never started: they close all the same.
            if self._release_task is None and self._hook_confirmations is not None:
                await self._hook_confirmations.close()
            raise
        await self._release()

    async def _release(self) -> None:
        """Release the session's resources and lock on a task of the engine's own.

        A shutdown awaited from inside a workflow run's agent pass (a subscriber to one of its tool events)
        returns from the drain at once, and the cancel it queued then cancels the subscriber's task under
        it: the release runs on its own task so the session is released either way. A shutdown awaited
        while a release is in flight waits for that one.
        """
        task = self._release_task
        if task is None or task.done():
            task = asyncio.create_task(self._release_resources(), name="chrys.engine.release")
            self._release_task = task
        # Shutdown drains usage publications. An inline UsageUpdate handler must return before that
        # drain can finish; the release task retains ownership after the handler gives up its wait.
        if asyncio.current_task() in self._usage_publisher.tasks:
            return
        await asyncio.shield(task)

    async def _release_resources(self) -> None:
        try:
            try:
                await self._side_questions.close()
            finally:
                await self._lifecycle.shutdown()
        finally:
            if self._hook_confirmations is not None:
                # After the release: what still waits on a question ends unavailable, and no answer is taken after.
                await self._hook_confirmations.close()

    async def _session_operation(
        self, operation: str, run: Callable[[], Awaitable[None]], *, target_session_id: str | None = None
    ) -> None:
        """Session-family operations and workflow runs exclude each other, in both directions.

        The operation is refused, not queued, while a run owns the lease; and while it is in flight (its
        reads and fences included) a run request is refused, so no run is admitted into a session that is
        being restored, replaced, forked, deleted or rolled back under it.

        The refusal is addressed to *target_session_id* when the operation names another session (a
        restore): the requester waiting for that session's answer hears it, where a refusal addressed to
        the active session would never reach it.
        """
        lease = self._turns.turn_state.lease
        if lease.workflow is not None:
            await self._bus.publish(
                Error(
                    code="workflow_active",
                    message=f"Cannot {operation} while a workflow run is active. Cancel the workflow run first.",
                    session_id=target_session_id or self._session.session_id,
                )
            )
            return
        with lease.session_operation():
            await run()

    # --- Event handlers ---

    @property
    def mutation_tracker(self) -> MutationTracker | None:
        """Live mutation tracker, or ``None`` if no session is active.

        Exposed read-only so the TUI (e.g. the rollback modal) can build
        per-turn diff views directly from in-memory state without a JSON
        round-trip through ``session.json``.  This matters during
        interrupted / failed runs where the tracker has up-to-date turn
        info that hasn't been persisted yet.
        """
        return self._session.mutation_tracker

    @property
    def mutation_coordinator(self) -> MutationCoordinator | None:
        """Cross-session mutation coordinator, or ``None`` when disabled."""
        return self._session.mutation_coordinator

    @property
    def todo_tracker(self) -> TodoTracker | None:
        """Live session todo tracker, or ``None`` if no session is active.

        Exposed read-only so hosts (e.g. the ACP plan-update sender) can read
        the current list via the sync ``snapshot()`` without touching the
        private attribute.
        """
        return self._session.todo_tracker

    async def refresh_mutation_attribution(self, *, force: bool = False) -> bool:
        return await self._lifecycle.refresh_mutation_attribution(force=force)

    # --- Rollback snapshots -------------------------------------------------
    #
    # Before each new user turn begins, the engine copies the current
    # ``session.json`` to ``{session_dir}/snapshots/turn_{N}.json`` where
    # ``N`` is the turn about to start.  Rolling back to keep turn K means
    # swapping the snapshot whose payload has ``turn_counter == K`` back in
    # as ``session.json`` and reloading.  ``K == 0`` is the pre-session
    # welcome state, so it deletes the session file instead of swapping.

    def turn_prompt_previews(self) -> dict[int, str]:
        """Return ``{turn_number: first_user_prompt}`` for every known turn."""
        return self._rollback.turn_prompt_previews()

    def first_rolled_back_user_text(self, target_turn: int) -> str:
        """Return the first user prompt discarded by rollback to ``target_turn``."""
        return self._rollback.first_rolled_back_user_text(target_turn)

    def available_rollback_turns(self) -> list[int]:
        """Public accessor for :meth:`_available_rollback_turns`.

        Used by the TUI to decide whether ``/rollback`` should show the
        modal at all, and to populate the turn picker.
        """
        return self._rollback.available_turns()

    @property
    def current_turn_number(self) -> int:
        """Current engine turn counter.

        Tracks the most-recently-started turn (incremented by the turn
        runner at the start of each run, restored from ``turn_counter``
        on session reload).  Equal to the last
        completed turn index while the engine is idle between turns.

        Exposed so the TUI can label the "you are here" entry in the
        rollback picker independently of which snapshots happen to
        exist on disk — otherwise a session with all snapshots
        deleted would show ``Turn 1 (Current)`` instead of the real
        current-turn index.
        """
        return self._session.turn_number

    async def _on_user_rollback(self, event: UserRollback) -> None:
        """Handle a rollback request."""
        await self._session_operation(
            "roll back",
            lambda: self._rollback.on_user_rollback(
                event,
                atomic_copy_file=atomic_copy_file,
                lock_timeout_seconds=SESSION_WRITE_LOCK_TIMEOUT_SECONDS,
            ),
        )

    async def reset_after_failed_startup_restore(self) -> None:
        """Return a partial startup restore to a clean, unlocked baseline."""
        await self._lifecycle.reset_after_failed_startup_restore()

    async def _on_user_message(self, event: UserMessage) -> None:
        """Handle a user message by running the executor as an async task."""
        await self._turns.on_user_message(event)

    async def _on_user_interrupt(self, _event: UserInterrupt) -> None:
        """Handle user interrupt.

        Cascades to every live sub-agent controller first so paused
        sub-agents resolve via the cascade-abort branch (otherwise their
        ``pending_decision`` future would keep their ``_invoke``
        coroutine pinned forever and the subsequent task cancel below
        would leak).  Then sets the interrupt flag / cancels the parent
        task. It then binds cancellation to the exact pre-executor run task
        or interrupts the active executor; ``run_and_save`` detects
        ``was_interrupted`` and rolls back history.
        """
        await self._turns.on_user_interrupt(_event)

    async def _on_sub_agent_retry(self, event: InvocationRetryRequested) -> None:
        """Route a user's per-card Retry click to the owning controller."""
        await self._turns.on_sub_agent_retry(event)

    async def _on_sub_agent_abort(self, event: InvocationAbortRequested) -> None:
        """Route a user's per-card Abort click to the owning controller."""
        await self._turns.on_sub_agent_abort(event)

    async def _on_sub_agent_paused(self, event: InvocationPaused) -> None:
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
        await self._turns.on_sub_agent_paused(event)

    async def _on_sub_agent_unpaused(
        self,
        event: InvocationResumed | InvocationAborted | InvocationCascadeAborted,
    ) -> None:
        """Common handler — retry/abort/cascade all remove the invocation from the paused set.

        FSM transitions only on the N→0 edge (last paused sub-agent
        resolved).  Marker is updated or stripped accordingly.
        """
        await self._turns.on_sub_agent_unpaused(event)

    async def _on_user_retry(self, event: UserRetry) -> None:
        """Handle retry — resume from current state.

        When ``event.text`` is non-empty, it is forwarded to
        ``retry_and_save`` (or stashed for the pending-retry path) and
        becomes the mid-turn continuation prompt, replacing the
        executor's default ``"continue"`` placeholder.
        """
        await self._turns.on_user_retry(event)

    async def _on_user_inject(self, event: UserInject) -> None:
        """Handle user injection (prompt inserted before next model call)."""
        await self._turns.on_user_inject(event)

    async def _on_user_inject_cancel(self, event: UserInjectCancel) -> None:
        """Handle user cancellation of a still-pending mid-run injection."""
        await self._turns.on_user_inject_cancel(event)

    async def _on_background_task_stop(self, event: BackgroundTaskStop) -> None:
        """Start stopping the background command the user picked (task ids are random, never reused)."""
        tasks = self._session.background_tasks
        if tasks is None:
            return
        for task in tasks.running:
            if task.id == event.task_id:
                tasks.stop(task, "user")

    async def _on_set_approval_mode(self, event: SetApprovalMode) -> None:
        """Update the active approval mode on the running middleware."""
        if event.session_id is not None and event.session_id != self._session.session_id:
            return
        await self._controls.on_set_approval_mode(
            event,
            persist_approval_mode_fn=persist_approval_mode,
        )

    async def _on_set_model_profile(self, event: SetModelProfile) -> None:
        """Switch the active model profile for this session only."""
        await self._controls.on_set_model_profile(event)

    async def _on_profile_switch(self, event: AgentProfileSwitch) -> None:
        """Handle agent profile switch — preserves conversation history."""
        await self._controls.on_profile_switch(event)

    async def _on_session_only_switch(self, event: SessionOnlySwitch) -> None:
        """Switch this session's own agent and/or model in one rebuild (a prompt shortcut's)."""
        await self._controls.on_session_only_switch(event)

    def pin_ask_user_timeout(self) -> None:
        """Mark ``ask_user_timeout_seconds`` as caller-owned across reloads.

        Callers that inject the timeout out-of-band (ACP sets it to ``None`` at
        launch via ``dataclasses.replace``) call this so a later ``SettingsReload``
        keeps the live value instead of reverting to the env default.
        """
        self._session.ask_user_timeout_pinned = True

    def pin_model_profile(self) -> None:
        """Mark the model selection as caller-owned across reloads.

        Headless ``--model`` lives only in this host's settings — it never
        parks the choice in the process pointer the way the TUI does — so
        without the pin one ``SettingsReload`` would silently revert the run
        to the global default.
        """
        self._session.model_profile_pinned = True

    async def _on_settings_reload(self, _event: SettingsReload) -> None:
        """Handle settings reload — recreate Settings from env and rebuild agent."""
        await self._controls.on_settings_reload(_event)

    async def _on_workspace_change(self, event: WorkspaceChange) -> None:
        """Handle workspace/cwd change — rebuild agent with new workspace."""
        await self._controls.on_workspace_change(event)

    async def _on_new_session(self, _event: SessionNew) -> None:
        """Handle new session request — save current, then start fresh."""
        await self._session_operation("start a new session", lambda: self._lifecycle.on_new_session(_event))

    async def on_session_restore(self, event: SessionRestore) -> None:
        """Handle session restore — load saved session state."""
        await self._session_operation(
            "restore a session",
            lambda: self._lifecycle.on_session_restore(event),
            target_session_id=event.session_id,
        )

    async def _on_session_delete(self, event: SessionDelete) -> None:
        """Handle session deletion; the active session is the one whose lock we hold, by its full or short id."""
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        if not self._session.guard.owns(event.session_id):
            await self._lifecycle.on_session_delete(event)
            return
        await self._session_operation("delete the active session", lambda: self._lifecycle.on_session_delete(event))

    async def _on_session_clear(self, event: SessionClear) -> None:
        """Handle clear: delete the active session and start fresh as one fenced transition."""
        await self._session_operation("clear the session", lambda: self._lifecycle.on_session_clear(event))

    async def _on_session_fork(self, event: SessionFork) -> None:
        await self._session_operation("fork the session", lambda: self._lifecycle.on_session_fork(event))
