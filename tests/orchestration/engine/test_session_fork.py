# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for backend session fork orchestration."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, SessionFork, SessionForked
from chrys.foundation.i18n import DisplayBlock
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.trajectory.event_types import EventType, ProfileKind
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.kernel import Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import trajectory_events_path
from tests.support.event_capture import capture_events
from tests.support.loaded_agents import install_loaded_agent
from tests.support.rollback_spec import ask
from tests.support.waiting import ENGINE_TURN_TIMEOUT
from tests.support.workspace_engine import workspace_case as workspace_case


async def _collect(out: list[object], event: object) -> None:
    out.append(event)


def _executor_with_state(
    messages: list[Message],
    compressed_msgs: list[CompressedBlock] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        backend=SimpleNamespace(history_state={"messages": messages, "compressed_msgs": compressed_msgs or []})
    )


def _executor_with_messages(messages: list[Message]) -> SimpleNamespace:
    return _executor_with_state(messages)


def _assert_display_message(event: Error, key: str, args: dict[str, object]) -> None:
    reference = event.display_message
    assert reference is not None
    assert reference.definition.key == key
    assert dict(reference.args) == args


async def _raise_timeout(_self: AgentEngine, **_kwargs: object) -> None:
    raise TimeoutError("write lock busy")


async def _raise_runtime_error(_self: AgentEngine, **_kwargs: object) -> None:
    raise RuntimeError("disk full")


@pytest.mark.parametrize(
    ("in_workspace", "committed"),
    [
        pytest.param(True, False, id="workspace-before-commit"),
        pytest.param(True, True, id="workspace-after-commit"),
        pytest.param(False, True, id="unspecified-after-commit"),
    ],
)
async def test_cancelled_fork_respects_the_disk_commit(workspace_case, tmp_path, monkeypatch, in_workspace, committed):
    workspace = workspace_case.registry.create("Team", str(tmp_path)) if in_workspace else None
    engine = await workspace_case(tmp_path, workspace_id=workspace.id if workspace else None)
    await ask(engine, "fork this turn")
    parent_id = engine.session_id
    assert parent_id is not None
    store = workspace_case.store
    root = store.session_dir(parent_id).parent
    registry_before = workspace_case.registry.snapshot()
    events = await capture_events(engine.event_bus, SessionForked)

    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finished_copy = asyncio.Event()
    release = threading.Event()
    prepare = store._prepare_fork_directory
    copy_session = store.fork_session

    def hold_copy():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(ENGINE_TURN_TIMEOUT), "fork copy was not released"

    def prepare_and_hold(
        tmp_dir: Path,
        *,
        dest_dir: Path,
        parent_session_id: str,
        new_session_id: str,
        copied_sub_agent_artifacts: list[str],
        last_surface: SessionSurface | None = None,
    ):
        prepared = prepare(
            tmp_dir,
            dest_dir=dest_dir,
            parent_session_id=parent_session_id,
            new_session_id=new_session_id,
            copied_sub_agent_artifacts=copied_sub_agent_artifacts,
            last_surface=last_surface,
        )
        if not committed:
            # The only cancellation point left before the rename.
            hold_copy()
        return prepared

    def copy(
        parent_session_id: str,
        *,
        last_surface: SessionSurface | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> str:
        try:
            if check_cancelled is None:
                new_id = copy_session(parent_session_id, last_surface=last_surface)
            else:
                new_id = copy_session(parent_session_id, last_surface=last_surface, check_cancelled=check_cancelled)
            if committed:
                hold_copy()
            return new_id
        finally:
            loop.call_soon_threadsafe(finished_copy.set)

    monkeypatch.setattr(store, "_prepare_fork_directory", prepare_and_hold)
    monkeypatch.setattr(store, "fork_session", copy)
    fork = asyncio.create_task(engine.event_bus.publish(SessionFork(session_id=parent_id), raise_handler_errors=True))
    try:
        await asyncio.wait_for(entered.wait(), timeout=ENGINE_TURN_TIMEOUT)
        assert len(list(root.glob("[!.]*/session.json"))) == (2 if committed else 1)
        assert not events
        fork.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(fork, timeout=ENGINE_TURN_TIMEOUT)
        await asyncio.wait_for(finished_copy.wait(), timeout=ENGINE_TURN_TIMEOUT)

    assert not list(root.glob(".*.fork.*.tmp"))
    assert len(list(root.glob("[!.]*/session.json"))) == (2 if committed else 1)
    if not committed:
        assert not events
        assert workspace_case.registry.snapshot() == registry_before
        return

    (forked,) = events
    assert forked.session_id == forked.parent_session_id == parent_id
    new_id = forked.new_session_id
    provenance = read_trajectory(trajectory_events_path(store.session_dir(new_id)))
    (origin,) = [record for record in provenance.events if record.event_type == EventType.SESSION_FORKED]
    assert origin.payload["origin_session_id"] == parent_id
    assert workspace_case.registry.snapshot().session_workspace(new_id) == workspace


@pytest.mark.asyncio
async def test_session_fork_saves_then_publishes_forked_without_owning_fork(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    events: list[object] = []
    await bus.subscribe(SessionForked, lambda event: _collect(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    parent_id = "parent-session"
    engine.session.session_id = parent_id
    install_loaded_agent(engine, bindings=_executor_with_messages([Message("user", ["hello"])]))
    assert engine.session.guard.ensure(parent_id)
    try:
        await engine._on_session_fork(SessionFork(session_id=parent_id))
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, SessionForked)
        assert event.session_id == parent_id
        assert event.parent_session_id == parent_id
        assert event.new_session_id
        assert engine.session.guard.owns(parent_id)
        assert not engine.session.guard.owns(event.new_session_id)
    finally:
        engine.session.guard.release()

    fork_meta = await store.load_session_meta(event.new_session_id)
    assert fork_meta is not None
    assert fork_meta.parent_session_id == parent_id
    loaded = await store.load_session(event.new_session_id)
    assert loaded is not None
    assert loaded["messages"][0].text == "hello"


@pytest.mark.asyncio
async def test_session_fork_rejects_empty_session(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    errors: list[object] = []
    await bus.subscribe(Error, lambda event: _collect(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    parent_id = "parent-session"
    engine.session.session_id = parent_id
    install_loaded_agent(engine, bindings=_executor_with_messages([]))
    assert engine.session.guard.ensure(parent_id)
    try:
        await engine._on_session_fork(SessionFork(session_id=parent_id))
    finally:
        engine.session.guard.release()

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, Error)
    assert error.code == "session_fork_empty"
    assert error.message == "Cannot fork an empty session."
    _assert_display_message(error, "engine.fork_empty_session", {})
    assert not store.session_dir(parent_id).exists()


@pytest.mark.asyncio
async def test_session_fork_allows_fully_compacted_session(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    events: list[object] = []
    await bus.subscribe(SessionForked, lambda event: _collect(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    parent_id = "parent-session"
    summary = Message("assistant", ["[Compressed context: ctx_1]\nSummary: old turn"])
    summary.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
    compressed = CompressedBlock(
        compressed_context_id="ctx_1",
        messages=[Message("user", ["old real turn"])],
        summary_text="old turn",
        marker_id="turn_1",
        turn_range=(1, 1),
        created_at="2026-03-17T00:00:00+00:00",
    )
    engine.session.session_id = parent_id
    install_loaded_agent(engine, bindings=_executor_with_state([summary], [compressed]))
    assert engine.session.guard.ensure(parent_id)
    try:
        await engine._on_session_fork(SessionFork(session_id=parent_id))
    finally:
        engine.session.guard.release()

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, SessionForked)
    loaded = await store.load_session(event.new_session_id)
    assert loaded is not None
    assert loaded["compressed_msgs"][0].messages[0].text == "old real turn"


@pytest.mark.asyncio
async def test_session_fork_without_ready_session_publishes_not_ready(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    errors: list[object] = []
    await bus.subscribe(Error, lambda event: _collect(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)

    await engine._on_session_fork(SessionFork(session_id="requested-session"))

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, Error)
    assert error.code == "session_fork_not_ready"
    assert error.session_id == "requested-session"
    assert error.message == "Cannot fork before a session is ready."
    _assert_display_message(error, "engine.fork_not_ready", {})


@pytest.mark.asyncio
async def test_session_fork_stale_error_targets_requested_session(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    errors: list[object] = []
    await bus.subscribe(Error, lambda event: _collect(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    engine.session.session_id = "active-session"
    install_loaded_agent(engine, bindings=_executor_with_messages([Message("user", ["hello"])]))

    await engine._on_session_fork(SessionFork(session_id="requested-session"))

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, Error)
    assert error.code == "session_fork_stale"
    assert error.session_id == "requested-session"
    assert error.message == "Cannot fork because the active session changed."
    _assert_display_message(error, "engine.fork_session_changed", {})


@pytest.mark.asyncio
async def test_session_fork_save_timeout_publishes_busy_error(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    errors: list[object] = []
    await bus.subscribe(Error, lambda event: _collect(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    parent_id = "parent-session"
    engine.session.session_id = parent_id
    install_loaded_agent(engine, bindings=_executor_with_messages([Message("user", ["hello"])]))
    engine.writer.save_current_session = MethodType(_raise_timeout, engine)
    assert engine.session.guard.ensure(parent_id)
    try:
        await engine._on_session_fork(SessionFork(session_id=parent_id))
    finally:
        engine.session.guard.release()

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, Error)
    assert error.code == "session_fork_busy"
    assert error.message == "Timed out preparing session for fork: write lock busy"
    _assert_display_message(error, "engine.fork_prepare_timeout", {"detail": DisplayBlock("write lock busy")})


@pytest.mark.asyncio
async def test_session_fork_save_failure_publishes_failed_error(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    errors: list[object] = []
    await bus.subscribe(Error, lambda event: _collect(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    parent_id = "parent-session"
    engine.session.session_id = parent_id
    install_loaded_agent(engine, bindings=_executor_with_messages([Message("user", ["hello"])]))
    engine.writer.save_current_session = MethodType(_raise_runtime_error, engine)
    assert engine.session.guard.ensure(parent_id)
    try:
        await engine._on_session_fork(SessionFork(session_id=parent_id))
    finally:
        engine.session.guard.release()

    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, Error)
    assert error.code == "session_fork_failed"
    assert error.message == "Failed to prepare session for fork: disk full"
    _assert_display_message(error, "engine.fork_prepare_failed", {"detail": DisplayBlock("disk full")})


@pytest.mark.asyncio
async def test_session_fork_store_save_failure_does_not_copy_stale_state(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("parent-session", {"messages": [Message("user", ["old"])], "compressed_msgs": []})
    bus = EventBus()
    errors: list[object] = []
    forked: list[object] = []
    await bus.subscribe(Error, lambda event: _collect(errors, event))
    await bus.subscribe(SessionForked, lambda event: _collect(forked, event))

    async def fail_save_session(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_session", fail_save_session)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    parent_id = "parent-session"
    engine.session.session_id = parent_id
    install_loaded_agent(engine, bindings=_executor_with_messages([Message("user", ["new"])]))
    assert engine.session.guard.ensure(parent_id)
    try:
        await engine._on_session_fork(SessionFork(session_id=parent_id))
    finally:
        engine.session.guard.release()

    assert forked == []
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, Error)
    assert error.code == "session_fork_failed"
    sessions = await store.list_sessions()
    assert [session.session_id for session in sessions] == [parent_id]


async def test_fork_prelude_keeps_parent_identity_after_restore_and_reload(
    workspace_case, tmp_path, monkeypatch
) -> None:
    from chrys.foundation.events.types import SessionRestore
    from tests.support.waiting import wait_for

    engine = await workspace_case(tmp_path)
    await ask(engine, "fork this history")
    parent_id = engine.session_id
    assert parent_id is not None
    store = workspace_case.store
    original = read_trajectory(trajectory_events_path(store.session_dir(parent_id))).events
    (parent_started,) = [event for event in original if event.event_type == EventType.SESSION_STARTED]
    profile = engine.agent_profile
    assert profile is not None
    other = tmp_path / "other"
    other_id = str(uuid4())
    await asyncio.to_thread(other.mkdir)
    await store.save_session(other_id, {"messages": [Message("user", ["another session"])]}, primary_cwd=str(other))

    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    copy_session = store.fork_session

    def held_copy(
        parent_session_id: str,
        *,
        last_surface: SessionSurface | None = None,
        check_cancelled: Callable[[], None] | None = None,
    ) -> str:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(ENGINE_TURN_TIMEOUT)
        if check_cancelled is None:
            return copy_session(parent_session_id, last_surface=last_surface)
        return copy_session(parent_session_id, last_surface=last_surface, check_cancelled=check_cancelled)

    monkeypatch.setattr(store, "fork_session", held_copy)
    bus = engine.event_bus
    forked = await capture_events(bus, SessionForked)
    task = asyncio.create_task(bus.publish(SessionFork(session_id=parent_id), raise_handler_errors=True))
    try:
        await wait_for(lambda: entered.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="fork copy")
        if task.done():
            await task
        await bus.publish(SessionRestore(session_id=other_id), raise_handler_errors=True)
        assert engine.session_id == other_id
        await engine.lifecycle.reload(replace(profile, name="Replacement", instructions="A different agent"))
        assert engine.current.manifest.agent_profile_fingerprint != parent_started.payload["agent_profile_fingerprint"]
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)

    (event,) = forked
    log = read_trajectory(trajectory_events_path(store.session_dir(event.new_session_id)))
    assert log.corrupt_lines == []
    (started,) = [item for item in log.events if item.event_type == EventType.SESSION_STARTED]
    (origin,) = [item for item in log.events if item.event_type == EventType.SESSION_FORKED]
    parent_log = read_trajectory(trajectory_events_path(store.session_dir(parent_id)))
    assert origin.payload == {
        "origin_session_id": parent_id,
        "forked_at_sequence": parent_log.events[-1].sequence,
    }
    assert parent_started.payload["agent_profile_fingerprint"]
    assert parent_started.payload["model_profile_fingerprint"]
    for key in ("agent_profile_fingerprint", "model_profile_fingerprint", "workspace_fingerprint"):
        assert started.payload[key] == parent_started.payload[key], key


async def test_fork_records_parent_events_added_during_copy(workspace_case, tmp_path, monkeypatch) -> None:
    engine = await workspace_case(tmp_path)
    await ask(engine, "fork this history")
    parent_id = engine.session_id
    assert parent_id is not None
    store = workspace_case.store
    parent_log = trajectory_events_path(store.session_dir(parent_id))
    sequence_before_copy = read_trajectory(parent_log).events[-1].sequence
    loop = asyncio.get_running_loop()
    prepare_directory = store._prepare_fork_directory

    def append_event_during_copy(*args, **kwargs):
        prepared = prepare_directory(*args, **kwargs)
        appended = asyncio.run_coroutine_threadsafe(
            engine._trajectory_recorder.profile_switched(
                kind=ProfileKind.AGENT, from_fingerprint="previous", to_fingerprint="current"
            ),
            loop,
        )
        appended.result(timeout=ENGINE_TURN_TIMEOUT)
        return prepared

    monkeypatch.setattr(store, "_prepare_fork_directory", append_event_during_copy)
    forked = await capture_events(engine.event_bus, SessionForked)
    await engine.event_bus.publish(SessionFork(session_id=parent_id), raise_handler_errors=True)

    (event,) = forked
    last_parent_event = read_trajectory(parent_log).events[-1]
    assert last_parent_event.event_type == EventType.PROFILE_SWITCHED
    assert last_parent_event.sequence > sequence_before_copy
    child_log = read_trajectory(trajectory_events_path(store.session_dir(event.new_session_id)))
    (origin,) = [item for item in child_log.events if item.event_type == EventType.SESSION_FORKED]
    assert origin.payload["origin_session_id"] == parent_id
    assert origin.payload["forked_at_sequence"] == last_parent_event.sequence
