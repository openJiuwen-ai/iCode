# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused failing repros for core session/runtime state bugs."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.foundation.observability.sink as otel_sink_module
import chrys.orchestration.engine.build.builder as builder_module
import chrys.service.state.locks as locks_module
import chrys.service.state.store as store_module
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, SessionDelete, SessionDeleted, SessionRestore
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import JsonFileStateStore
from tests.support.gc_isolation import earlier_tests_heap_frozen


def _awaiting_marker() -> Message:
    marker = Message("assistant", ["Awaiting 1 sub-agent(s)"])
    marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.AWAITING_SUB_AGENTS
    marker.additional_properties["_invocation_ids"] = ["inv-1"]
    return marker


def _profile() -> AgentProfile:
    return AgentProfile(
        name="test",
        instructions="Test.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )


def _model_registry() -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="mock-profile", name="mock", provider="mock", model_id="mock"))
    return registry


def _agent_registry(profile: AgentProfile) -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    registry.register(profile)
    return registry


def test_active_session_guard_writes_owner_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active-session ownership writes owner metadata and releases cleanly."""
    monkeypatch.setattr(locks_module, "SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS", 0.05)
    store = JsonFileStateStore(tmp_path)
    guard = ActiveSessionGuard(store)
    contender = ActiveSessionGuard(store)
    long_session_id = "40d9a048-3e08-4cff-a0e0-ce8c09d3e011"

    try:
        assert guard.ensure(long_session_id) is True
        assert guard.owns(long_session_id)

        owner_path = store.active_owner_path(long_session_id)
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        assert owner["session_id"] == long_session_id
        assert owner["pid"] == os.getpid()
        assert "host" not in owner
        assert "cwd" not in owner

        assert contender.ensure(long_session_id) is False
        assert contender.conflict_message(long_session_id) == (
            f"Session '40d9a0483e08' is already open in another {APP_DISPLAY_NAME} instance (pid={os.getpid()})."
        )

        guard.release()
        assert not owner_path.exists()

        assert contender.ensure(long_session_id) is True
        assert contender.owns(long_session_id)
    finally:
        contender.release()
        guard.release()


def test_active_session_guard_owns_equivalent_short_session_id(tmp_path: Path) -> None:
    """Equivalent short/full session ids should be treated as the current active session."""
    store = JsonFileStateStore(tmp_path)
    guard = ActiveSessionGuard(store)
    long_session_id = "40d9a048-3e08-4cff-a0e0-ce8c09d3e011"
    short_session_id = "40d9a0483e08"

    try:
        assert guard.ensure(long_session_id) is True
        assert guard.owns(short_session_id) is True
    finally:
        guard.release()


@pytest.mark.asyncio
async def test_engine_start_refuses_session_active_in_another_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine,
) -> None:
    """The startup path must enforce the same active-session lock as restore."""
    monkeypatch.setattr(locks_module, "SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS", 0.05)
    store = JsonFileStateStore(tmp_path)
    store.active_owner_path("busy").write_text(json.dumps({"pid": 12345}), encoding="utf-8")
    held = FileLock(store.active_lock_path("busy"), timeout=1.0)
    held.acquire()

    bus = EventBus()
    errors: list[Error] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    await bus.subscribe(Error, collect_error)
    engine = agent_engine(bus, settings=Settings(), state_store=store)
    engine.session.session_id = "busy"
    try:
        with pytest.raises(RuntimeError, match="already open"):
            await engine.start(_profile())
    finally:
        held.release()

    assert [e.code for e in errors] == ["session_in_use"]
    assert "pid=12345" in errors[0].message
    assert engine.session.session_id == "busy"


@pytest.mark.asyncio
async def test_awaiting_sub_agent_marker_does_not_inflate_session_message_count(tmp_path: Path) -> None:
    """The paused-sub-agent marker is internal status, not a user-visible message."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "paused",
        {
            "messages": [Message("user", ["start task"]), _awaiting_marker()],
            "compressed_msgs": [],
        },
        agent_profile="test",
    )

    sessions = await store.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].message_count == 1


@pytest.mark.asyncio
async def test_session_restore_persists_awaiting_marker_cleanup_before_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    """Restore cleanup must update persisted history before replay reloads it."""
    store = JsonFileStateStore(tmp_path)
    profile = _profile()
    await store.save_session(
        "paused",
        {
            "messages": [Message("user", ["start task"]), _awaiting_marker()],
            "compressed_msgs": [],
        },
        agent_profile=profile.name,
    )

    bus = EventBus()
    engine = assemble_agent_engine(
        bus,
        settings=Settings(model_profile="mock-profile"),
        model_registry=_model_registry(),
        agent_registry=_agent_registry(profile),
        state_store=store,
    )

    monkeypatch.setattr(
        builder_module,
        "create_client",
        create_autospec(builder_module.create_client, side_effect=lambda s=None, **kw: MockChatClient()),
    )
    try:
        await engine.on_session_restore(SessionRestore(session_id="paused"))

        in_memory_kinds = [
            m.additional_properties.get(HistoryMarkerKind.KEY) for m in engine_services(engine).history.messages
        ]
        assert HistoryMarkerKind.AWAITING_SUB_AGENTS not in in_memory_kinds

        raw_messages = await store.load_session_raw("paused")
        assert raw_messages is not None
        persisted_kinds = [m.get("additional_properties", {}).get(HistoryMarkerKind.KEY) for m in raw_messages]
        assert HistoryMarkerKind.AWAITING_SUB_AGENTS not in persisted_kinds
    finally:
        await engine.shutdown()


@pytest.mark.asyncio
async def test_session_restore_saves_current_session_before_switching_cwd(
    tmp_path: Path,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring folderB must not rewrite the current folderA session cwd."""
    store = JsonFileStateStore(tmp_path / "sessions")
    folder_a = tmp_path / "folderA"
    folder_b = tmp_path / "folderB"
    folder_a.mkdir()
    folder_b.mkdir()

    profile = _profile()
    await store.save_session(
        "target",
        {
            "messages": [Message("user", ["target session"])],
            "compressed_msgs": [],
        },
        agent_profile=profile.name,
        primary_cwd=str(folder_b),
    )

    bus = EventBus()
    engine = agent_engine(
        bus,
        settings=Settings(model_profile="mock-profile"),
        model_registry=_model_registry(),
        agent_registry=_agent_registry(profile),
        state_store=store,
    )

    original_cwd = os.getcwd()
    monkeypatch.setattr(
        builder_module,
        "create_client",
        create_autospec(builder_module.create_client, side_effect=lambda s=None, **kw: MockChatClient()),
    )
    try:
        os.chdir(folder_a)
        await engine.start(profile)
        current_session_id = engine.session.session_id
        assert current_session_id is not None
        assert engine.current.loaded is not None
        engine.current.loaded.bindings.backend.history_state = {
            "messages": [Message("user", ["current session"])],
            "compressed_msgs": [],
        }

        await engine.on_session_restore(SessionRestore(session_id="target"))

        sessions = await store.list_sessions()
        current_meta = next(s for s in sessions if s.session_id == current_session_id)
        target_meta = next(s for s in sessions if s.session_id == "target")

        assert Path(current_meta.primary_cwd).resolve() == folder_a.resolve()
        assert Path(target_meta.primary_cwd).resolve() == folder_b.resolve()
    finally:
        os.chdir(original_cwd)


@pytest.mark.asyncio
async def test_session_restore_does_not_chdir_to_target_workspace(
    tmp_path: Path,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring a session should switch engine workspace without process chdir."""
    store = JsonFileStateStore(tmp_path / "sessions")
    folder_a = tmp_path / "folderA"
    folder_b = tmp_path / "folderB"
    folder_a.mkdir()
    folder_b.mkdir()

    profile = _profile()
    await store.save_session(
        "target",
        {
            "messages": [Message("user", ["target session"])],
            "compressed_msgs": [],
        },
        agent_profile=profile.name,
        primary_cwd=str(folder_b),
    )

    bus = EventBus()
    engine = agent_engine(
        bus,
        settings=Settings(model_profile="mock-profile"),
        model_registry=_model_registry(),
        agent_registry=_agent_registry(profile),
        state_store=store,
    )

    original_cwd = os.getcwd()
    monkeypatch.setattr(
        builder_module,
        "create_client",
        create_autospec(builder_module.create_client, side_effect=lambda s=None, **kw: MockChatClient()),
    )
    try:
        os.chdir(folder_a)
        await engine.start(profile)
        assert engine.current.loaded is not None
        engine.current.loaded.bindings.backend.history_state = {
            "messages": [Message("user", ["current session"])],
            "compressed_msgs": [],
        }

        await engine.on_session_restore(SessionRestore(session_id="target"))

        assert engine.session.guard.owns("target")
        assert engine.workspace is not None
        assert Path(engine.workspace.primary_cwd).resolve() == folder_b.resolve()
        assert Path.cwd().resolve() == folder_a.resolve()
    finally:
        await engine.shutdown()
        os.chdir(original_cwd)

    contender = ActiveSessionGuard(store)
    try:
        assert contender.ensure("target") is True
        assert contender.owns("target")
    finally:
        contender.release()


@pytest.mark.asyncio
async def test_session_restore_refuses_session_active_in_another_instance(tmp_path: Path) -> None:
    """Restoring a session with a held active lock would risk divergent writers."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "busy",
        {
            "messages": [Message("user", ["hello"])],
            "compressed_msgs": [],
        },
        agent_profile="test",
    )

    owner = {"pid": 12345, "host": "test-host", "cwd": "/tmp/project"}
    store.active_owner_path("busy").write_text(json.dumps(owner), encoding="utf-8")
    held = FileLock(store.active_lock_path("busy"), timeout=1.0)
    held.acquire()

    bus = EventBus()
    errors: list[Error] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    await bus.subscribe(Error, collect_error)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    try:
        await engine.on_session_restore(SessionRestore(session_id="busy"))
    finally:
        held.release()
        await engine.shutdown()

    assert [e.code for e in errors] == ["session_in_use"]
    assert "pid=12345" in errors[0].message
    assert "host=" not in errors[0].message
    assert "cwd=" not in errors[0].message
    assert engine.session.session_id is None


@pytest.mark.asyncio
async def test_session_restore_active_lock_conflict_fails_fast(tmp_path: Path) -> None:
    """Interactive restore should not wait for the long active-lock timeout on conflict."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "busy",
        {
            "messages": [Message("user", ["hello"])],
            "compressed_msgs": [],
        },
        agent_profile="test",
    )

    held = FileLock(store.active_lock_path("busy"), timeout=1.0)
    held.acquire()

    bus = EventBus()
    errors: list[Error] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    await bus.subscribe(Error, collect_error)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    try:
        with earlier_tests_heap_frozen():
            start = time.monotonic()
            await engine.on_session_restore(SessionRestore(session_id="busy"))
            elapsed = time.monotonic() - start
    finally:
        held.release()
        await engine.shutdown()

    assert [e.code for e in errors] == ["session_in_use"]
    assert elapsed < 1.0
    assert engine.session.session_id is None


@pytest.mark.asyncio
async def test_session_restore_checks_active_lock_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    """Restore must not load or heal a session owned by another live process."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "busy",
        {
            "messages": [Message("user", ["hello"])],
            "compressed_msgs": [],
        },
        agent_profile="test",
    )

    held = FileLock(store.active_lock_path("busy"), timeout=1.0)
    held.acquire()

    bus = EventBus()
    errors: list[Error] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    async def fail_load_session(session_id: str) -> object:
        raise AssertionError(f"load_session should not run for active session {session_id}")

    await bus.subscribe(Error, collect_error)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    monkeypatch.setattr(engine_services(engine).persistence, "load_session", fail_load_session)

    try:
        await engine.on_session_restore(SessionRestore(session_id="busy"))
    finally:
        held.release()
        await engine.shutdown()

    assert [e.code for e in errors] == ["session_in_use"]
    assert engine.session.session_id is None


@pytest.mark.asyncio
async def test_session_restore_releases_target_lock_when_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    """A restore setup error after active-lock acquisition must not leak the lock."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "target",
        {
            "messages": [Message("user", ["hello"])],
            "compressed_msgs": [],
        },
        agent_profile="test",
    )

    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)

    async def fail_load_chat_for_restore(
        _session_id: str, *, prefer_recovery: bool, ignore_recovery: bool, repair: bool = True
    ) -> object:
        _ = prefer_recovery, ignore_recovery, repair
        raise RuntimeError("checkpoint read failed")

    monkeypatch.setattr(engine_services(engine).persistence, "load_chat_for_restore", fail_load_chat_for_restore)

    with pytest.raises(RuntimeError, match="checkpoint read failed"):
        await engine.on_session_restore(SessionRestore(session_id="target"))

    assert not store.active_owner_path("target").exists()
    with FileLock(store.active_lock_path("target"), timeout=1.0):
        pass


@pytest.mark.asyncio
async def test_session_delete_refuses_session_active_in_another_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a non-current session must respect another process's active lock."""
    monkeypatch.setattr(store_module, "SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS", 0.05)
    store = JsonFileStateStore(tmp_path)
    await store.save_session("busy", {"messages": [Message("user", ["hello"])], "compressed_msgs": []})
    store.active_owner_path("busy").write_text(
        json.dumps({"pid": 12345, "host": "test-host", "cwd": "/tmp/project"}),
        encoding="utf-8",
    )
    held = FileLock(store.active_lock_path("busy"), timeout=1.0)
    held.acquire()

    bus = EventBus()
    errors: list[Error] = []
    deleted: list[SessionDeleted] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    async def collect_deleted(event: SessionDeleted) -> None:
        deleted.append(event)

    await bus.subscribe(Error, collect_error)
    await bus.subscribe(SessionDeleted, collect_deleted)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)

    try:
        await engine._on_session_delete(SessionDelete(session_id="busy"))
    finally:
        held.release()
        await engine.shutdown()

    assert [e.code for e in errors] == ["session_in_use"]
    assert "pid=12345" in errors[0].message
    assert "host=" not in errors[0].message
    assert "cwd=" not in errors[0].message
    assert deleted == []
    assert await store.load_session("busy") is not None


@pytest.mark.asyncio
async def test_session_delete_current_session_removes_owner_and_directory(tmp_path: Path) -> None:
    """Current-session deletion holds its own active lock through the delete."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("current", {"messages": [Message("user", ["hello"])], "compressed_msgs": []})

    bus = EventBus()
    deleted: list[SessionDeleted] = []

    async def collect_deleted(event: SessionDeleted) -> None:
        deleted.append(event)

    await bus.subscribe(SessionDeleted, collect_deleted)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    assert engine.session.guard.ensure("current") is True
    engine.session.session_id = "current"

    await engine._on_session_delete(SessionDelete(session_id="current"))

    assert [e.session_id for e in deleted] == ["current"]
    assert not store.session_dir("current").exists()
    assert not store.active_owner_path("current").exists()
    with FileLock(store.active_lock_path("current"), timeout=1.0):
        pass


@pytest.mark.asyncio
async def test_session_delete_current_session_accepts_equivalent_short_id(tmp_path: Path) -> None:
    """Current-session delete should not contend with its own active lock when ids differ only by shortening."""
    store = JsonFileStateStore(tmp_path)
    long_session_id = "40d9a048-3e08-4cff-a0e0-ce8c09d3e011"
    short_session_id = "40d9a0483e08"
    await store.save_session(long_session_id, {"messages": [Message("user", ["hello"])], "compressed_msgs": []})

    bus = EventBus()
    deleted: list[SessionDeleted] = []

    async def collect_deleted(event: SessionDeleted) -> None:
        deleted.append(event)

    await bus.subscribe(SessionDeleted, collect_deleted)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    assert engine.session.guard.ensure(long_session_id) is True
    engine.session.session_id = long_session_id

    await engine._on_session_delete(SessionDelete(session_id=short_session_id))

    assert [e.session_id for e in deleted] == [short_session_id]
    assert engine.session.session_id is None
    assert not store.session_dir(long_session_id).exists()
    assert not store.active_owner_path(long_session_id).exists()
    with FileLock(store.active_lock_path(long_session_id), timeout=1.0):
        pass


@pytest.mark.asyncio
async def test_session_delete_current_session_keeps_state_when_write_lock_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed current-session delete must not half-detach the engine."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("current", {"messages": [Message("user", ["hello"])], "compressed_msgs": []})
    monkeypatch.setattr(store_module, "SESSION_WRITE_LOCK_TIMEOUT_SECONDS", 0.01)

    bus = EventBus()
    errors: list[Error] = []
    deleted: list[SessionDeleted] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    async def collect_deleted(event: SessionDeleted) -> None:
        deleted.append(event)

    await bus.subscribe(Error, collect_error)
    await bus.subscribe(SessionDeleted, collect_deleted)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    assert engine.session.guard.ensure("current") is True
    engine.session.session_id = "current"

    class FakeOtelSink:
        def __init__(self) -> None:
            self.deactivate_calls = 0
            self.activate_calls: list[tuple[str, Path | None]] = []

        def deactivate(self) -> None:
            self.deactivate_calls += 1

        def activate(self, session_id: str, session_dir: Path | None) -> None:
            self.activate_calls.append((session_id, session_dir))

    fake_otel_sink = FakeOtelSink()
    monkeypatch.setattr(otel_sink_module, "get_otel_sink", lambda: fake_otel_sink)

    lock_path = store_module.session_write_lock_path(tmp_path, "current")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = FileLock(lock_path, timeout=1.0)
    held.acquire()
    try:
        await engine._on_session_delete(SessionDelete(session_id="current"))
        assert engine.session.guard.owns("current")
    finally:
        held.release()
        engine.session.guard.release()

    assert [e.code for e in errors] == ["session_busy"]
    assert deleted == []
    assert engine.session.session_id == "current"
    assert fake_otel_sink.deactivate_calls == 0
    assert fake_otel_sink.activate_calls == []
    assert await store.load_session("current") is not None


@pytest.mark.asyncio
async def test_session_delete_current_session_restores_state_when_delete_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    """A non-timeout delete failure must not leave the active engine detached."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("current", {"messages": [Message("user", ["hello"])], "compressed_msgs": []})

    bus = EventBus()
    errors: list[Error] = []
    deleted: list[SessionDeleted] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    async def collect_deleted(event: SessionDeleted) -> None:
        deleted.append(event)

    await bus.subscribe(Error, collect_error)
    await bus.subscribe(SessionDeleted, collect_deleted)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    assert engine.session.guard.ensure("current") is True
    engine.session.session_id = "current"

    async def fail_delete(_session_id: str, *, allow_active: bool = False) -> None:
        _ = allow_active
        raise OSError("simulated delete failure")

    monkeypatch.setattr(engine_services(engine).persistence, "delete_session", fail_delete)
    try:
        await engine._on_session_delete(SessionDelete(session_id="current"))
        assert engine.session.guard.owns("current")
    finally:
        engine.session.guard.release()

    assert [e.code for e in errors] == ["session_delete_failed"]
    assert "simulated delete failure" in errors[0].message
    assert deleted == []
    assert engine.session.session_id == "current"
    assert await store.load_session("current") is not None


@pytest.mark.asyncio
async def test_session_delete_completes_when_post_delete_cleanup_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-delete side-effect failure must not corrupt state or read as a delete failure."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("current", {"messages": [Message("user", ["hello"])], "compressed_msgs": []})

    bus = EventBus()
    errors: list[Error] = []
    deleted: list[SessionDeleted] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    async def collect_deleted(event: SessionDeleted) -> None:
        deleted.append(event)

    await bus.subscribe(Error, collect_error)
    await bus.subscribe(SessionDeleted, collect_deleted)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    assert engine.session.guard.ensure("current") is True
    engine.session.session_id = "current"

    class ExplodingOtelSink:
        def __init__(self) -> None:
            self.deactivate_calls = 0

        def deactivate(self) -> None:
            self.deactivate_calls += 1
            raise RuntimeError("simulated otel deactivate failure")

    sink = ExplodingOtelSink()
    monkeypatch.setattr(otel_sink_module, "get_otel_sink", lambda: sink)

    await engine._on_session_delete(SessionDelete(session_id="current"))

    # The delete committed: cleanup failure was swallowed, not misreported.
    assert sink.deactivate_calls == 1
    assert errors == []
    assert [e.session_id for e in deleted] == ["current"]
    # State stays consistent: session id not restored to a deleted session, lock released.
    assert engine.session.session_id is None
    assert not engine.session.guard.owns("current")
    assert await store.load_session("current") is None
