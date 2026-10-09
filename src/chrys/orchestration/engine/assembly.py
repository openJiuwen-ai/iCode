# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose the session, build, turn, and control owners for an agent engine."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine import trajectory as trajectory_recorder
from chrys.orchestration.engine.build.builder import build_agent
from chrys.orchestration.engine.engine import AgentEngine, _set_current_engine, _unset_current_engine
from chrys.orchestration.engine.execution import ExecutionLease
from chrys.orchestration.engine.loader import AgentLoader
from chrys.orchestration.engine.rollback import RollbackController
from chrys.orchestration.engine.run.coordinator import TurnCoordinator
from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
from chrys.orchestration.engine.session_lifecycle import SessionLifecycle
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.controls import RuntimeControls
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
from chrys.orchestration.engine.state.machine import EngineStateMachine
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.orchestration.engine.usage import UsagePublisher
from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.orchestration.workflows.coordinator import WorkflowCoordinator
from chrys.service.approval.policy import ApprovalMode
from chrys.service.approval.turn_context import TurnContextHolder
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.session.history import SessionHistoryManager
from chrys.service.session.persistence import SessionPersistence

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import MCPServerConfig
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.state.store import StateStore


def _noop_successful_turn() -> None:
    """Default successful-turn callback."""


def _noop_turn_started() -> None:
    """Default turn-started callback."""


def assemble_agent_engine(
    event_bus: EventBus,
    settings: Settings | None = None,
    loaded_settings: LoadedSettings | None = None,
    model_registry: ModelProfileRegistry | None = None,
    agent_registry: AgentProfileRegistry | None = None,
    state_store: StateStore | None = None,
    initial_approval_mode: ApprovalMode | None = None,
    mcp_overlay: list[MCPServerConfig] | None = None,
    initial_workspace: Workspace | None = None,
    on_successful_turn: Callable[[], None] | None = None,
    on_turn_started: Callable[[], None] | None = None,
    allow_user_interaction: bool = True,
    surface: SessionSurface | None = None,
) -> AgentEngine:
    """Build the owners and inject the engine's event-routing facade.

    *surface* is the frontend this engine serves; turns and workflow runs
    record it on their session. Production entry points always pass it.
    """
    bus = event_bus
    # AIxCoding telemetry: tool-detail/ai-code reporting on this bus (idempotent per bus).
    from chrys.aixcoding.telemetry import subscriber

    subscriber.attach(bus)
    if loaded_settings is not None and settings is not None and settings is not loaded_settings.settings:
        error_message = "Pass either settings or loaded_settings, not two different ones."
        raise ValueError(error_message)
    settings_handle = SettingsHandle(
        loaded_settings or LoadedSettings(settings=settings or Settings.from_env(), provenance={})
    )
    persistence = SessionPersistence(state_store, event_bus)
    session = ActiveSession(
        persistence=persistence,
        workspace=initial_workspace,
        approval_mode=initial_approval_mode or ApprovalMode(settings_handle.settings.default_approval_mode),
        surface=surface,
    )
    current = CurrentAgent()
    on_successful_turn: Callable[[], None] = (
        on_successful_turn if on_successful_turn is not None else _noop_successful_turn
    )
    on_turn_started: Callable[[], None] = on_turn_started if on_turn_started is not None else _noop_turn_started
    turn_state = TurnRuntimeState(lease=ExecutionLease(bus=bus))
    permits = LifecyclePermits(turn_state=turn_state, session=session)
    recorder = trajectory_recorder.TrajectoryRecorder()
    fsm = EngineStateMachine()
    history = SessionHistoryManager()
    workspace_change_tracker = WorkspaceChangeTracker()
    usage_publisher = UsagePublisher(bus=bus, session=session, current=current)
    writer = SessionWriter(
        persistence=persistence,
        session=session,
        current=current,
        turn_state=turn_state,
        workspace_change_tracker=workspace_change_tracker,
    )
    hooks = TurnHookDispatcher(session=session, current=current)

    def register_current_engine() -> None:
        _set_current_engine(engine)

    def unregister_current_engine() -> None:
        _unset_current_engine(engine)

    mcp_cache = MCPConnectionCache()
    loader = AgentLoader(
        bus=bus,
        persistence=persistence,
        agent_registry=agent_registry,
        model_registry=model_registry,
        settings_handle=settings_handle,
        session=session,
        current=current,
        permits=permits,
        checkpoints=writer,
        usage_publisher=usage_publisher,
        hooks=hooks,
        history=history,
        turn_state=turn_state,
        workspace_change_tracker=workspace_change_tracker,
        trajectory_recorder=recorder,
        fsm=fsm,
        mcp_cache=mcp_cache,
        turn_context=TurnContextHolder(),
        mcp_overlay=mcp_overlay,
        allow_user_interaction=allow_user_interaction,
        build_agent_fn=build_agent,
        register_current_engine=register_current_engine,
    )

    lifecycle = SessionLifecycle(
        session=session,
        current=current,
        loader=loader,
        permits=permits,
        writer=writer,
        turn_state=turn_state,
        usage_publisher=usage_publisher,
        bus=bus,
        fsm=fsm,
        history=history,
        persistence=persistence,
        settings_handle=settings_handle,
        agent_registry=agent_registry,
        model_registry=model_registry,
        trajectory_recorder=recorder,
        workspace_change_tracker=workspace_change_tracker,
        unregister_current_engine=unregister_current_engine,
    )
    rollback = RollbackController(
        session=session,
        current=current,
        permits=permits,
        writer=writer,
        turn_state=turn_state,
        lifecycle=lifecycle,
        bus=bus,
        history=history,
        fsm=fsm,
        workspace_change_tracker=workspace_change_tracker,
        trajectory_recorder=recorder,
        settings_handle=settings_handle,
    )
    turns = TurnCoordinator(
        turn_state=turn_state,
        current=current,
        session=session,
        permits=permits,
        writer=writer,
        loader=loader,
        hooks=hooks,
        bus=bus,
        fsm=fsm,
        history=history,
        trajectory_recorder=recorder,
        workspace_change_tracker=workspace_change_tracker,
        settings_handle=settings_handle,
        persistence=persistence,
        on_successful_turn=on_successful_turn,
        on_turn_started=on_turn_started,
    )
    workflows = WorkflowCoordinator(
        mcp_cache=mcp_cache,
        bus=bus,
        session=session,
        turn_state=turn_state,
        settings_handle=settings_handle,
        agent_registry=agent_registry,
        model_registry=model_registry,
        allow_user_interaction=allow_user_interaction,
        persistence=persistence,
        build_hooks=SessionHookFactory(bus),
    )
    controls = RuntimeControls(
        session=session,
        current=current,
        permits=permits,
        lifecycle=lifecycle,
        bus=bus,
        settings_handle=settings_handle,
        agent_registry=agent_registry,
        workspace_change_tracker=workspace_change_tracker,
        workflows=workflows,
    )
    engine = AgentEngine(
        bus=bus,
        session=session,
        permits=permits,
        current=current,
        writer=writer,
        usage_publisher=usage_publisher,
        loader=loader,
        lifecycle=lifecycle,
        rollback=rollback,
        turns=turns,
        controls=controls,
        settings_handle=settings_handle,
        model_registry=model_registry,
        fsm=fsm,
        history=history,
        trajectory_recorder=recorder,
        workflows=workflows,
    )
    return engine
