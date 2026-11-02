# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Profiles, registries, executor stand-ins, session seeds, and the start/shutdown stub shared by the engine lifecycle tests."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import Content, LoopRecorder, Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.service.approval.policy import ApprovalMode
from chrys.service.hooks.schema import HookDecision
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.persistence import SessionPersistence
from chrys.service.state.store import ChatSessionMeta, JsonFileStateStore
from chrys.service.todos.tracker import TodoTracker
from tests.support.components import make_current, make_session, make_turn_state, make_writer
from tests.support.loaded_agents import install_loaded_agent


def _profile(name: str = "Code", display_name: str = "") -> AgentProfile:
    return AgentProfile(
        name=name,
        display_name=display_name,
        instructions=f"{name} instructions.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )


def _registry(*profiles: AgentProfile) -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    for profile in profiles:
        registry.register(profile)
    return registry


def _model_registry(*profiles: ModelProfile) -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    for profile in profiles:
        registry.register(profile)
    return registry


def _session_meta(*, model_profile_id: str) -> ChatSessionMeta:
    now = datetime.now(UTC)
    return ChatSessionMeta(
        session_id="restore_me",
        agent_profile="Code",
        agent_display_name="Code",
        created_at=now,
        updated_at=now,
        message_count=1,
        model_profile_id=model_profile_id,
    )


class _ApprovalTarget:
    @property
    def approval(self):
        return self

    def __init__(self) -> None:
        self.modes: list[ApprovalMode] = []

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        self.modes.append(mode)


class _SessionEndProbeHookManager:
    def __init__(self, session_file: Path) -> None:
        self._session_file = session_file
        self.exists_during_fire: list[bool] = []
        self.payloads: list[dict[str, Any]] = []

    async def fire(self, _event: object, payload: dict[str, Any], **_kwargs: object) -> HookDecision:
        self.exists_during_fire.append(self._session_file.exists())
        self.payloads.append(payload)
        return HookDecision()

    async def drain_session(self, *, close: bool = True) -> None:
        return None


class _HistoryStateExecutor:
    """Minimal executor surface for session state save/restore plumbing."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.history_state: dict[str, Any] = state if state is not None else {}
        self.input_properties: dict[str, Any] | None = None

    @property
    def backend(self):
        return self

    @property
    def inputs(self):
        return self

    @property
    def state(self):
        return self

    @property
    def approval(self):
        return self

    @property
    def tool_events(self):
        return self


def _seed_checkpoint_engine(store: JsonFileStateStore, session_id: str) -> AgentEngine:
    """Build an engine wired enough to produce a real recovery checkpoint."""
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = session_id
    _seed_checkpoint_state(engine.current, engine.turns.turn_state)
    return engine


def _seed_checkpoint_state(current: CurrentAgent, turn_state: TurnRuntimeState) -> None:
    carrier = SimpleNamespace(current=current)
    user = Message("user", ["do work"])
    assistant = Message("assistant", [Content.from_function_call("c1", "read_file", arguments={})])
    tool = Message("tool", [Content.from_function_result("c1", result="done")])

    install_loaded_agent(
        carrier,
        bindings=_HistoryStateExecutor(  # type: ignore[assignment]
            {"messages": [user], "compressed_msgs": [], "turn_counter": 1}
        ),
    )
    capture = LoopRecorder()
    capture._initial_count = 1
    capture._captured = [user, assistant, tool]
    install_loaded_agent(carrier, loop_recorder=capture)
    turn_state.set_current_input("do work", None, None)


@dataclass
class CheckpointComponents:
    """The writer's actual dependencies, retained by a component test."""

    session: ActiveSession
    current: CurrentAgent
    turn_state: TurnRuntimeState
    persistence: SessionPersistence
    writer: SessionWriter


def make_checkpoint_components(store: JsonFileStateStore, session_id: str | None) -> CheckpointComponents:
    """Construct and optionally seed a writer without a coordinating engine."""
    persistence = SessionPersistence(store, EventBus())
    session = make_session(persistence=persistence, session_id=session_id)
    current = make_current()
    turn_state = make_turn_state()
    writer = make_writer(
        persistence=persistence,
        session=session,
        current=current,
        turn_state=turn_state,
        workspace_change_tracker=WorkspaceChangeTracker(),
    )
    if session_id is not None:
        _seed_checkpoint_state(current, turn_state)
    return CheckpointComponents(session, current, turn_state, persistence, writer)


_TODOS = [
    {"content": "read the plan", "status": "completed", "active_form": "Reading the plan"},
    {"content": "implement", "status": "in_progress", "active_form": "Implementing"},
]


async def _tracker_with_todos(todos: list[dict[str, str]]) -> TodoTracker:
    tracker = TodoTracker()
    await tracker.restore(todos)
    return tracker


async def _seed_restorable_session(store: JsonFileStateStore, *, text: str = "saved", **meta: Any) -> None:
    """Save a one-turn ``restore_me`` session; ``meta`` is forwarded to ``save_session``."""
    await store.save_session(
        "restore_me",
        {"messages": [Message("user", [text])], "compressed_msgs": [], "turn_counter": 1},
        **meta,
    )


async def _seed_recovery_sidecar(store: JsonFileStateStore, **meta: Any) -> None:
    """Write a turn-9 recovery sidecar for ``restore_me`` that outranks the primary save."""
    await asyncio.to_thread(
        store.save_recovery_session,
        "restore_me",
        {"messages": [Message("user", ["recovery"])], "compressed_msgs": [], "turn_counter": 9},
        **meta,
    )


class _ResetExecutor(_HistoryStateExecutor):
    """TurnBindings stand-in a welcome reset can interrupt and close."""

    running = False

    async def interrupt(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @property
    def backend(self):
        return self

    @property
    def inputs(self):
        return self

    @property
    def state(self):
        return self

    @property
    def approval(self):
        return self

    @property
    def tool_events(self):
        return self


@dataclass
class StubbedEngine:
    """What the ``start``/``shutdown`` stand-ins of one engine recorded.

    It holds no reference to the engine itself: the caller passed that in and
    still has it. ``shutdown_calls`` is here even though the restart scenarios
    assert only on ``start_calls`` — the shutdown half is stubbed to suppress a
    real teardown, and keeping its record means a scenario that does want to
    pin the teardown does not have to reach past this type to get it.
    """

    start_calls: list[tuple[AgentProfile, str]]
    shutdown_calls: list[tuple[bool, bool]]


def _record_start(
    monkeypatch: pytest.MonkeyPatch,
    engine: AgentEngine,
    expect_operation: str | None,
) -> list[tuple[AgentProfile, str]]:
    start_calls: list[tuple[AgentProfile, str]] = []

    async def fake_start(
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = workspace
        if expect_operation is not None:
            assert operation == expect_operation
        start_calls.append((profile, operation))
        if staged_loaded is not None:
            engine.settings_handle.install_prepared(engine.settings_handle.prepare(staged_loaded, session=session))

    monkeypatch.setattr(engine.lifecycle, "start", fake_start)
    return start_calls


def _record_shutdown(monkeypatch: pytest.MonkeyPatch, engine: AgentEngine) -> list[tuple[bool, bool]]:
    shutdown_calls: list[tuple[bool, bool]] = []

    async def fake_shutdown() -> None:
        shutdown_calls.append((True, True))

    async def fake_close_session() -> None:
        shutdown_calls.append((True, False))

    async def fake_close_session_in_place(*, keep_side_answers: bool = False) -> None:
        shutdown_calls.append((False, False))

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_close_session)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_close_session_in_place)
    return shutdown_calls


def stub_engine_start(
    monkeypatch: pytest.MonkeyPatch,
    engine: AgentEngine,
    *,
    expect_operation: str | None = None,
) -> list[tuple[AgentProfile, str]]:
    """Replace only ``engine.lifecycle.start`` with a recording stand-in; return what it records.

    The stand-in records ``(profile, operation)`` and installs a staged settings load
    the way a successful build's commit does; it builds nothing else, and must never
    be turned into a real start. ``expect_operation`` fails the call outright when the
    engine dispatches under another name.

    ``shutdown`` is deliberately left real: for these tests a teardown is not part of
    the scenario, so one happening anyway must still surface instead of being absorbed
    by a stand-in nobody asserts on.
    """
    return _record_start(monkeypatch, engine, expect_operation)


def stub_engine_shutdown(monkeypatch: pytest.MonkeyPatch, engine: AgentEngine) -> list[tuple[bool, bool]]:
    """Replace only ``engine.lifecycle.shutdown`` with a recording stand-in; return what it records.

    ``start`` is deliberately left real, so a path that erroneously restarts the engine
    still fails loudly rather than quietly appending to a list.
    """
    return _record_shutdown(monkeypatch, engine)


def stub_engine_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    engine: AgentEngine,
    *,
    expect_operation: str | None = None,
) -> StubbedEngine:
    """Replace both ``engine.lifecycle.start`` and ``engine.lifecycle.shutdown`` with recording stand-ins.

    Only for scenarios that drive a full restart, where leaving either half real would
    run a build or a teardown the test never asked for. Everything else takes the
    narrower ``stub_engine_start`` or ``stub_engine_shutdown``.
    """
    return StubbedEngine(
        start_calls=_record_start(monkeypatch, engine, expect_operation),
        shutdown_calls=_record_shutdown(monkeypatch, engine),
    )
