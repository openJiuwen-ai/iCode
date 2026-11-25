# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A deleted working directory refuses new turns and retries on a real engine, and recovers once it returns.

The component-level ordering (before the prompt hook, after the retry's wait
for the previous run) lives in ``test_lifecycle_hooks.py``; these drive the
assembled engine through its bus so the FSM, lease and model calls are the
real ones.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, InvocationMessage, UserMessage, UserRetry
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.execution import PendingRetry
from chrys.orchestration.engine.run.retry import RetryCoordinator
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine.run._engine_run_helpers import (
    _PROFILE,
    _filter,
    _final_agent_messages_after_run,
    _make_registry,
)
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain
from tests.support.workspace_engine import HeldFirstCallClient


@dataclass(frozen=True, slots=True)
class _Started:
    engine: AgentEngine
    bus: EventBus
    events: list[object]


async def _start_in(
    agent_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    client: MockChatClient,
    *,
    workspace_id: str | None = None,
) -> _Started:
    """Start an engine whose workspace is *work*; sessions live elsewhere so *work* can be deleted."""
    events: list[object] = []
    bus = EventBus()
    for cls in (Error, InvocationMessage):
        await bus.subscribe(cls, lambda event, _events=events: collect_events(_events, event))
    settings, model_registry = make_mock_settings_and_registry(stream=False)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        initial_workspace=Workspace.from_cwd(str(work)),
    )
    await engine.start(_PROFILE, workspace_id=workspace_id)
    return _Started(engine=engine, bus=bus, events=events)


def _final_texts(events: list[object]) -> list[str]:
    return [
        event.text for event in _filter(events, InvocationMessage) if event.origin.kind == "turn" and event.is_final
    ]


async def test_fresh_prompt_is_refused_while_working_dir_missing_and_runs_once_it_returns(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    client = MockChatClient(responses=[MockResponse(text="listed")])
    started = await _start_in(agent_engine, tmp_path, monkeypatch, work, client)
    engine = started.engine
    session_id = engine.session.session_id

    work.rmdir()
    await started.bus.publish(UserMessage(text="ls"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    errors = _filter(started.events, Error)
    assert [(error.code, error.session_id) for error in errors] == [("working_dir_missing", session_id)]
    assert errors[0].message == f"Working directory no longer exists: {work}"
    assert client.call_count == 0
    assert engine.state == EngineState.IDLE
    assert engine.turns.turn_state.lease.active_admission_count() == 0
    assert engine.turns.turn_state.lease.run_task is None

    work.mkdir()
    await started.bus.publish(UserMessage(text="ls"))
    finals = await _final_agent_messages_after_run(engine, started.events)

    assert [final.text for final in finals] == ["listed"]
    assert client.call_count == 1
    assert len(_filter(started.events, Error)) == 1
    assert engine.state == EngineState.IDLE


@pytest.mark.parametrize("retry", [False, True])
async def test_missing_unspecified_cwd_takes_precedence_over_workflow_ownership(
    tmp_path, agent_engine, monkeypatch, retry
):
    from chrys.service.trajectory.preparation import PreparationTrace

    work = tmp_path / "work"
    work.mkdir()
    client = MockChatClient(responses=[MockResponse(text="first")])
    started = await _start_in(agent_engine, tmp_path, monkeypatch, work, client)
    await started.bus.publish(UserMessage(text="first"))
    await _final_agent_messages_after_run(started.engine, started.events)
    work.rmdir()
    lease = started.engine.turns.turn_state.lease
    workflow = None
    start_preparation = PreparationTrace.started

    async def start_workflow_during_admission(trace):
        nonlocal workflow
        await start_preparation(trace)
        if workflow is None:
            workflow = lease.begin_workflow("workflow", "request")

    monkeypatch.setattr(PreparationTrace, "started", start_workflow_during_admission)
    try:
        await started.bus.publish(UserRetry() if retry else UserMessage(text="second"), raise_handler_errors=True)
    finally:
        if workflow is not None:
            lease.end_workflow(workflow)

    assert workflow is not None
    assert [error.code for error in _filter(started.events, Error)] == ["working_dir_missing"]


async def test_user_retry_is_refused_while_working_dir_missing(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    client = MockChatClient(responses=[MockResponse(text="first"), MockResponse(text="unused")])
    started = await _start_in(agent_engine, tmp_path, monkeypatch, work, client)
    engine = started.engine
    await started.bus.publish(UserMessage(text="hello"))
    await _final_agent_messages_after_run(engine, started.events)
    history_before = list(engine.current.require_loaded().bindings.backend.history_state.get("messages", []))

    work.rmdir()
    await started.bus.publish(UserRetry())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    errors = _filter(started.events, Error)
    assert [(error.code, error.session_id) for error in errors] == [("working_dir_missing", engine.session.session_id)]
    assert client.call_count == 1
    assert engine.state == EngineState.IDLE
    assert engine.turns.turn_state.lease.active_admission_count() == 0
    history_after = engine.current.require_loaded().bindings.backend.history_state.get("messages", [])
    assert [message.role for message in history_after] == [message.role for message in history_before]


@pytest.mark.parametrize(
    ("interrupt", "settled"),
    [(False, EngineState.IDLE), (True, EngineState.INTERRUPTED)],
    ids=["completed-pass", "interrupted-pass"],
)
async def test_queued_retry_is_dropped_when_working_dir_disappears_and_the_engine_settles(
    tmp_path: Path,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: bool,
    settled: EngineState,
) -> None:
    """A retry queued behind a running pass never starts once the cwd is gone, and the FSM leaves RUNNING.

    The frontend already showed the accepted retry as running, so the drop is
    reported with one error that ends it.
    """
    dispatch_results: list[str | None] = []
    real_dispatch = RetryCoordinator.start_pending_retry_if_due

    def recording_dispatch(self: RetryCoordinator) -> str | None:
        result = real_dispatch(self)
        dispatch_results.append(result)
        return result

    monkeypatch.setattr(RetryCoordinator, "start_pending_retry_if_due", recording_dispatch)
    work = tmp_path / "work"
    work.mkdir()
    client = HeldFirstCallClient([MockResponse(text="first pass"), MockResponse(text="fresh after recreate")])
    started = await _start_in(agent_engine, tmp_path, monkeypatch, work, client)
    engine = started.engine
    lease = engine.turns.turn_state.lease

    try:
        await started.bus.publish(UserMessage(text="start"))
        await asyncio.wait_for(client.first_call_entered.wait(), timeout=ENGINE_TURN_TIMEOUT)
        if interrupt:
            engine.current.require_loaded().bindings._interrupt.set_interrupted()
        await started.bus.publish(UserRetry())
        assert engine.state == EngineState.PENDING_RETRY
        assert lease.pending_retry.owner_admission_id is not None

        work.rmdir()
    finally:
        client.release_first_call.set()
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert dispatch_results == [str(work)]
    assert client.call_count == 1
    assert engine.state == settled
    assert lease.pending_retry == PendingRetry()
    assert lease.active_admission_count() == 0
    assert lease.run_task is None or lease.run_task.done()
    errors = _filter(started.events, Error)
    assert [(error.code, error.session_id) for error in errors] == [("working_dir_missing", engine.session.session_id)]
    assert str(work) in errors[0].message

    work.mkdir()
    await started.bus.publish(UserMessage(text="again"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert client.call_count == 2
    assert _final_texts(started.events)[-1] == "fresh after recreate"
    assert engine.state == EngineState.IDLE
    assert dispatch_results == [str(work), None]


async def test_queued_retry_uses_the_installed_workspace_after_an_edit(tmp_path, agent_engine, monkeypatch):
    primary = tmp_path / "primary"
    primary.mkdir()
    registry = JsonFileStateStore(tmp_path / "sessions").workspace_registry
    workspace = registry.create("Installed", str(primary))
    client = HeldFirstCallClient([MockResponse(text="first pass"), MockResponse(text="answer after retry")])
    started = await _start_in(agent_engine, tmp_path, monkeypatch, primary, client, workspace_id=workspace.id)
    engine = started.engine
    try:
        await started.bus.publish(UserMessage(text="initial prompt"))
        await asyncio.wait_for(client.first_call_entered.wait(), timeout=ENGINE_TURN_TIMEOUT)
        await started.bus.publish(UserRetry())
        assert engine.state is EngineState.PENDING_RETRY
        registry.update(workspace.id, name="Edited", primary=str(primary))
    finally:
        client.release_first_call.set()
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert not _filter(started.events, Error)
    assert _final_texts(started.events)[-1] == "answer after retry"
    assert engine.state is EngineState.IDLE
    assert engine.workspace.name == "Installed"
