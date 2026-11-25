# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small, independently owned engine components for focused tests."""

from __future__ import annotations

from types import SimpleNamespace

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.build.loaded import AgentManifest, LoadedAgent
from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.orchestration.engine.workspaces import SessionWorkspaces, WorkspacePlan
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.session.persistence import SessionPersistence
from chrys.service.workspaces.registry import RegistrySnapshot
from tests.support.loaded_agents import install_loaded_agent


def plan_workspace(workspace: Workspace) -> WorkspacePlan:
    return SessionWorkspaces.plan(workspace=workspace, snapshot=RegistrySnapshot())


def make_session(**overrides) -> ActiveSession:
    """Construct a session, overriding only its existing public state."""
    persistence = overrides.pop("persistence", None)
    workspace = overrides.pop("workspace", None)
    session = ActiveSession(
        persistence=persistence if persistence is not None else SessionPersistence(None, EventBus()),
        workspace_plan=plan_workspace(workspace) if workspace is not None else None,
        approval_mode=overrides.pop("approval_mode", None),
    )
    for name, value in overrides.items():
        if name.startswith("_") or name not in vars(session):
            raise TypeError(f"Unknown session field: {name}")
        setattr(session, name, value)
    return session


def make_current(*, loaded: LoadedAgent | None = None, manifest: AgentManifest | None = None) -> CurrentAgent:
    """Keep build installation in the shared installer, including narrow test records."""
    current = CurrentAgent()
    install_loaded_agent(SimpleNamespace(current=current), loaded=loaded)
    if manifest is not None:
        install_loaded_agent(SimpleNamespace(current=current), manifest=manifest)
    return current


def make_permits(*, session: ActiveSession, turn_state: TurnRuntimeState) -> LifecyclePermits:
    """Connect admission and session identity without constructing an engine."""
    return LifecyclePermits(session=session, turn_state=turn_state)


def make_writer(
    *,
    persistence: SessionPersistence,
    session: ActiveSession,
    current: CurrentAgent,
    turn_state: TurnRuntimeState,
    workspace_change_tracker: WorkspaceChangeTracker,
) -> SessionWriter:
    """Construct a writer with resources retained by its test."""
    return SessionWriter(
        persistence=persistence,
        session=session,
        current=current,
        turn_state=turn_state,
        workspace_change_tracker=workspace_change_tracker,
    )


def make_turn_state() -> TurnRuntimeState:
    """Create an independent execution lease and its turn state."""
    return TurnRuntimeState()


def make_hooks(*, session: ActiveSession, current: CurrentAgent) -> TurnHookDispatcher:
    """Share only the session and build queried by hook dispatch."""
    return TurnHookDispatcher(session=session, current=current)
