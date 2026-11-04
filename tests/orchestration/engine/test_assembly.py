# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Composition constructs stable owners and wires the complete session lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionReady
from chrys.foundation.trajectory.event_types import EventType
from chrys.orchestration.engine import assembly
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.build import builder
from chrys.orchestration.engine.build.loaded import LoadedAgent
from chrys.orchestration.engine.engine import get_current_engine
from chrys.orchestration.engine.loader import AgentLoader
from chrys.orchestration.engine.rollback import RollbackController
from chrys.orchestration.engine.run.coordinator import TurnCoordinator
from chrys.orchestration.engine.session_lifecycle import SessionLifecycle
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.controls import RuntimeControls
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.orchestration.engine.usage import UsagePublisher
from chrys.service.agent_middleware.system_reminder import CurrentRunReminderScope
from chrys.service.llm.mock import MockChatClient
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationScope, PreparationTrace
from tests.orchestration.engine.test_agent_loader import _profile
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.loaded_agents import install_loaded_agent
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.reminder_stack import reminder_pair


def _components(engine):
    return (
        engine.session,
        engine.permits,
        engine.current,
        engine.writer,
        engine.usage_publisher,
        engine.loader,
        engine.turns,
        engine.rollback,
        engine.controls,
        engine.lifecycle,
    )


async def test_assembly_prepares_starts_and_shuts_down_real_component_owners(
    agent_engine, monkeypatch, tmp_path
) -> None:
    bus = EventBus()
    settings, registry = make_mock_settings_and_registry()
    settings = replace(settings, workspace_change_notice=False)
    client = MockChatClient(responses=[])
    monkeypatch.setattr(builder, "create_client", create_autospec(builder.create_client, return_value=client))
    build_agent_fn = create_autospec(assembly.build_agent, side_effect=assembly.build_agent)
    monkeypatch.setattr(assembly, "build_agent", build_agent_fn)
    engine = agent_engine(bus, settings=settings, model_registry=registry, state_store=JsonFileStateStore(tmp_path))
    owners = _components(engine)
    expected_types = (
        ActiveSession,
        LifecyclePermits,
        CurrentAgent,
        SessionWriter,
        UsagePublisher,
        AgentLoader,
        TurnCoordinator,
        RollbackController,
        RuntimeControls,
        SessionLifecycle,
    )
    assert all(isinstance(owner, expected) for owner, expected in zip(owners, expected_types, strict=True))
    ready = []

    async def record(event):
        ready.append(event)

    await bus.subscribe(SessionReady, record)
    profile = _profile()
    await engine.prepare(profile)
    assert engine.agent_profile is profile
    assert engine.current.loaded is None
    build_agent_fn.assert_not_awaited()
    await engine.start(profile)
    build_agent_fn.assert_awaited_once()
    assert len(ready) == 1
    assert ready[0].session_id == engine.session_id
    assert get_current_engine(engine.session_id) is engine
    assert _components(engine) == owners
    loaded = engine.current.loaded
    assert loaded is not None
    assert loaded.prepared.closing is False
    session_id = engine.session_id
    await engine.shutdown()
    assert engine.current.loaded is None
    assert get_current_engine(session_id) is None
    assert loaded.prepared.closing is True
    assert loaded.conversation.closing is True
    assert _components(engine) == owners


@pytest.mark.asyncio
async def test_final_save_evidence_routes_through_owned_coordinator_task() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    owned_task = asyncio.current_task()
    assert owned_task is not None
    engine.turns.turn_state.lease.run_task = owned_task

    engine.turns.turn_state.lease.record_current_run_final_save()

    assert engine._turns.was_run_task_finally_saved(owned_task) is True
    assert engine.was_turn_lifecycle_saved(owned_task) is True


@pytest.mark.asyncio
async def test_agent_engine_owns_turn_coordinator_state() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())

    assert isinstance(engine._turns, TurnCoordinator)
    assert engine.turns is engine._turns

    async def _run() -> None:
        return None

    task = asyncio.create_task(_run())
    engine.turns.turn_state.lease.run_task = task
    try:
        assert engine._turns.run_task is task
        assert engine.turn_lifecycle_task is task
        assert engine.is_turn_lifecycle_active is True
    finally:
        await task
    assert engine.is_turn_lifecycle_active is False


async def test_engine_owner_clocks_and_transition_reset_expire_reminder_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    reminder, last_words = reminder_pair()
    scope_token = reminder.create_current_run_scope()
    install_loaded_agent(engine, reminder_middleware=reminder, last_words=last_words)
    loaded = engine.current.loaded
    assert loaded is not None
    calls: list[tuple[str, CurrentRunReminderScope]] = []
    original_close = LoadedAgent.aclose
    original_expire = reminder.expire_current_run_scope

    async def record_close(owner: LoadedAgent) -> None:
        await original_close(owner)
        if owner is loaded:
            calls.append(("closed", scope_token))

    def record_expire(scope: CurrentRunReminderScope) -> None:
        calls.append(("expire", scope))
        original_expire(scope)

    monkeypatch.setattr(LoadedAgent, "aclose", record_close)
    monkeypatch.setattr(reminder, "expire_current_run_scope", record_expire)
    engine.turns.turn_state.lease.begin_current_run_scope(
        owner_admission_id=1,
        session_generation=engine.session_generation,
        build_generation=engine.build_generation,
        reminder_scope=scope_token,
    )
    engine.turns.turn_state.set_current_input("do work", None, None)

    assert engine.session_generation == 0
    assert engine.build_generation == 0
    assert engine.load_generation == 0

    engine.permits.begin_agent_load()
    engine.permits.finish_agent_load()
    assert engine.load_generation == 1
    assert engine.build_generation == 0

    engine.permits.advance_build_generation()
    assert engine.build_generation == 1

    engine.permits.invalidate_for_session_transition_pre_shutdown()
    assert engine.session_generation == 1
    assert engine.turns.turn_state.current_input.text == "do work"

    await engine.shutdown()
    engine.lifecycle.reset_turn_runtime_after_session_shutdown()
    assert engine.turns.turn_state.current_input.text == ""
    assert engine.current.loaded is None
    assert calls.count(("closed", scope_token)) == 1
    closed_index = calls.index(("closed", scope_token))
    assert all(action != "expire" for action, _scope in calls[closed_index + 1 :]), calls


@pytest.mark.asyncio
async def test_start_runs_inside_session_transition_boundary(monkeypatch: pytest.MonkeyPatch, agent_engine) -> None:
    engine = agent_engine(EventBus(), settings=Settings())
    calls: list[str] = []

    async def fake_start_locked(
        _profile: object,
        *,
        operation: str = "startup",
        staged_loaded: object = None,
        session: object = None,
        workspace: object = None,
    ) -> None:
        calls.append(operation)

    monkeypatch.setattr(engine.lifecycle, "_start_locked", fake_start_locked)

    owner = await engine.permits.begin_session_transition("restore")
    try:
        await engine.start(object(), operation="restore")  # type: ignore[arg-type]
    finally:
        engine.permits.finish_session_transition(owner)

    assert calls == ["restore"]


@pytest.mark.asyncio
async def test_rollback_projection_fences_prompts_without_committing_session_transition() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.session_id = "session-a"
    generation = engine.session_generation

    owner = await engine.begin_rollback_projection(session_id=None, session_generation=generation)
    assert owner is not None
    try:
        assert engine.session_generation == generation
        assert engine.turns.turn_state.lease.prompt_admission_closed is True
        assert (
            engine.turns.turn_state.lease.reserve_prompt_admission(
                kind="fresh",
                session_generation=generation,
                build_generation=engine.build_generation,
            )
            is None
        )
    finally:
        engine.finish_rollback_projection(owner)

    assert engine.session_generation == generation
    assert engine.turns.turn_state.lease.prompt_admission_closed is False
    assert engine.permits.gate_lock.locked() is False


@pytest.mark.asyncio
async def test_session_transition_permit_keeps_admission_closed_until_finish() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    sink = FakeSink()
    trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="prompt_admission",
        context=make_context(sink).with_turn(None).with_run(None),
    )
    assert trace is not None
    await trace.started()
    admission = engine.turns.turn_state.lease.reserve_prompt_admission(
        kind="fresh",
        session_generation=engine.session_generation,
        build_generation=engine.build_generation,
        preparation_trace=trace,
    )
    assert admission is not None

    owner = await engine.permits.begin_session_transition("restore")
    waiter = asyncio.create_task(engine.turns.turn_state.lease.wait_for_prompt_admission_open())
    try:
        await asyncio.sleep(0)
        assert engine.session_generation == 1
        assert engine.turns.turn_state.lease.prompt_admission_closed is True
        assert engine.turns.turn_state.lease.active_admission_count() == 0
        assert waiter.done() is False
        assert sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == PreparationOutcome.OWNER_CHANGED
        sink.assert_operations_settled()

        engine.lifecycle.reset_turn_runtime_after_session_shutdown()
        await asyncio.sleep(0)
        assert engine.turns.turn_state.lease.prompt_admission_closed is True
        assert waiter.done() is False
    finally:
        engine.permits.finish_session_transition(owner)

    await asyncio.wait_for(waiter, timeout=5.0)
    assert engine.turns.turn_state.lease.prompt_admission_closed is False
