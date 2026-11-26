# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The snapshot swap + reload path of ``_on_user_rollback`` and the ``_suppress_save`` gate around it."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import chrys.orchestration.engine.engine as engine_module
import chrys.orchestration.engine.rollback as rollback_module
import chrys.orchestration.engine.session_lifecycle as lifecycle_module
import chrys.service.tools.background.crash as background_crash
from chrys.app.tui.screens.main.event_handlers import BackendEventHandler
from chrys.foundation.events.types import Error, RollbackResult, SessionRestore, SessionRestored, UserRollback, Warning
from chrys.foundation.i18n import DisplayBlock, Localizer
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import files as platform_files
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.mutations import workspace_changes
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource, RollbackWarning
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.state.serializers import serialize_state
from chrys.service.state.store import (
    ChatRestoreKindError,
    JsonFileStateStore,
    StartupSessionInUseError,
    atomic_copy_file,
)
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY, turn_records
from chrys.service.workspaces.paths import MissingWorkspacePrimary
from tests.orchestration.engine._rollback_helpers import (
    _collect_events,
    _make_engine,
    _state_after_turns,
    _StubExec,
    _write_session_json,
    fake_reset,
    fake_restore_factory,
    fake_start_factory,
    seed_turns,
)
from tests.support.background_tasks import HeldProcess
from tests.support.components import plan_workspace
from tests.support.event_capture import assert_display_message, capture_event_sequence, capture_events
from tests.support.loaded_agents import install_loaded_agent
from tests.support.rollback_spec import SpecClient, answer, ask
from tests.support.waiting import ENGINE_TURN_TIMEOUT
from tests.support.workspace_engine import workspace_case as workspace_case


def _prompts(state):
    messages = [m for block in state["compressed_msgs"] for m in block.messages] + state["messages"]
    return [m.text for m in messages if m.role == "user"]


async def _cold_open(case, tmp_path, sid):
    """Fork the durable checkpoint files before any old-engine save or shutdown."""
    root = tmp_path / "cold-sessions"
    cold_store = JsonFileStateStore(root)
    directory = case.store.session_dir(sid)
    cold_dir = cold_store.session_dir(sid)
    cold_dir.mkdir(parents=True)
    if case.store.workspace_registry.path.exists():
        registry_path = cold_store.workspace_registry.path
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case.store.workspace_registry.path, registry_path)
    for name in ("session.json", "session.json.bak", "session.recovery.json", "snapshots"):
        source = directory / name
        if source.is_dir():
            shutil.copytree(source, cold_dir / name)
        elif source.is_file():
            shutil.copy2(source, cold_dir / name)

    cold = await case(tmp_path, store=cold_store, client=SpecClient([answer("continued")]))
    async with capture_event_sequence(cold.event_bus, Error, SessionRestored) as events:
        await cold.event_bus.publish(SessionRestore(session_id=sid), raise_handler_errors=True)
    (outcome,) = events
    return cold, outcome


async def _check_save_and_cold_open(engine, case, tmp_path, sid, prompts, *, missing_primary=False):
    cold, outcome = await _cold_open(case, tmp_path, sid)
    if missing_primary:
        assert isinstance(outcome, Error) and outcome.code == "session_cwd_missing"
    elif prompts:
        assert isinstance(outcome, SessionRestored) and cold.session_id == sid
        assert _prompts(cold.current.require_loaded().bindings.backend.history_state) == prompts
        assert cold.session.turn_number == len(prompts)
    else:
        assert isinstance(outcome, Error) and outcome.code == "session_not_found"
    assert engine.session.turn_number == len(prompts)
    assert await engine.writer.save_current_session() == bool(prompts)
    saved = await case.store.load_session(sid, prefer_recovery=True)
    if prompts:
        assert saved is not None and saved["turn_counter"] == len(prompts) and _prompts(saved) == prompts
    else:
        assert saved is None

    return cold


def _rollback_records(directory):
    return [
        row for row in read_trajectory(trajectory_events_path(directory)).events if row.event_type == "session.rollback"
    ]


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("missing", "rollback_unavailable"),
        ("removed-under-lock", "rollback_snapshot_missing"),
        ("read-error", "rollback_swap_failed"),
        ("late-owner", "rollback_session_changed"),
        ("late-primary", "workspace_primary_missing"),
        ("owner", "rollback_session_changed"),
        ("precommit-cancel", None),
        ("missing-primary", "workspace_primary_missing"),
        ("lock", "rollback_swap_locked"),
        *(
            (fault, "rollback_swap_failed")
            for fault in (
                "json",
                "encoding",
                "envelope",
                "missing-state",
                "markers-only",
                "empty-compressed-block",
                "empty",
                "counter",
                "unknown",
                "foreign",
                "workflow",
                "modern-publish",
                "patch-publish",
            )
        ),
    ],
)
async def test_refusal_preserves_checkpoint_files_commands_and_next_save(
    tmp_path, workspace_case, monkeypatch, fault, code
):
    """Main preserves early refusals and cancellation. Late disappearance, lock and I/O faults can stop jobs.
    Main advances the generation before a failed primary write and can promote invalid or empty snapshots.
    """
    if fault in ("missing-primary", "late-primary"):
        folder = tmp_path / "workspace"
        folder.mkdir()
        engine, _ = await workspace_case.start_selected(folder)
    else:
        engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    tasks = engine.session.background_tasks
    monkeypatch.setattr(background_crash, "_members", lambda _pid: ())
    kept_process = HeldProcess()
    kept = tasks.adopt(kept_process, command="serve", trigger="timeout")
    assert kept is not None
    await ask(engine, "second")
    await ask(engine, "third")
    sid = engine.session_id
    directory = workspace_case.store.session_dir(sid)
    primary, snapshot = directory / "session.json", directory / "snapshots/turn_2.json"
    envelope = json.loads(snapshot.read_bytes())
    if fault == "missing-primary":
        folder.rmdir()
    elif fault == "missing":
        snapshot.unlink()
    elif fault in ("json", "encoding", "envelope"):
        snapshot.write_bytes({"json": b"{broken", "encoding": bytes([255]), "envelope": b"{}"}[fault])
    else:
        if fault == "missing-state":
            del envelope["state"]
        elif fault in ("empty", "counter", "unknown"):
            envelope["state"] = {"empty": {}, "counter": {"turn_counter": 1}, "unknown": {"unknown": True}}[fault]
        elif fault == "markers-only":
            envelope["state"]["messages"] = [envelope["state"]["messages"][-1]]
            assert envelope["state"]["messages"][0]["additional_properties"]["_turn"] == 1
        elif fault == "empty-compressed-block":
            envelope["state"] = {
                "compressed_msgs": [{"compressed_context_id": "ctx", "summary_text": "", "messages": []}]
            }
        elif fault == "foreign":
            envelope["meta"]["session_id"] = "other-session"
        elif fault == "workflow":
            envelope["meta"]["kind"] = "workflow"
        elif fault == "patch-publish":
            del envelope["state"]["turn_counter"]
            envelope["meta"]["custom_title"] = "old title"
        snapshot.write_bytes(json.dumps(envelope).encode())

    target = tmp_path / "output.txt"
    tracker = engine.mutation_tracker
    tracker.start_turn(2)
    _record_file_edit(tracker, target)
    retained = tmp_path / "retained.txt"
    retained.write_text("independent", encoding="utf-8")
    mutations = tracker.serialize()
    process = HeldProcess()
    command = tasks.adopt(process, command="build", trigger="timeout")
    assert command is not None

    def checkpoint_bytes():
        return {
            p.name: p.read_bytes()
            for p in [
                primary,
                directory / "session.json.bak",
                directory / "session.recovery.json",
                *snapshot.parent.glob("*.json"),
            ]
            if p.is_file()
        }

    if fault.endswith("-publish"):
        # A failed swap must leave the newer three-turn crash checkpoint byte-for-byte recoverable.
        workspace_case.store.save_recovery_session(sid, engine.current.require_loaded().bindings.backend.history_state)
        assert (directory / "session.recovery.json").is_file()
    before = checkpoint_bytes()
    runtime, generation = engine.current.loaded, engine.session_generation
    real_replace = platform_files.replace_with_retry

    def fail_publish(source, dest):
        if dest == primary:
            raise OSError("publication failed")
        real_replace(source, dest)

    def lock_busy(*args, **kwargs):
        raise TimeoutError("snapshot write lock busy")

    with monkeypatch.context() as patch:
        if fault == "read-error":
            original_read = Path.read_bytes

            def read(path):
                if path == snapshot:
                    raise PermissionError("unreadable snapshot")
                return original_read(path)

            patch.setattr(Path, "read_bytes", read)
        elif fault == "removed-under-lock":

            class RemovingLock(FileLock):
                def __enter__(self):
                    super().__enter__()
                    snapshot.unlink()
                    return self

            patch.setattr(rollback_module, "FileLock", RemovingLock)
            del before[snapshot.name]
        elif fault in ("late-owner", "late-primary"):
            coordinator = engine.session.mutation_coordinator
            augment = coordinator.augment_rollback_plan
            loop = asyncio.get_running_loop()

            async def advance_owner() -> None:
                engine.permits.advance_session_generation()

            def change_during_plan(*args, **kwargs):
                plan = augment(*args, **kwargs)
                if fault == "late-owner":
                    # Wait for the loop to apply the owner change, so rollback resumes after it.
                    asyncio.run_coroutine_threadsafe(advance_owner(), loop).result(timeout=ENGINE_TURN_TIMEOUT)
                else:
                    folder.rmdir()
                return plan

            patch.setattr(coordinator, "augment_rollback_plan", change_during_plan)
        elif fault.endswith("-publish"):
            patch.setattr(platform_files, "replace_with_retry", fail_publish)
        elif fault == "lock":
            patch.setattr(rollback_module, "FileLock", lock_busy)
        async with capture_event_sequence(engine.event_bus, Error, Warning, RollbackResult, SessionRestored) as events:
            request = UserRollback(target_turn=1, revert_changes=True, session_id="other" if fault == "owner" else sid)
            if fault == "precommit-cancel":
                gate = workspace_case.gate(engine.rollback, "_prepare_user_rollback_with_permit")
                task = asyncio.create_task(engine.event_bus.publish(request, raise_handler_errors=True))
                await asyncio.wait_for(gate.entered.wait(), ENGINE_TURN_TIMEOUT)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert events == []
            else:
                await engine.event_bus.publish(request, raise_handler_errors=True)
                (refusal,) = events
                assert isinstance(refusal, (Error, Warning)) and refusal.code == code
    assert checkpoint_bytes() == before
    assert engine.session_id == sid and engine.current.loaded is runtime
    assert tracker.serialize() == mutations and engine.session_generation == generation + (fault == "late-owner")
    assert tasks.running == [kept, command] and process.signals == kept_process.signals == []
    assert target.read_text(encoding="utf-8") == "after" and retained.read_text(encoding="utf-8") == "independent"
    assert _rollback_records(directory) == []
    await _check_save_and_cold_open(
        engine,
        workspace_case,
        tmp_path,
        sid,
        ["first", "second", "third"],
        missing_primary=fault in ("missing-primary", "late-primary"),
    )


@pytest.mark.parametrize(
    "layout", ["modern", "missing-backup", "legacy-messages", "compressed-only", "legacy-numeric", "missing-boundary"]
)
async def test_rollback_restores_the_turn_number_and_can_be_repeated(
    tmp_path: Path, workspace_case, monkeypatch, layout: str
) -> None:
    """Main passes modern and failed backup. Missing counters load as 0/1, missing boundaries advance the turn."""
    client = SpecClient([answer(text) for text in ("one", "two", "three", "continued")])
    engine = await workspace_case(tmp_path, client=client)
    for prompt in ("first", "second", "third"):
        await ask(engine, prompt)
    sid = engine.session_id
    assert sid is not None
    directory = workspace_case.store.session_dir(sid)
    snapshot = directory / "snapshots" / "turn_3.json"
    envelope = json.loads(snapshot.read_bytes())
    messages = envelope["state"]["messages"]
    if layout == "compressed-only":
        envelope["state"] = {
            "compressed_msgs": [{"compressed_context_id": "ctx", "summary_text": "past turns", "messages": messages}]
        }
    elif layout == "missing-boundary":
        messages.pop()
    elif layout not in ("modern", "missing-backup"):
        if layout == "legacy-numeric":
            snapshot = snapshot.rename(snapshot.with_name("2.json"))
            messages = [message for message in messages if "_turn" not in message.get("additional_properties", {})]
        envelope["state"] = {"messages": messages}
    if layout not in ("modern", "missing-backup"):
        snapshot.write_text(json.dumps(envelope), encoding="utf-8")
    original = snapshot.read_bytes()

    if layout == "missing-boundary":
        workspace_case.store.save_recovery_session(
            sid, engine.current.require_loaded().bindings.backend.history_state, last_surface=SessionSurface.ACP
        )

    with monkeypatch.context() as patch:
        if layout == "missing-backup":
            replace_file = platform_files.replace_with_retry

            def fail_backup(source, dest):
                if dest == directory / "session.json.bak":
                    raise OSError("backup is unavailable")
                replace_file(source, dest)

            patch.setattr(platform_files, "replace_with_retry", fail_backup)
        async with capture_event_sequence(engine.event_bus, Error, RollbackResult, SessionRestored) as events:
            await engine.event_bus.publish(UserRollback(target_turn=2), raise_handler_errors=True)

    restored, result = events
    assert isinstance(restored, SessionRestored)
    assert isinstance(result, RollbackResult) and result.target_turn == 2
    assert engine.session.turn_number == 2
    assert 0 in engine.available_rollback_turns()
    assert snapshot.read_bytes() == original
    primary = (directory / "session.json").read_bytes()
    if layout == "missing-boundary":
        assert not (directory / "session.recovery.json").exists()
        assert json.loads(primary)["meta"]["last_surface"] == "acp"
    assert json.loads(primary)["state"]["turn_counter"] == 2
    if layout == "missing-backup":
        assert not (directory / "session.json.bak").exists()
    else:
        assert (directory / "session.json.bak").read_bytes() == primary
    if layout in ("modern", "missing-backup"):
        assert primary == original
    saved = await workspace_case.store.load_session(sid)
    assert saved is not None
    kept = saved["compressed_msgs"][0].messages if layout == "compressed-only" else saved["messages"]
    assert [message.text for message in kept if message.role == "user"] == ["first", "second"]

    cold = await _check_save_and_cold_open(engine, workspace_case, tmp_path, sid, ["first", "second"])
    async with capture_event_sequence(cold.event_bus, Error, SessionRestored) as reopened:
        await cold.event_bus.publish(SessionRestore(session_id=sid), raise_handler_errors=True)
    assert len(reopened) == 1 and isinstance(reopened[0], SessionRestored)
    assert cold.session.turn_number == 2
    await ask(cold, "continued from disk")
    assert cold.session.turn_number == 3 and 2 in cold.available_rollback_turns()
    await ask(engine, "continue with turn three")
    assert engine.session.turn_number == 3
    assert 2 in engine.available_rollback_turns()
    async with capture_event_sequence(engine.event_bus, Error, RollbackResult, SessionRestored) as events:
        await engine.event_bus.publish(UserRollback(target_turn=2), raise_handler_errors=True)
    restored, result = events
    assert isinstance(restored, SessionRestored)
    assert isinstance(result, RollbackResult) and result.target_turn == 2
    assert engine.session.turn_number == 2


async def test_cold_restore_after_a_committed_rollback_fails_to_stop_commands(tmp_path, workspace_case, monkeypatch):
    """Main stops before publication and the original three-turn checkpoint still wins on cold restore."""
    engine = await workspace_case(tmp_path)
    for prompt in ("first", "second", "third"):
        await ask(engine, prompt)
    sid = engine.session_id
    store = workspace_case.store
    store.save_recovery_session(sid, engine.current.require_loaded().bindings.backend.history_state)
    directory = store.session_dir(sid)
    sidecar = directory / "session.recovery.json"
    assert json.loads(sidecar.read_bytes())["state"]["turn_counter"] == 3

    at_stop = []
    generation = engine.session_generation

    async def fail_stop(_target):
        primary = json.loads((directory / "session.json").read_bytes())
        at_stop.append((primary["state"]["turn_counter"], sidecar.exists(), engine.session_generation))
        raise OSError("cannot stop background commands")

    monkeypatch.setattr(engine.session.background_tasks, "stop_turns_after", fail_stop)
    async with capture_event_sequence(engine.event_bus, Error, RollbackResult, SessionRestored) as events:
        await engine.event_bus.publish(UserRollback(target_turn=1, revert_changes=False))

    cold, restored = await _cold_open(workspace_case, tmp_path, sid)
    assert isinstance(restored, SessionRestored)
    assert _prompts(cold.current.require_loaded().bindings.backend.history_state) == ["first"]
    assert cold.session.turn_number == 1
    assert at_stop == [(1, False, generation + 1)]
    assert not sidecar.exists()
    (error,) = events
    assert isinstance(error, Error) and error.code == "session_restore_failed"
    assert engine.current.loaded is None and not await engine.writer.save_current_session()


@pytest.mark.parametrize("failure", ["missing", "in-use", "wrong-kind", "settings", "lock-release"])
async def test_committed_rollback_retires_the_old_runtime_when_restore_fails(
    tmp_path: Path, workspace_case, monkeypatch, failure: str
) -> None:
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    await ask(engine, "second")
    sid = engine.session_id
    assert sid is not None
    store = workspace_case.store

    async def load_failed(session_id, **kwargs):
        assert session_id == sid
        if failure == "in-use":
            raise StartupSessionInUseError(session_id, None)
        if failure == "wrong-kind":
            raise ChatRestoreKindError("Open this session in Workflow mode.")
        return

    async def settings_failed(*args, **kwargs):
        raise OSError("could not read settings")

    class LockReleaseFailed(FileLock):
        def __exit__(self, *args):
            super().__exit__(*args)
            raise OSError("snapshot write lock release failed")

    if failure == "settings":
        monkeypatch.setattr(engine.lifecycle, "stage_workspace_settings", settings_failed)
    elif failure == "lock-release":
        monkeypatch.setattr(rollback_module, "FileLock", LockReleaseFailed)
    else:
        monkeypatch.setattr(store, "load_chat_for_restore", load_failed)

    async with capture_event_sequence(engine.event_bus, Error, RollbackResult, SessionRestored) as events:
        await engine.event_bus.publish(UserRollback(target_turn=1, revert_changes=True))

    (error,) = events
    assert isinstance(error, Error)
    assert (error.code, error.session_id) == ("session_restore_failed", sid)
    assert engine.session_id is None
    assert engine.current.loaded is None
    assert not await engine.writer.save_current_session()
    disk = await store.load_session(sid)
    assert disk is not None
    assert disk["turn_counter"] == 1
    assert [message.text for message in disk["messages"] if message.role == "user"] == ["first"]


@pytest.mark.parametrize(("target_turn", "failure"), [(0, "build"), (1, "build"), (1, "missing-checkpoint")])
async def test_committed_rollback_reports_file_outcomes_when_the_chat_cannot_reopen(
    tmp_path: Path, workspace_case, monkeypatch: pytest.MonkeyPatch, target_turn: int, failure: str
) -> None:
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    await ask(engine, "second")
    restored = tmp_path / "restored.txt"
    blocked = [tmp_path / f"blocked-{index}.txt" for index in range(5)]
    unselected = tmp_path / "unselected.txt"
    tracker = engine.mutation_tracker
    tracker.start_turn(2)
    for path in (restored, *blocked, unselected):
        path.write_text("before", encoding="utf-8")
        mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, path.name)
        assert mutation is not None
        path.write_text("after", encoding="utf-8")
        tracker.record_after(mutation)
    for path in blocked:
        path.unlink()
        path.mkdir()

    async def fail_build(*args, **kwargs):
        raise MissingWorkspacePrimary(str(tmp_path / "missing-build-root"))

    async def missing_checkpoint(*args, **kwargs):
        return None

    if failure == "build":
        monkeypatch.setattr(engine.loader, "build", fail_build)
    else:
        monkeypatch.setattr(workspace_case.store, "load_chat_for_restore", missing_checkpoint)

    async with capture_event_sequence(engine.event_bus, Error, RollbackResult) as events:
        await engine.event_bus.publish(
            UserRollback(
                target_turn=target_turn, revert_changes=True, selected_paths=[str(restored), *map(str, blocked)]
            )
        )

    assert restored.read_text(encoding="utf-8") == "before"
    assert all(path.is_dir() for path in blocked)
    assert unselected.read_text(encoding="utf-8") == "after"
    (error,) = events
    assert isinstance(error, Error)
    assert error.code == "session_restore_failed"
    assert "1 file restored, 5 failed." in error.message
    assert "blocked-0.txt: refusing to overwrite directory" in error.message
    assert "(+2 more)" in error.message
    assert "blocked-3.txt" not in error.message and "blocked-4.txt" not in error.message
    assert "unselected.txt" in error.message
    assert error.display_message is not None and error.display_hint is not None
    localizer = Localizer("zh-Hans")
    renderer = SimpleNamespace(_render_display=localizer.render)
    display = BackendEventHandler._render_display_with_hint(renderer, error.display_message, error.display_hint)
    if failure == "build":
        assert "主文件夹不存在" in display
    assert "已还原 1 个文件" in display
    assert "\n失败：blocked-0.txt" in display  # noqa: RUF001
    assert "另有 2 个" in display
    assert "�" not in display


@pytest.mark.parametrize("target_turn", [0, 1])
@pytest.mark.parametrize("restart_fails", [False, True])
@pytest.mark.parametrize("file_outcome", ["interrupted-batch", "unselected-only"])
async def test_file_outcome_survives_restart(
    tmp_path, workspace_case, engine_services, monkeypatch, target_turn, restart_fails, file_outcome
):
    """Main omits unselected/unknown outcomes from notifications and can tell the model a restored file was retained."""
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    await ask(engine, "second")
    tracker = engine.mutation_tracker
    tracker.start_turn(2)
    selected, unselected = tmp_path / "selected.txt", tmp_path / "untouched.txt"
    _record_file_edit(tracker, unselected)
    selection = [str(selected)]
    if file_outcome == "interrupted-batch":
        _record_file_edit(tracker, selected)

        def interrupted(turns, *, only_paths, plan):
            snapshot = dict(plan.entries)[str(selected)]
            assert tracker.store.restore(snapshot).changed
            raise OSError("lost the batch result")

        monkeypatch.setattr(tracker, "rollback_turns", interrupted)

    if restart_fails:

        async def fail_build(*args, **kwargs):
            raise RuntimeError("restart failed")

        monkeypatch.setattr(engine.loader, "build", fail_build)

    async with capture_event_sequence(engine.event_bus, Error, RollbackResult) as events:
        await engine.event_bus.publish(
            UserRollback(target_turn=target_turn, revert_changes=True, selected_paths=selection)
        )
    (outcome,) = events
    assert unselected.read_text(encoding="utf-8") == "after"
    if file_outcome == "interrupted-batch":
        assert selected.read_text(encoding="utf-8") == "before"

    if restart_fails:
        assert isinstance(outcome, Error) and outcome.code == "session_restore_failed"
        assert engine.current.loaded is None and not await engine.writer.save_current_session()
        assert outcome.display_hint is not None
        reports = [Localizer("en").render(outcome.display_hint)]
    else:
        assert isinstance(outcome, RollbackResult)
        assert outcome.restore_results == [] and outcome.files_reverted == 0
        notice = engine_services(engine).workspace_change_tracker.take_pending_notice()
        assert notice is not None and "selected.txt" not in notice
        from unittest.mock import Mock

        from chrys.app.acp.server import _rollback_result_payload
        from chrys.app.tui.screens.main.ports import RollbackView
        from tests.app.tui.screens.main.test_rollback_controller import _make_rollback_controller

        expected = (
            ["File restoration results are unknown. Check the working files."]
            if file_outcome == "interrupted-batch"
            else []
        )
        assert json.loads(json.dumps(_rollback_result_payload(outcome)))["warnings"] == expected
        view = Mock(spec=RollbackView)
        await _make_rollback_controller(view=view, workspace_cwd=lambda: str(tmp_path)).on_result(outcome)
        reports = [view.notify.call_args.args[0], notice]
    for report in reports:
        assert unselected.name in report and selected.name not in report
        assert ("results are unknown" in report) == (file_outcome == "interrupted-batch")


async def test_committed_rollback_reports_a_plan_with_only_warnings(
    tmp_path: Path, workspace_case, monkeypatch
) -> None:
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    await ask(engine, "second")
    tracker = engine.mutation_tracker
    tracker.start_turn(2)
    get_plan = tracker.get_rollback_plan_for_turns

    def warning_plan(turns):
        return replace(
            get_plan(turns),
            warnings=[RollbackWarning(code="peer_active", message=f"peer warning {i}") for i in range(5)],
        )

    async def fail_build(*args, **kwargs):
        raise RuntimeError("restore build failed")

    monkeypatch.setattr(tracker, "get_rollback_plan_for_turns", warning_plan)
    monkeypatch.setattr(engine.loader, "build", fail_build)
    async with capture_event_sequence(engine.event_bus, Error, RollbackResult) as events:
        await engine.event_bus.publish(UserRollback(target_turn=1, revert_changes=True))

    (error,) = events
    assert isinstance(error, Error)
    assert "peer warning 0 peer warning 1 peer warning 2 (+2 more)" in error.message
    assert "peer warning 3" not in error.message
    assert error.display_hint is not None
    assert "另有 2 个" in Localizer("zh-Hans").render(error.display_hint)


@pytest.mark.parametrize("target_turn", [0, 1])
@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param("space", marks=pytest.mark.skipif(sys.platform == "win32", reason="Win32 trims space-only names")),
        "hint-error",
        "truncated",
    ],
)
async def test_failed_rollback_reports_the_file_hint_and_retires_the_runtime(
    tmp_path: Path, workspace_case, monkeypatch: pytest.MonkeyPatch, caplog, target_turn: int, outcome: str
) -> None:
    """Main has no file-hint callback on restart failure; a failed welcome rebuild can omit the error event."""
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    await ask(engine, "second")
    sid = engine.session_id
    assert sid is not None
    selected = tmp_path / "selected.txt"
    retained = tmp_path / (" " if outcome == "space" else "retained.txt")
    tracker = engine.mutation_tracker
    tracker.start_turn(2)
    if outcome == "truncated":
        tracker.mark_detection_truncated()
    else:
        for path in (selected, retained):
            _record_file_edit(tracker, path)

    async def fail_build(*args, **kwargs):
        raise RuntimeError("restore build failed")

    during_hint = []
    during_error = []

    async def observe_error(error):
        assert engine.session_id is None and engine.current.loaded is None
        assert not await engine.writer.save_current_session()
        during_error.append(error)

    await engine.event_bus.subscribe(Error, observe_error)

    def fail_hint(*args, **kwargs):
        during_hint.append(True)
        assert engine.session_id is None and engine.current.loaded is None
        raise ValueError("bad file hint")

    monkeypatch.setattr(engine.loader, "build", fail_build)
    if outcome == "hint-error":
        monkeypatch.setattr(rollback_module, "rollback_file_result_messages", fail_hint, raising=False)

    async with capture_event_sequence(engine.event_bus, Error, RollbackResult) as events:
        await engine.event_bus.publish(
            UserRollback(target_turn=target_turn, revert_changes=True, selected_paths=[str(selected)])
        )

    (error,) = events
    assert during_error == [error]
    assert isinstance(error, Error) and error.code == "session_restore_failed"
    assert engine.session_id is None and engine.current.loaded is None
    assert not await engine.writer.save_current_session()
    saved = await workspace_case.store.load_session(sid)
    if target_turn == 0:
        assert saved is None
    else:
        assert saved is not None and saved["turn_counter"] == target_turn
        assert [message.text for message in saved["messages"] if message.role == "user"] == ["first"]

    if outcome != "truncated":
        assert selected.read_text(encoding="utf-8") == "before"
        assert retained.read_text(encoding="utf-8") == "after"
    if outcome == "space":
        assert '1 file left unchanged: " "' in error.message
    elif outcome == "hint-error":
        assert during_hint == [True] and "bad file hint" in caplog.text
        assert error.message == "restore build failed" and error.display_hint is None
        assert "Failed to build the restart failure hint" in caplog.text
    else:
        assert "Some file changes from the discarded turns could not be detected." in error.message
        assert error.display_hint is not None
        assert "未能检测到撤回轮次中的全部文件变更。" in Localizer("zh-Hans").render(error.display_hint)


async def test_rollback_publishes_restored_session_before_a_failing_restore_hook(
    tmp_path: Path, workspace_case, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    await ask(engine, "second")
    sid = engine.session_id
    hook_events: list[object] = []

    async with capture_event_sequence(engine.event_bus, SessionRestored, Warning, RollbackResult, Error) as events:

        async def fail_hook(**kwargs):
            hook_events.extend(events)
            raise RuntimeError("session restored hook failed")

        monkeypatch.setattr(engine.lifecycle, "_fire_session_restored_hook", fail_hook)
        await engine.event_bus.publish(UserRollback(target_turn=1), raise_handler_errors=True)

    restored, warning, result = events
    assert isinstance(restored, SessionRestored)
    assert restored.session_id == sid
    assert hook_events == [restored]
    assert isinstance(warning, Warning)
    assert (warning.code, warning.session_id) == ("session_restored_hook_failed", sid)
    assert "session restored hook failed" in warning.message
    assert isinstance(result, RollbackResult)
    assert engine.session_id == sid
    assert engine.current.loaded is not None
    assert engine.session.turn_number == 1
    assert [message.text for message in engine.history_messages if message.role == "user"] == ["first"]


async def test_refused_welcome_deletion_keeps_background_commands_running(tmp_path, workspace_case, monkeypatch):
    """Main stops the command before attempting the failed deletion."""
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    await ask(engine, "second")
    sid = engine.session_id
    tasks = engine.session.background_tasks
    assert sid is not None and tasks is not None
    directory = workspace_case.store.session_dir(sid)

    def refuse_delete(_directory: Path) -> None:
        raise OSError("session file is busy")

    monkeypatch.setattr(lifecycle_module, "_delete_reset_session_files", refuse_delete)
    monkeypatch.setattr(background_crash, "_members", lambda _pid: ())
    process = HeldProcess()
    task = tasks.adopt(process, command="build", trigger="timeout")
    assert task is not None
    before = (directory / "session.json").read_bytes()

    async with capture_event_sequence(engine.event_bus, Error, RollbackResult) as events:
        await engine.event_bus.publish(UserRollback(target_turn=0), raise_handler_errors=True)

    assert process.signals == []
    assert tasks.running == [task]
    (error,) = events
    assert isinstance(error, Error) and error.code == "rollback_reset_failed"
    assert engine.session.turn_number == 2
    assert (directory / "session.json").read_bytes() == before


async def test_welcome_without_primary_directory_discards_only_the_chat(tmp_path, workspace_case):
    """Main permits a conversation-only reset after the named primary folder is moved."""
    folder = tmp_path / "project"
    folder.mkdir()
    engine, _ = await workspace_case.start_selected(folder)
    await ask(engine, "first")
    sid = engine.session_id
    target = folder / "output.txt"
    engine.mutation_tracker.start_turn(1)
    _record_file_edit(engine.mutation_tracker, target)
    moved = folder.rename(tmp_path / "moved")
    async with capture_event_sequence(engine.event_bus, RollbackResult, Error) as events:
        await engine.event_bus.publish(UserRollback(target_turn=0, revert_changes=False), raise_handler_errors=True)
    (result,) = events
    assert isinstance(result, RollbackResult) and result.files_reverted == 0
    assert (moved / target.name).read_text(encoding="utf-8") == "after" and not folder.exists()
    await _check_save_and_cold_open(engine, workspace_case, tmp_path, sid, [])


def _record_file_edit(tracker: MutationTracker, path: Path) -> None:
    path.write_text("before", encoding="utf-8")
    mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, path.name)
    assert mutation is not None
    path.write_text("after", encoding="utf-8")
    tracker.record_after(mutation)


@pytest.mark.parametrize("target_turn", [0, 1])
@pytest.mark.parametrize("revert_changes", [False, True])
async def test_rollback_stops_background_commands_after_checkpoint_change_and_before_file_restore(
    tmp_path: Path, workspace_case, monkeypatch: pytest.MonkeyPatch, target_turn: int, revert_changes: bool
) -> None:
    """Main stops these jobs before changing the checkpoint; both versions await their actual exit."""
    engine = await workspace_case(tmp_path)
    await ask(engine, "first")
    sid = engine.session_id
    tasks = engine.session.background_tasks
    assert sid is not None and tasks is not None
    monkeypatch.setattr(background_crash, "_members", lambda _pid: ())
    preserved = HeldProcess()
    kept = tasks.adopt(preserved, command="serve", trigger="timeout")
    assert kept is not None
    await ask(engine, "second")
    await ask(engine, "third")
    turn_ids = [
        record.turn_id for record in turn_records(engine.current.require_loaded().bindings.backend.history_state)
    ]
    assert len(set(turn_ids)) == 3
    primary = workspace_case.store.session_dir(sid) / "session.json"
    target = tmp_path / "output.txt"
    tracker = engine.mutation_tracker
    tracker.start_turn(2)
    _record_file_edit(tracker, target)
    process = HeldProcess(ignores=(False,))
    terminate = process.terminate
    at_stop = []
    terminated = asyncio.Event()

    async def observe_stop(*, force: bool) -> None:
        saved_turn = json.loads(primary.read_bytes())["state"]["turn_counter"] if primary.exists() else 0
        at_stop.append((saved_turn, target.read_text(encoding="utf-8")))
        await terminate(force=force)
        terminated.set()

    monkeypatch.setattr(process, "terminate", observe_stop)
    task = tasks.adopt(process, command="build", trigger="timeout")
    assert task is not None
    async with capture_event_sequence(engine.event_bus, Error, RollbackResult) as events:
        rollback = asyncio.create_task(
            engine.event_bus.publish(
                UserRollback(target_turn=target_turn, revert_changes=revert_changes), raise_handler_errors=True
            )
        )
        try:
            await asyncio.wait_for(terminated.wait(), timeout=ENGINE_TURN_TIMEOUT)
            assert process.signals == [False] and not rollback.done() and not events
            assert target.read_text(encoding="utf-8") == "after"
        finally:
            process.exit(-15)
            await rollback

    (result,) = events
    assert isinstance(result, RollbackResult) and result.target_turn == target_turn
    assert process.signals == [False]
    assert target.read_text(encoding="utf-8") == ("before" if revert_changes else "after")
    assert result.files_reverted == int(revert_changes)
    assert preserved.signals == ([] if target_turn else [False])
    directory = workspace_case.store.session_dir(sid)
    assert len(_rollback_records(directory)) == 1
    assert result.superseded_turn_ids == turn_ids[target_turn:]
    assert [
        record.turn_id for record in turn_records(engine.current.require_loaded().bindings.backend.history_state)
    ] == turn_ids[:target_turn]
    assert (directory / "session.json.bak").exists() == bool(target_turn)
    assert all(
        rollback_module.snapshot_target_turn(path) <= target_turn for path in (directory / "snapshots").glob("*.json")
    )
    await _check_save_and_cold_open(engine, workspace_case, tmp_path, sid, ["first"][:target_turn])
    await ask(engine, "new turn")
    assert engine.session.turn_number == target_turn + 1
    assert at_stop == [(target_turn, "after")]


@pytest.mark.parametrize(("named", "cancel_count"), [(False, 1), (True, 2)], ids=["unspecified", "named"])
async def test_committed_rollback_restores_before_propagating_cancellation(
    tmp_path: Path, workspace_case, named: bool, cancel_count: int
) -> None:
    workspace = workspace_case.registry.create("Named", str(tmp_path)) if named else None
    client = SpecClient([answer("one"), answer("two")])
    engine = await workspace_case(tmp_path, workspace_id=workspace.id if workspace else None, client=client)
    await ask(engine, "first")
    await ask(engine, "second")
    sid = engine.session_id
    assert sid is not None
    store = workspace_case.store
    restore = workspace_case.gate(store, "load_chat_for_restore")
    results = await capture_events(engine.event_bus, RollbackResult)

    task = asyncio.create_task(
        engine.event_bus.publish(UserRollback(target_turn=1, revert_changes=False), raise_handler_errors=True)
    )
    try:
        await asyncio.wait_for(restore.entered.wait(), timeout=ENGINE_TURN_TIMEOUT)
        disk = await store.load_session(sid)
        assert disk is not None and disk["turn_counter"] == 1
        for _ in range(cancel_count):
            assert task.cancel()
            await asyncio.sleep(0)
        finished_while_blocked = task.done()
        assert engine.turns.turn_state.lease.prompt_admission_closed is (not finished_while_blocked)
        restore.open()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)
    finally:
        restore.open()
        await asyncio.gather(task, return_exceptions=True)

    assert await engine.writer.save_current_session()
    disk = await store.load_session(sid)
    assert disk is not None and disk["turn_counter"] == 1
    assert [message.text for message in disk["messages"] if message.role == "user"] == ["first"]
    assert [result.target_turn for result in results] == [1]
    assert not finished_while_blocked
    assert not engine.turns.turn_state.lease.prompt_admission_closed
    assert engine.session.workspace_id == (workspace.id if workspace else None)


async def test_committed_welcome_rollback_finishes_after_cancellation(tmp_path: Path, workspace_case) -> None:
    client = SpecClient([answer("one"), answer("two")])
    engine = await workspace_case(tmp_path, client=client)
    await ask(engine, "first")
    await ask(engine, "second")
    sid = engine.session_id
    assert sid is not None
    primary = workspace_case.store.session_dir(sid) / "session.json"
    restart = workspace_case.gate(engine.lifecycle, "_reconcile_existing_spill_storage")
    results = await capture_events(engine.event_bus, RollbackResult)

    task = asyncio.create_task(engine.event_bus.publish(UserRollback(target_turn=0), raise_handler_errors=True))
    try:
        await asyncio.wait_for(restart.entered.wait(), timeout=ENGINE_TURN_TIMEOUT)
        assert not primary.exists()
        assert task.cancel()
        await asyncio.sleep(0)
        finished_while_blocked = task.done()
        restart.open()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=ENGINE_TURN_TIMEOUT)
    finally:
        restart.open()
        await asyncio.gather(task, return_exceptions=True)

    assert engine.session_id == sid
    assert engine.current.loaded is not None
    assert engine.history_messages == []
    await engine.writer.save_current_session()
    assert not primary.exists()
    assert [result.target_turn for result in results] == [0]
    assert not finished_while_blocked


# ===========================================================================
# Suppress-save gate
# ===========================================================================


class TestSuppressSave:
    async def test_save_is_gated_by_suppress_flag(self, tmp_path: Path, *, engine_services) -> None:
        """Unit: flag flip alone gates ``_save_current_session``."""
        engine = _make_engine(tmp_path, engine_services=engine_services)

        install_loaded_agent(engine, bindings=_StubExec({"messages": []}))  # type: ignore[assignment]
        engine.session.suppress_save = True

        called: list[bool] = []

        async def _spy(**_kwargs: Any) -> None:
            called.append(True)

        engine_services(engine).persistence.save_session = _spy  # type: ignore[assignment]

        await engine.writer.save_current_session()
        assert called == []  # suppressed — no save call reached persistence


# ===========================================================================
# _on_user_rollback — snapshot swap + reload
# ===========================================================================


class TestRollbackSwap:
    async def test_rollback_swap_holds_suppress_save_around_reload(self, tmp_path: Path, *, engine_services) -> None:
        """Regression: the ``target_turn >= 1`` swap must keep
        ``_suppress_save`` True across ``on_session_restore``.

        The swap writes ``session.json`` from the turn snapshot, then
        reloads the engine.  If any intermediate step calls
        ``_save_current_session`` without the flag set, the in-memory
        pre-rollback state clobbers the just-swapped snapshot.  This
        test stubs out ``on_session_restore`` so we can observe the
        flag value at the point the reload would run, without dragging
        the full restore flow into the test.
        """
        engine = _make_engine(tmp_path, engine_services=engine_services)
        # Bind non-empty history so ``_available_rollback_turns`` adds
        # the welcome target (0); without it the guardrail returns
        # ``[]`` and the swap branch is never reached.
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        # Set up a valid snapshot for target_turn=1 (keep 1 turn →
        # restore turn_2.json) so the handler reaches the swap+reload
        # branch.
        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        _write_session_json(session_file)
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()  # creates turn_2.json

        observed: dict[str, Any] = {}

        async def _fake_restore(event: Any, *, for_rollback: bool = False) -> None:
            observed["suppress_during_restore"] = engine.session.suppress_save
            observed["session_id"] = event.session_id

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        # Count any stray save calls that leak through during the swap.
        save_calls: list[bool] = []

        async def _spy(**_kwargs: Any) -> None:
            save_calls.append(engine.session.suppress_save)

        engine_services(engine).persistence.save_session = _spy  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert observed.get("suppress_during_restore") is True, (
            "``_suppress_save`` must be True while on_session_restore runs during swap"
        )
        assert observed.get("session_id") == "rb_test"
        # After the try/finally, the flag is reset.
        assert engine.session.suppress_save is False
        # Any save that leaked through during the swap window would have
        # had ``_suppress_save == True`` at observation time — i.e. it
        # was properly gated upstream.  No ungated saves should occur.
        assert all(v is True for v in save_calls)

    @pytest.mark.parametrize("revert_changes", [False, True])
    async def test_rollback_workspace_baseline_policy_after_restore(
        self, tmp_path: Path, revert_changes: bool, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind({"messages": [Message("user", ["hello"])], "compressed_msgs": []})
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(workspace_root))))
        tracker = engine_services(engine).workspace_change_tracker
        tracker.apply_retarget(tracker.resolve_retarget(engine.session.workspace))
        original_baseline = tracker.capture_baseline(1)

        session_dir = tmp_path / "rb_test"
        _write_session_json(session_dir / "session.json")
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()
        save_payloads: list[dict[str, Any] | None] = []

        async def _fake_restore(_event: Any, *, for_rollback: bool = False) -> None:
            assert engine.session.suppress_save is True

        async def _fake_save(*, raise_on_error: bool = False) -> bool:
            assert raise_on_error is False
            save_payloads.append(tracker.serialize())
            return True

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]
        engine.writer.save_current_session = _fake_save  # type: ignore[method-assign]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=revert_changes))

        if revert_changes:
            assert tracker.baseline is None
            assert save_payloads == [None]
        else:
            assert tracker.baseline == original_baseline
            assert save_payloads == []

    async def test_rollback_waits_for_active_capture_and_save_before_commit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = await seed_turns(engine, 3, engine_services=engine_services)

        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(workspace_root))))
        tracker = engine_services(engine).workspace_change_tracker
        tracker.apply_retarget(tracker.resolve_retarget(engine.session.workspace))

        entered = threading.Event()
        release = threading.Event()
        original_capture = workspace_changes._capture_workspace

        def _blocked(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            assert release.wait(timeout=10.0)
            return original_capture(*args, **kwargs)

        monkeypatch.setattr(workspace_changes, "_capture_workspace", _blocked)

        order: list[str] = []
        generation_after_finalization: list[int] = []

        async def _finalization() -> None:
            await asyncio.to_thread(tracker.capture_baseline, 3)
            order.append("capture")
            order.append("save")
            generation_after_finalization.append(engine.session_generation)

        async def _fake_restore(event: Any, *, for_rollback: bool = False) -> None:
            order.append("restore")
            loaded = await store.load_session(event.session_id)
            assert loaded is not None
            engine_services(engine).history.bind(loaded)
            engine.session.turn_number = loaded.get("turn_counter", 0)

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]
        lifecycle_task = asyncio.create_task(_finalization())
        engine.turns.turn_state.lease.run_task = lifecycle_task

        rollback_task = asyncio.create_task(engine._on_user_rollback(UserRollback(target_turn=1, session_id="rb_test")))
        await asyncio.to_thread(entered.wait, 5.0)
        await asyncio.sleep(0.05)
        assert order == []
        assert engine.session_generation == 0

        release.set()
        await asyncio.wait_for(rollback_task, timeout=10)
        await asyncio.wait_for(lifecycle_task, timeout=10)

        assert order == ["capture", "save", "restore"]
        assert generation_after_finalization == [0]
        assert engine.session_generation == 1

    async def test_rollback_result_carries_first_discarded_user_prompt(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = await seed_turns(engine, 3, engine_services=engine_services)

        results: list[RollbackResult] = []
        await _collect_events(engine.event_bus, RollbackResult, results)

        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert len(results) == 1
        assert results[0].rolled_back_user_text == "user 2"

    async def test_rollback_swap_updates_backup_for_recovery(self, tmp_path: Path, *, engine_services) -> None:
        """After rollback, backup recovery must not resurrect the pre-rollback state."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        rolled_back_state = {"messages": [Message("user", ["rolled back"])], "compressed_msgs": []}
        pre_rollback_state = {"messages": [Message("user", ["pre rollback"])], "compressed_msgs": []}

        await store.save_session("rb_test", rolled_back_state)
        session_dir = tmp_path / "rb_test"
        snapshot_payload = (session_dir / "session.json").read_text(encoding="utf-8")
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "turn_2.json").write_text(snapshot_payload, encoding="utf-8")

        await store.save_session("rb_test", pre_rollback_state)

        async def _fake_restore(_event: Any, *, for_rollback: bool = False) -> None: ...

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        (session_dir / "session.json").write_text("{ broken primary", encoding="utf-8")
        loaded = await store.load_session("rb_test")

        assert loaded is not None
        assert loaded["messages"][0].text == "rolled back"

    async def test_a_rollback_keeps_the_surface_the_session_last_worked_in(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """A rollback is not a turn: the snapshot's older surface never replaces the session's last one."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        rolled_back_state = {"messages": [Message("user", ["rolled back"])], "compressed_msgs": []}
        pre_rollback_state = {"messages": [Message("user", ["pre rollback"])], "compressed_msgs": []}
        await store.save_session("rb_test", rolled_back_state, last_surface=SessionSurface.CLI)
        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "turn_2.json").write_text((session_dir / "session.json").read_text(encoding="utf-8"), "utf-8")
        await store.save_session("rb_test", pre_rollback_state, last_surface=SessionSurface.ACP)

        async def _fake_restore(_event: Any, *, for_rollback: bool = False) -> None: ...

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        for name in ("session.json", "session.json.bak"):
            envelope = json.loads((session_dir / name).read_text(encoding="utf-8"))
            assert envelope["meta"]["last_surface"] == "acp", name
        loaded = await store.load_session("rb_test")
        assert loaded is not None
        assert loaded["messages"][0].text == "rolled back"

    async def test_a_rollback_keeps_the_surface_a_crashed_turn_recorded_in_its_sidecar(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """Publication must capture the last surface before discarding its only record in the sidecar."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        rolled_back = [Message("user", ["rolled back"])]
        pre_rollback = [*rolled_back, Message("user", ["more"])]
        await store.save_session(
            "rb_test", {"messages": rolled_back, "compressed_msgs": []}, last_surface=SessionSurface.TUI
        )
        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "turn_2.json").write_text((session_dir / "session.json").read_text(encoding="utf-8"), "utf-8")
        await store.save_session(
            "rb_test", {"messages": pre_rollback, "compressed_msgs": []}, last_surface=SessionSurface.CLI
        )
        store.save_recovery_session(
            "rb_test",
            {"messages": [*pre_rollback, Message("user", ["cut short"])], "compressed_msgs": []},
            last_surface=SessionSurface.ACP,
        )
        meta = await store.load_session_meta("rb_test", prefer_recovery=True)
        assert meta is not None and meta.last_surface == SessionSurface.ACP

        async def _restore_ignoring_recovery(event: Any, *, for_rollback: bool = False) -> None:
            assert event.ignore_recovery is True
            assert not (session_dir / "session.recovery.json").exists()

        engine.lifecycle.on_session_restore = _restore_ignoring_recovery  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert not (session_dir / "session.recovery.json").exists()
        meta = await store.load_session_meta("rb_test", prefer_recovery=True)
        assert meta is not None
        assert meta.last_surface == SessionSurface.ACP
        loaded = await store.load_session("rb_test")
        assert loaded is not None
        assert _prompts(loaded) == ["rolled back"]
        assert loaded["messages"][-1].additional_properties["_turn"] == 1

    async def test_a_cancel_on_the_audit_record_still_restores_the_swapped_session(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """The swap has committed by then; abandoning the restore would undo it on the next save."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        rolled_back_state = {"messages": [Message("user", ["rolled back"])], "compressed_msgs": []}
        pre_rollback_state = {"messages": [Message("user", ["pre rollback"])], "compressed_msgs": []}
        await store.save_session("rb_test", rolled_back_state)
        session_dir = tmp_path / "rb_test"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "turn_2.json").write_text((session_dir / "session.json").read_text(encoding="utf-8"), "utf-8")
        await store.save_session("rb_test", pre_rollback_state)

        async def _fake_restore(event: Any, *, for_rollback: bool = False) -> None:
            loaded = await store.load_session(event.session_id)
            assert loaded is not None
            engine_services(engine).history.bind(loaded)

        async def _cancelled_record(*, superseded_turns: list[Any], target_turns: list[Any]) -> None:
            _ = superseded_turns, target_turns
            raise asyncio.CancelledError

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]
        engine_services(engine).trajectory_recorder.rollback = _cancelled_record  # type: ignore[assignment]

        with pytest.raises(asyncio.CancelledError):
            await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        # The engine holds the restored history, so the next save writes that
        # rather than putting the superseded conversation back on disk.
        assert engine_services(engine).history.state["messages"][0].text == "rolled back"

    async def test_rollback_restore_round_trips_runtime_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        """Rollback reload path restores runtime metadata from the promoted snapshot."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine.session.agent_profile = AgentProfile(name="Code")
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        rolled_back_state = {
            "messages": [Message("user", ["rolled back"])],
            "compressed_msgs": [],
            "last_usage": {"total_token_count": 77, "calibration_ratio": 1.4},
            "total_session_tokens": 77,
            "total_session_input_tokens": 33,
            "total_session_output_tokens": 44,
        }
        pre_rollback_state = {
            "messages": [Message("user", ["pre rollback"])],
            "compressed_msgs": [],
            "last_usage": {"total_token_count": 12},
            "total_session_tokens": 12,
            "total_session_input_tokens": 5,
            "total_session_output_tokens": 7,
        }

        await store.save_session("rb_test", rolled_back_state, agent_profile="Code")
        session_dir = tmp_path / "rb_test"
        snapshot_payload = (session_dir / "session.json").read_text(encoding="utf-8")
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "turn_2.json").write_text(snapshot_payload, encoding="utf-8")

        await store.save_session("rb_test", pre_rollback_state, agent_profile="Code")
        original_meta = engine.session.runtime_meta

        monkeypatch.setattr(engine.lifecycle, "start", fake_start_factory(engine))

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert engine.session.runtime_meta is not original_meta
        assert engine.session.runtime_meta.total_session_tokens == 77
        assert engine.session.runtime_meta.total_session_input_tokens == 33
        assert engine.session.runtime_meta.total_session_output_tokens == 44
        assert engine.session.runtime_meta.last_usage_details == {"total_token_count": 77, "calibration_ratio": 1.4}

    async def test_rollback_restores_target_turn_todo_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        """Rolling back to turn N rehydrates the todo tracker from turn N's
        snapshot, replacing the pre-rollback list."""
        from chrys.service.todos.tracker import TodoTracker

        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine.session.agent_profile = AgentProfile(name="Code")
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        turn_one_todos = [{"content": "turn one task", "status": "pending", "active_form": ""}]
        turn_three_todos = [
            {"content": "turn one task", "status": "completed", "active_form": ""},
            {"content": "turn three task", "status": "in_progress", "active_form": "Working"},
        ]
        rolled_back_state = {
            "messages": [Message("user", ["rolled back"])],
            "compressed_msgs": [],
            "chrys_todos": turn_one_todos,
        }
        pre_rollback_state = {
            "messages": [Message("user", ["pre rollback"])],
            "compressed_msgs": [],
            "chrys_todos": turn_three_todos,
        }

        await store.save_session("rb_test", rolled_back_state, agent_profile="Code")
        session_dir = tmp_path / "rb_test"
        snapshot_payload = (session_dir / "session.json").read_text(encoding="utf-8")
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "turn_2.json").write_text(snapshot_payload, encoding="utf-8")

        await store.save_session("rb_test", pre_rollback_state, agent_profile="Code")
        engine.session.todo_tracker = TodoTracker()
        await engine.session.todo_tracker.restore(turn_three_todos)

        monkeypatch.setattr(engine.lifecycle, "start", fake_start_factory(engine))

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        tracker = engine.todo_tracker
        assert tracker is not None
        assert tracker.serialize() == turn_one_todos
        assert engine.current.loaded.bindings.backend.history_state["chrys_todos"] == turn_one_todos

    async def test_rollback_swap_keeps_promoted_snapshot_when_backup_update_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        """If backup refresh fails, the promoted snapshot remains as recovery fallback."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        rolled_back_state = {"messages": [Message("user", ["rolled back"])], "compressed_msgs": []}
        pre_rollback_state = {"messages": [Message("user", ["pre rollback"])], "compressed_msgs": []}

        await store.save_session("rb_test", rolled_back_state)
        session_dir = tmp_path / "rb_test"
        snapshot_payload = (session_dir / "session.json").read_text(encoding="utf-8")
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        promoted_snapshot = snap_dir / "turn_2.json"
        promoted_snapshot.write_text(snapshot_payload, encoding="utf-8")

        await store.save_session("rb_test", pre_rollback_state)

        real_replace = platform_files.replace_with_retry

        def fail_backup_copy(source: Path, dest: Path) -> None:
            if dest.name == "session.json.bak":
                raise OSError("simulated backup failure")
            real_replace(source, dest)

        async def _fake_restore(_event: Any, *, for_rollback: bool = False) -> None: ...

        monkeypatch.setattr(platform_files, "replace_with_retry", fail_backup_copy)
        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert promoted_snapshot.exists()
        assert not (session_dir / "session.json.bak").exists()

        (session_dir / "session.json").write_text("{ broken primary", encoding="utf-8")
        loaded = await store.load_session("rb_test")

        assert loaded is not None
        assert loaded["messages"][0].text == "rolled back"

    async def test_rollback_swap_reports_locked_write_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        """A session write-lock timeout should surface an Error instead of racing the swap."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        _write_session_json(session_file)
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()
        generation = engine.session_generation
        engine.turns.turn_state.lease.injection_admission_open = True

        errors: list[Error] = []
        await _collect_events(engine.event_bus, Error, errors)

        class _TimedOutFileLock:
            def __init__(self, _path: Path | str, timeout: float | None = None) -> None:
                _ = timeout

            def __enter__(self) -> _TimedOutFileLock:
                raise TimeoutError("lock busy")

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                return None

        monkeypatch.setattr(rollback_module, "FileLock", _TimedOutFileLock)

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        await asyncio.sleep(0)
        error = next(error for error in errors if error.code == "rollback_swap_locked")
        assert error.message == "Timed out waiting for session lock: lock busy"
        assert_display_message(error, "rollback.swap_locked", {"detail": DisplayBlock("lock busy")})
        assert engine.session_generation == generation
        assert engine.turns.turn_state.lease.injection_admission_open is True
        assert engine.turns.turn_state.lease.prompt_admission_closed is False

    async def test_snapshot_removed_after_target_validation_reports_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        _write_session_json(session_file)
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()
        snapshot_path = session_dir / "snapshots" / "turn_2.json"
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        def _remove_snapshot_after_validation(_target_turn: int) -> str:
            snapshot_path.unlink()
            return "hello"

        monkeypatch.setattr(engine.rollback, "first_rolled_back_user_text", _remove_snapshot_after_validation)

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert len(warnings) == 1
        assert warnings[0].message == "Snapshot for turn 1 is missing."
        assert_display_message(warnings[0], "rollback.snapshot_missing", {"target_turn": 1})

    async def test_rollback_swap_lock_failure_does_not_revert_workspace_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        """File restores must wait until the session snapshot swap succeeds."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        _write_session_json(session_file)
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()

        workspace_file = tmp_path / "work.txt"
        workspace_file.write_text("original", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(session_dir))
        tracker.start_turn(2)
        mutation = tracker.record(str(workspace_file), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert mutation is not None
        workspace_file.write_text("changed", encoding="utf-8")
        tracker.record_after(mutation)
        engine.session.mutation_tracker = tracker

        errors: list[Error] = []
        results: list[RollbackResult] = []
        await _collect_events(engine.event_bus, Error, errors)
        await _collect_events(engine.event_bus, RollbackResult, results)
        monkeypatch.setattr(engine_module, "SESSION_WRITE_LOCK_TIMEOUT_SECONDS", 0.01)

        lock_path = engine.session.session_write_lock_path("rb_test")
        assert lock_path is not None
        held = FileLock(lock_path, timeout=1.0)
        held.acquire()
        try:
            await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=True))
        finally:
            held.release()

        assert [e.code for e in errors] == ["rollback_swap_locked"]
        assert results == []
        assert workspace_file.read_text(encoding="utf-8") == "changed"
        assert [turn.turn_id for turn in tracker.get_all_turns()] == [2]
        assert engine.session_generation == 0

    async def test_rollback_reverts_workspace_files_after_successful_swap(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """A successful target-turn swap still applies requested file restores."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        _write_session_json(session_file)
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()

        workspace_file = tmp_path / "work.txt"
        workspace_file.write_text("original", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(session_dir))
        tracker.start_turn(2)
        mutation = tracker.record(str(workspace_file), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert mutation is not None
        workspace_file.write_text("changed", encoding="utf-8")
        tracker.record_after(mutation)
        engine.session.mutation_tracker = tracker

        results: list[RollbackResult] = []
        await _collect_events(engine.event_bus, RollbackResult, results)

        async def _fake_restore(_event: Any, *, for_rollback: bool = False) -> None: ...

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=True))

        assert workspace_file.read_text(encoding="utf-8") == "original"
        assert [turn.turn_id for turn in tracker.get_all_turns()] == []
        assert len(results) == 1
        assert engine.session_generation == 1
        assert results[0].files_reverted == 1


class TestRollbackSwapSemantics:
    """End-to-end: rolling back to a compressed turn un-compresses it.

    Snapshots are written at the *start* of each turn, so a snapshot
    taken before any compression ran contains ``compressed_msgs == []``
    even if the live state later folded that turn into a block.
    Swapping that snapshot back in as ``session.json`` therefore drops
    the compressed blocks — which is exactly what powers the Context
    panel rebuild on ``SessionRestored``.
    """

    async def test_rollback_to_pre_compress_snapshot_clears_blocks(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        # Live history anchors the welcome target so the picker also
        # offers target_turn=1 (keep one turn = restore turn_2.json).
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hi"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"

        # 1. Pre-compress session.json → snapshot turn_2.json.
        _write_session_json(session_file)  # compressed_msgs == []
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()

        # 2. Simulate compression happening later: rewrite session.json
        #    with a CompressedBlock folded into the live state.
        post_compress = {
            "meta": {"session_id": "rb_test", "agent_profile": "p"},
            "state": {
                "messages": [],
                "compressed_msgs": [
                    {
                        "compressed_context_id": "ctx_x",
                        "summary_text": "summary",
                        "marker_id": "turn_2",
                        "turn_range": [1, 2],
                        "messages": [],
                    }
                ],
            },
        }
        session_file.write_text(json.dumps(post_compress), encoding="utf-8")

        # Stub restore so the test focuses on the swap itself.
        async def _fake_restore(_event: Any, *, for_rollback: bool = False) -> None: ...

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        # target_turn=1 → restore turn_2.json (pre-compress state).
        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        # 3. session.json must now match the pre-compress snapshot.
        restored = json.loads(session_file.read_text(encoding="utf-8"))
        assert restored["state"]["compressed_msgs"] == []

    async def test_rollback_uses_snapshot_payload_turn_counter_for_legacy_snapshots(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """Legacy after-turn snapshots must not be interpreted as pre-turn snapshots.

        Older development builds named snapshots after the completed turn,
        so ``turn_2.json`` could contain ``turn_counter == 2``.  The newer
        pre-turn convention maps ``turn_2.json`` to target 1.  Restoring by
        filename alone would therefore make "rollback to Turn 1" restore a
        two-turn session and then delete the snapshot that proved it.
        """
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        session_dir = store.session_dir("rb_test")
        session_file = session_dir / "session.json"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)

        for turn in range(1, 4):
            await store.save_session("rb_test", _state_after_turns(turn))
            atomic_copy_file(session_file, snap_dir / f"turn_{turn}.json")

        await store.save_session("rb_test", _state_after_turns(3))
        live = await store.load_session("rb_test")
        assert live is not None
        engine_services(engine).history.bind(live)
        engine.session.turn_number = 3

        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]

        assert engine.available_rollback_turns() == [0, 1, 2]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        restored = await store.load_session("rb_test")
        assert restored is not None
        assert restored["turn_counter"] == 1
        assert [m.text for m in restored["messages"] if m.role == "user"] == ["user 1"]
        assert sorted(p.name for p in snap_dir.glob("*.json")) == ["turn_1.json"]

    async def test_chained_rollback_keeps_current_snapshot_without_offering_current_target(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """A rollback should keep the promoted snapshot for recovery but hide it from the picker."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = await seed_turns(engine, 6, engine_services=engine_services)
        snap_dir = store.session_dir("rb_test") / "snapshots"

        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]

        assert engine.available_rollback_turns() == [0, 1, 2, 3, 4, 5]

        await engine._on_user_rollback(UserRollback(target_turn=3, revert_changes=False))

        restored = await store.load_session("rb_test")
        assert restored is not None
        assert restored["turn_counter"] == 3
        assert [m.text for m in restored["messages"] if m.role == "user"] == ["user 1", "user 2", "user 3"]
        assert sorted(p.name for p in snap_dir.glob("*.json")) == [
            "turn_2.json",
            "turn_3.json",
            "turn_4.json",
        ]
        assert engine.available_rollback_turns() == [0, 1, 2]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        restored = await store.load_session("rb_test")
        assert restored is not None
        assert restored["turn_counter"] == 1
        assert [m.text for m in restored["messages"] if m.role == "user"] == ["user 1"]
        assert sorted(p.name for p in snap_dir.glob("*.json")) == ["turn_2.json"]
        assert engine.available_rollback_turns() == [0]


class TestRollbackSupersededTurns:
    """Which turns a rollback retires is decided by identity, never by ordinal.

    The registry is append-ordered and the snapshot holds an earlier state of
    it, so the superseded turns are the tail the snapshot does not name. After
    a failed or cancelled turn the running ordinal is ahead of the saved one,
    and the tail is exactly what subtraction by ordinal would get wrong.
    """

    async def test_the_snapshot_tail_is_superseded_by_id(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = engine_services(engine).persistence.state_store
        assert isinstance(store, JsonFileStateStore)
        session_dir = store.session_dir("rb_test")

        # Turn 1 opened at sequence 4, turn 2 at 9; the snapshot is taken when
        # turn 3 opens, so it names turns 1 and 2 and predates turn 3's entry.
        state = _state_after_turns(3)
        state[TRAJECTORY_STATE_KEY] = {
            "turn_log": [
                {"turn_number": 1, "turn_id": "turn-a", "started_sequence": 4},
                {"turn_number": 2, "turn_id": "turn-b", "started_sequence": 9},
                {"turn_number": 3, "turn_id": "turn-c", "started_sequence": 14},
            ]
        }
        await store.save_session("rb_test", state)
        engine_services(engine).history.bind(state)
        engine.session.turn_number = 3

        # Named for the turn it precedes, holding the two turns it keeps.
        snapshot_state = serialize_state(_state_after_turns(2))
        snapshot_state[TRAJECTORY_STATE_KEY] = {"turn_log": state[TRAJECTORY_STATE_KEY]["turn_log"][:2]}
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir()
        (snap_dir / "turn_2.json").write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": snapshot_state}), encoding="utf-8"
        )

        results: list[RollbackResult] = []
        await _collect_events(engine.event_bus, RollbackResult, results)
        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=2, revert_changes=False))

        assert len(results) == 1
        # Only turn 3 is retired: turn 2 survives, even though the aborted
        # attempt to run it left the ordinal where it was. The result's ids come
        # from the restored registry, not from the snapshot the range is built
        # off — the audit record the range lands in is pinned by the modules that
        # drive real turns (``run/test_turn_rollback_identity``).
        assert results[0].superseded_turn_ids == ["turn-c"]

    async def test_a_rollback_to_welcome_supersedes_every_registered_turn(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        state = _state_after_turns(2)
        state[TRAJECTORY_STATE_KEY] = {
            "turn_log": [
                {"turn_number": 1, "turn_id": "turn-a", "started_sequence": 4},
                {"turn_number": 2, "turn_id": "turn-b", "started_sequence": 9},
            ]
        }
        engine_services(engine).history.bind(state)
        engine.session.turn_number = 2
        # Supply the usual boundary snapshot for this two-turn session.
        snapshot_dir = tmp_path / "rb_test" / "snapshots"
        snapshot_dir.mkdir(parents=True)
        (snapshot_dir / "turn_2.json").write_text('{"state":{"turn_counter":1}}', encoding="utf-8")
        engine.lifecycle.reset_session_to_welcome = fake_reset(engine, engine_services=engine_services)  # type: ignore[assignment]

        results: list[RollbackResult] = []
        await _collect_events(engine.event_bus, RollbackResult, results)

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=False))

        assert len(results) == 1
        assert results[0].superseded_turn_ids == ["turn-a", "turn-b"]


async def test_peer_text_matching_a_file_warning_does_not_change_its_meaning(
    tmp_path, workspace_case, monkeypatch, engine_services
):
    """Main displays the peer text without the warning prefix and queues no model notice."""
    from unittest.mock import Mock

    from chrys.app.acp.server import _rollback_result_payload
    from chrys.app.tui.screens.main.ports import RollbackView
    from chrys.service.mutations.types import ROLLBACK_RESTORE_UNKNOWN
    from tests.app.tui.screens.main.test_rollback_controller import _make_rollback_controller

    engine = await workspace_case(tmp_path)
    for prompt in ("first", "second"):
        await ask(engine, prompt)
    tracker = engine.mutation_tracker
    tracker.start_turn(2)
    get_plan = tracker.get_rollback_plan_for_turns
    peer_text = ROLLBACK_RESTORE_UNKNOWN.fallback
    monkeypatch.setattr(
        tracker,
        "get_rollback_plan_for_turns",
        lambda turns: replace(get_plan(turns), warnings=[RollbackWarning("peer", peer_text)]),
    )
    async with capture_event_sequence(engine.event_bus, RollbackResult) as results:
        await engine.event_bus.publish(UserRollback(target_turn=1, revert_changes=True), raise_handler_errors=True)
    (result,) = results
    assert json.loads(json.dumps(_rollback_result_payload(result)))["warnings"] == [peer_text]
    view = Mock(spec=RollbackView)
    await _make_rollback_controller(view=view, workspace_cwd=lambda: str(tmp_path)).on_result(result)
    assert f"⚠ {peer_text}" in view.notify.call_args.args[0]
    assert engine_services(engine).workspace_change_tracker.take_pending_notice() is None
