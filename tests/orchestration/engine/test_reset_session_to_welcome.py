# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reset-to-welcome session teardown, spill reconciliation, empty-session-dir cleanup, and the session write-lock path."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import chrys.orchestration.engine.session_lifecycle as session_lifecycle
import chrys.service.trajectory.tombstone as tombstone_module
from chrys.foundation.config.settings import (
    SESSION_ROOT_DIR_ENV_VAR,
    Settings,
)
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
)
from chrys.foundation.trajectory.keys import ensure_owner_only_directory
from chrys.foundation.trajectory.lease import WriterLease
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.session_ids import session_short_id
from chrys.kernel import Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.build.construction import StagedBuild
from chrys.orchestration.engine.build.loaded import CompletedBuild
from chrys.orchestration.engine.state.machine import EngineState, Trigger
from chrys.service.context.compaction.spill import CATALOG_RELATIVE_PATH
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.profiles.agents.schema import (
    AgentProfile,
)
from chrys.service.session.message_metadata import stamp_message_created_at
from chrys.service.state.store import SESSION_RECOVERY_FILE_NAME, JsonFileStateStore
from chrys.service.trajectory.tombstone import pending_delete_intents
from tests.orchestration.engine._recovery_helpers import (
    _profile,
    _ResetExecutor,
    _SessionEndProbeHookManager,
)
from tests.support.event_capture import collect_events
from tests.support.loaded_agents import install_loaded_agent, make_loaded_agent
from tests.support.windows_replace import briefly_held_unlink


async def test_reset_session_to_welcome_deletes_state_and_reports_missing_profile(
    tmp_path: Path, *, engine_services
) -> None:
    events: list[Error] = []
    bus = EventBus()
    await bus.subscribe(Error, lambda event: collect_events(events, event))
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.turn_number = 7
    engine.session.runtime_meta.total_session_tokens = 11
    engine.session.runtime_meta.total_session_input_tokens = 5
    engine.session.runtime_meta.total_session_output_tokens = 6
    engine.session.runtime_meta.last_usage_details = {"total_token_count": 11}
    engine_services(engine).fsm.try_transition(Trigger.START)
    engine_services(engine).fsm.try_transition(Trigger.USER_MESSAGE)
    engine_services(engine).fsm.try_transition(Trigger.RUN_FAILED)
    engine.session.shutting_down = True

    session_dir = store.session_dir("reset_me")
    snapshots_dir = session_dir / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    (session_dir / "session.json.bak").write_text("{}", encoding="utf-8")
    (session_dir / SESSION_RECOVERY_FILE_NAME).write_text("{}", encoding="utf-8")
    (snapshots_dir / "turn_2.json").write_text("{}", encoding="utf-8")

    reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

    assert reset_succeeded is True
    assert not (session_dir / "session.json").exists()
    assert not (session_dir / "session.json.bak").exists()
    assert not (session_dir / SESSION_RECOVERY_FILE_NAME).exists()
    assert not snapshots_dir.exists()
    assert not session_dir.exists()
    assert engine.session.session_id == "reset_me"
    assert engine.session.turn_number == 0
    assert engine.session.runtime_meta.total_session_tokens == 0
    assert engine.session.runtime_meta.total_session_input_tokens == 0
    assert engine.session.runtime_meta.total_session_output_tokens == 0
    assert engine.session.runtime_meta.last_usage_details == {}
    assert engine.session.shutting_down is False
    assert engine.state is EngineState.UNINITIALIZED
    assert [event.code for event in events] == ["no_agent_profile"]


async def test_reset_session_to_welcome_leaves_no_delete_intent_for_the_id_it_restarts_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer still pinning the log turns the reset's cleanup into a logical
    delete. The restart keeps the session id, so nothing may be left naming
    that directory for a later sweep."""

    class _RefusesRename:
        """The module's own ``os`` name — patching the stdlib one is global."""

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

        def rename(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("rename refused")

    monkeypatch.setattr(tombstone_module, "os", _RefusesRename())

    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    lease_path = tombstone_module.session_lease_path(session_dir)
    ensure_owner_only_directory(lease_path.parent)
    # The stuck writer nobody can pull the directory from under.
    lease = WriterLease.try_acquire(lease_path)
    assert lease is not None
    try:
        assert await engine.lifecycle.reset_session_to_welcome("reset_me") is True
    finally:
        lease.release()

    assert engine.session.session_id == "reset_me"
    assert pending_delete_intents(session_dir.parent) == frozenset()


async def test_reset_session_to_welcome_reconciles_retained_spill_quota(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_with_spill"
    session_dir = store.session_dir("reset_with_spill")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    sub_agent_artifact = session_dir / "sub_agents" / "sessions" / "Explore_saved.json"
    sub_agent_artifact.parent.mkdir(parents=True)
    sub_agent_artifact.write_text("{}", encoding="utf-8")

    relative_path = "compactions/dropped/turn001/001_read_file_11111111.md"
    record_path = session_dir / relative_path
    record_path.parent.mkdir(parents=True)
    record_path.write_text("retained record\n<!-- end of record -->\n", encoding="utf-8")
    catalog = session_dir / CATALOG_RELATIVE_PATH
    catalog.write_text(
        json.dumps(
            {
                "record_id": "1" * 8,
                "relative_path": relative_path,
                "turn": 1,
                "round": 1,
                "tool": "read_file",
                "bytes": 1,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    engine.session.spill_quota.initialize(1)

    reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_with_spill")

    assert reset_succeeded is True
    assert session_dir.is_dir()
    assert sub_agent_artifact.is_file()
    assert not (session_dir / "session.json").exists()
    assert engine.session.spill_quota.spent_bytes == record_path.stat().st_size


async def test_spill_reconciliation_failure_does_not_abort_session_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "restore_with_broken_spill"
    session_dir = store.session_dir("restore_with_broken_spill")
    session_dir.mkdir(parents=True)
    engine.session.spill_quota.initialize(17)

    def fail_reconciliation(_session_dir: Path, _quota: object) -> None:
        raise OSError("spill catalog is unreadable")

    monkeypatch.setattr(session_lifecycle, "reconcile_spill_storage", fail_reconciliation)

    result = await engine.lifecycle._reconcile_existing_spill_storage()

    assert result == session_lifecycle.SpillReconciliationResult(0, 0, frozenset())
    assert not engine.session.spill_quota.storage_available
    assert engine.session.spill_quota.spent_bytes == 0
    assert engine.session.spill_quota.try_reserve(1) is False


async def test_malformed_spill_catalog_text_does_not_abort_session_lifecycle(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "restore_with_malformed_spill"
    session_dir = store.session_dir("restore_with_malformed_spill")
    catalog = session_dir / CATALOG_RELATIVE_PATH
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        '{"record_id":"1","relative_path":"compactions/dropped/turn001/'
        '001_read_file_11111111.md","turn":1,"round":1,'
        '"tool":"\\ud800","bytes":1,"created_at":"2026-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )
    engine.session.spill_quota.initialize(17)

    result = await engine.lifecycle._reconcile_existing_spill_storage()

    assert result == session_lifecycle.SpillReconciliationResult(0, 0, frozenset())
    assert engine.session.spill_quota.spent_bytes == 0


async def test_reset_session_to_welcome_no_lock_path_deletes_backup_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json.bak").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(engine.session, "session_write_lock_path", lambda _session_id: None)

    reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

    assert reset_succeeded is True
    assert not (session_dir / "session.json.bak").exists()
    assert not session_dir.exists()


async def test_reset_session_to_welcome_outlasts_a_reader_holding_session_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader holding session.json a moment (the sidebar's change preview) must not fail the reset on Windows."""
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.turn_number = 3
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    held = briefly_held_unlink(monkeypatch, session_file, failures=2)

    reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

    assert reset_succeeded is True
    assert not session_file.exists()
    assert held.sleeps == [0.01, 0.02]
    assert engine.session.turn_number == 0


async def test_reset_session_to_welcome_preserves_snapshots_when_state_delete_lock_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.turn_number = 5

    session_dir = store.session_dir("reset_me")
    snapshots_dir = session_dir / "snapshots"
    snapshots_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    snapshot_file = snapshots_dir / "turn_2.json"
    snapshot_file.write_text("{}", encoding="utf-8")
    lock_path = engine.session.session_write_lock_path("reset_me")
    assert lock_path is not None

    monkeypatch.setattr(session_lifecycle, "SESSION_WRITE_LOCK_TIMEOUT_SECONDS", 0.0)
    with FileLock(lock_path):
        reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

    assert reset_succeeded is False
    assert (session_dir / "session.json").exists()
    assert snapshot_file.exists()
    assert engine.session.turn_number == 5
    assert engine.session.suppress_save is False


async def test_reset_session_to_welcome_session_end_hooks_see_session_file(tmp_path: Path) -> None:
    events: list[Error] = []
    bus = EventBus()
    await bus.subscribe(Error, lambda event: collect_events(events, event))
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session.json"
    session_file.write_text("{}", encoding="utf-8")
    hook_manager = _SessionEndProbeHookManager(session_file)
    engine.session.hook_manager = hook_manager  # type: ignore[assignment]

    reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

    assert reset_succeeded is True
    assert hook_manager.exists_during_fire == [True]
    assert hook_manager.payloads[0]["session_id"] == "reset_me"
    assert not session_dir.exists()
    assert [event.code for event in events] == ["no_agent_profile"]


async def test_reset_session_to_welcome_delete_failure_restores_engine_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.turn_number = 5
    engine.session.runtime_meta.total_session_tokens = 23
    engine.session.runtime_meta.total_session_input_tokens = 17
    engine.session.runtime_meta.total_session_output_tokens = 6
    engine.session.runtime_meta.last_usage_details = {"total_token_count": 23}
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")

    def fail_delete(_session_dir: Path) -> None:
        raise OSError("delete failed")

    monkeypatch.setattr(session_lifecycle, "_delete_reset_session_files", fail_delete)

    reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

    assert reset_succeeded is False
    assert engine.session.session_id == "reset_me"
    assert engine.session.turn_number == 5
    assert engine.session.runtime_meta.total_session_tokens == 23
    assert engine.session.runtime_meta.total_session_input_tokens == 17
    assert engine.session.runtime_meta.total_session_output_tokens == 6
    assert engine.session.runtime_meta.last_usage_details == {"total_token_count": 23}
    assert engine.session.shutting_down is False
    assert engine.state is EngineState.UNINITIALIZED
    assert engine.session.suppress_save is False


async def test_failed_reset_recovery_unlink_does_not_refresh_created_at_on_next_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    message = Message("user", ["old history"])
    stamp_message_created_at(message, "2020-01-01T00:00:00+00:00")
    state = {"messages": [message], "compressed_msgs": []}
    await store.save_session("reset_me", state)
    await asyncio.to_thread(store.save_recovery_session, "reset_me", state)
    session_dir = store.session_dir("reset_me")
    primary_file = session_dir / "session.json"
    backup_file = session_dir / "session.json.bak"
    recovery_file = session_dir / SESSION_RECOVERY_FILE_NAME
    snapshots_dir = session_dir / "snapshots"
    snapshots_dir.mkdir()
    snapshot_file = snapshots_dir / "turn_1.json"
    snapshot_file.write_bytes(primary_file.read_bytes())
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    drifted = datetime.now(UTC).isoformat()
    recovery["meta"]["created_at"] = drifted
    recovery["meta"]["updated_at"] = drifted
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")
    real_unlink = session_lifecycle.unlink_with_retry

    def fail_recovery_unlink(path: str | os.PathLike[str]) -> None:
        if os.fspath(path) == os.fspath(recovery_file):
            raise OSError("simulated recovery unlink failure")
        real_unlink(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(session_lifecycle, "unlink_with_retry", fail_recovery_unlink)
        with pytest.raises(OSError, match="simulated recovery unlink failure"):
            session_lifecycle._delete_reset_session_files(session_dir)

    assert not primary_file.exists()
    assert not backup_file.exists()
    assert recovery_file.exists()
    assert snapshot_file.exists()

    await store.save_session("reset_me", state)

    saved = json.loads(primary_file.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(saved["meta"]["created_at"]) == datetime(2020, 1, 1, tzinfo=UTC)
    assert saved["state"]["messages"][0]["contents"][0]["text"] == "old history"
    assert snapshot_file.exists()


async def test_reset_session_to_welcome_delete_failure_releases_external_lock_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.agent_profile = _profile()
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    lock_path = engine.session.session_write_lock_path("reset_me")
    assert lock_path is not None
    external_lock = FileLock(lock_path, timeout=0.0)
    external_lock.acquire()
    lifecycle: list[str] = []

    def release_external_lock() -> None:
        lifecycle.append("released")
        external_lock.release()

    async def restart(_profile: AgentProfile, *, operation: str = "startup", **_kwargs: object) -> None:
        lifecycle.append(f"restart:{operation}")
        probe = FileLock(lock_path, timeout=0.0)
        probe.acquire()
        probe.release()

    def fail_delete(_session_dir: Path) -> None:
        raise OSError("delete failed")

    monkeypatch.setattr(engine.lifecycle, "start", restart)
    monkeypatch.setattr(session_lifecycle, "_delete_reset_session_files", fail_delete)
    try:
        reset_succeeded = await engine.lifecycle.reset_session_to_welcome(
            "reset_me",
            write_lock_held=True,
            before_restart=release_external_lock,
        )
    finally:
        external_lock.release()

    assert reset_succeeded is False
    assert lifecycle == ["released", "restart:reset_failed"]


async def test_reset_session_to_welcome_success_releases_external_lock_after_cleanup_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.agent_profile = _profile()
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    lock_path = engine.session.session_write_lock_path("reset_me")
    assert lock_path is not None
    external_lock = FileLock(lock_path, timeout=0.0)
    external_lock.acquire()
    lifecycle: list[str] = []
    real_cleanup = session_lifecycle.cleanup_empty_session_dir_path

    def assert_lock_is_held(stage: str) -> None:
        with pytest.raises(TimeoutError), FileLock(lock_path, timeout=0.0):
            pass
        lifecycle.append(stage)

    async def after_delete() -> None:
        assert_lock_is_held("after_delete")

    def cleanup(path: Path | None, *, path_reused: bool = False) -> None:
        if path is not None and not (path / "session.json").exists():
            assert_lock_is_held("cleanup")
        real_cleanup(path, path_reused=path_reused)

    def release_external_lock() -> None:
        lifecycle.append("released")
        external_lock.release()

    async def restart(_profile: AgentProfile, *, operation: str = "startup", **_kwargs: object) -> None:
        lifecycle.append(f"restart:{operation}")
        probe = FileLock(lock_path, timeout=0.0)
        probe.acquire()
        probe.release()

    monkeypatch.setattr(engine.lifecycle, "start", restart)
    monkeypatch.setattr(session_lifecycle, "cleanup_empty_session_dir_path", cleanup)
    try:
        reset_succeeded = await engine.lifecycle.reset_session_to_welcome(
            "reset_me",
            write_lock_held=True,
            after_delete=after_delete,
            before_restart=release_external_lock,
        )
    finally:
        external_lock.release()

    assert reset_succeeded is True
    assert lifecycle == ["after_delete", "cleanup", "released", "restart:reset"]


async def test_reset_session_to_welcome_delete_failure_restores_mutation_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    stamped_states = []
    stamp = session_lifecycle.stamp_history_item_ids

    def observe_stamping(state):
        stamp(state)
        stamped_states.append(state)

    monkeypatch.setattr(session_lifecycle, "stamp_history_item_ids", observe_stamping)
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "reset_me"
    engine.session.agent_profile = _profile()
    engine.session.turn_number = 5
    session_dir = store.session_dir("reset_me")
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    tracker = MutationTracker(SnapshotStore(session_dir))
    tracker.start_turn(3)
    engine.session.mutation_tracker = tracker

    install_loaded_agent(
        engine,
        bindings=_ResetExecutor(  # type: ignore[assignment]
            {"messages": [Message("user", ["saved"])], "compressed_msgs": [], "turn_counter": 5}
        ),
    )

    async def fake_build_agent(_profile: AgentProfile, staged: StagedBuild) -> CompletedBuild:
        return CompletedBuild(
            staged=staged,
            settings=engine.settings_handle.prepare(staged.loaded),
            workspace_retarget=engine_services(engine).workspace_change_tracker.resolve_retarget(
                staged.workspace,
                resolve_scope=engine.settings_handle.prepare(staged.loaded).effective.settings.workspace_change_notice,
            ),
            loaded=make_loaded_agent(
                bindings=_ResetExecutor({"messages": [], "compressed_msgs": [], "turn_counter": 0})
            ),
            manifest=engine.current.manifest,
            mutation_tracker=None,
            todo_tracker=None,
            compaction_strategy=None,
        )

    def fail_delete(_session_dir: Path) -> None:
        raise OSError("delete failed")

    monkeypatch.setattr(engine.loader, "build", fake_build_agent)
    monkeypatch.setattr(session_lifecycle, "_delete_reset_session_files", fail_delete)

    try:
        reset_succeeded = await engine.lifecycle.reset_session_to_welcome("reset_me")

        assert reset_succeeded is False
        assert engine.session.mutation_tracker is not None
        assert [turn.turn_id for turn in engine.session.mutation_tracker.get_all_turns()] == [3]
        assert engine.current.loaded is not None
        assert engine.current.loaded.bindings.backend.history_state["chrys_mutations"]["turns"][0]["turn_id"] == 3
        assert stamped_states == [engine_services(engine).history.state]
        assert all(
            read_analytics_item_id(message.additional_properties)
            for message in engine_services(engine).history.messages
        )
        assert engine.state is EngineState.IDLE
        assert engine.session.suppress_save is False
    finally:
        engine.session.guard.release()


def test_cleanup_empty_session_dir_removes_only_unsaved_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "empty"
    empty_dir = store.session_dir("empty")
    empty_dir.mkdir(parents=True)

    engine.lifecycle.cleanup_empty_session_dir()

    assert not empty_dir.exists()

    engine.session.session_id = "recovery"
    recovery_dir = store.session_dir("recovery")
    recovery_dir.mkdir(parents=True)
    (recovery_dir / SESSION_RECOVERY_FILE_NAME).write_text("{}", encoding="utf-8")

    engine.lifecycle.cleanup_empty_session_dir()

    assert recovery_dir.exists()

    engine.session.session_id = "saved"
    saved_dir = store.session_dir("saved")
    saved_dir.mkdir(parents=True)
    (saved_dir / "session.json").write_text("{}", encoding="utf-8")

    engine.lifecycle.cleanup_empty_session_dir()

    assert saved_dir.exists()


def test_cleanup_empty_session_dir_preserves_restorable_rollback_snapshots(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "snapshot-only"
    session_dir = store.session_dir("snapshot-only")
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "turn_1.json").write_text("{}", encoding="utf-8")

    engine.lifecycle.cleanup_empty_session_dir()

    assert session_dir.exists()


@pytest.mark.parametrize(
    "relative_path",
    [
        "sub_agents/legacy.json",
        "sub_agents/pending/active.json",
        "sub_agents/pending/unmatched/old.json",
        "sub_agents/pending/corrupt/bad.json",
        "sub_agents/sessions/Explore_a1b2c3d4e5f6.json",
    ],
)
def test_cleanup_empty_session_dir_preserves_sub_agent_artifacts(tmp_path: Path, relative_path: str) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "sub-agent-only"
    session_dir = store.session_dir("sub-agent-only")
    artifact = session_dir / relative_path
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")

    engine.lifecycle.cleanup_empty_session_dir()

    assert session_dir.exists()


def test_cleanup_empty_session_dir_preserves_a_recorded_trajectory(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "log-only"
    session_dir = store.session_dir("log-only")
    events = session_dir / "trajectory" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text("{}\n", encoding="utf-8")

    engine.lifecycle.cleanup_empty_session_dir()

    # Only an explicit clear or delete takes a session's log with it.
    assert session_dir.exists()

    events.write_text("", encoding="utf-8")
    engine.lifecycle.cleanup_empty_session_dir()

    # A file the writer opened and never wrote to records nothing.
    assert not session_dir.exists()


def test_cleanup_empty_session_dir_keeps_a_trajectory_it_cannot_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "unreadable"
    session_dir = store.session_dir("unreadable")
    (session_dir / "trajectory").mkdir(parents=True)

    class _Unreadable:
        def stat(self) -> None:
            raise PermissionError("cannot stat")

    monkeypatch.setattr("chrys.service.trajectory.session.trajectory_events_path", lambda _dir: _Unreadable())

    engine.lifecycle.cleanup_empty_session_dir()

    # "I could not look" is not "there is nothing there".
    assert session_dir.exists()


def test_cleanup_empty_session_dir_ignores_sub_agent_tmp_files(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "tmp-only"
    session_dir = store.session_dir("tmp-only")
    tmp_file = session_dir / "sub_agents" / "sessions" / ".Explore_a1b2c3d4e5f6.json.tmp"
    tmp_file.parent.mkdir(parents=True)
    tmp_file.write_text("{}", encoding="utf-8")

    engine.lifecycle.cleanup_empty_session_dir()

    assert not session_dir.exists()


def test_no_store_write_lock_path_uses_resolved_sessions_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_root = tmp_path / "custom-root"
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(custom_root))
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=None)

    lock_path = engine.session.session_write_lock_path("lock-session")

    assert lock_path == custom_root / "sessions" / ".locks" / f"{session_short_id('lock-session')}.write.lock"
    assert lock_path.parent.is_dir()
