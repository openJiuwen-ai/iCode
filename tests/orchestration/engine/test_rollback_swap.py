# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The snapshot swap + reload path of ``_on_user_rollback`` and the ``_suppress_save`` gate around it."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

import chrys.orchestration.engine.engine as engine_module
import chrys.orchestration.engine.rollback as rollback_module
from chrys.foundation.events.types import Error, RollbackResult, UserRollback, Warning
from chrys.foundation.i18n import DisplayBlock
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.mutations import workspace_changes
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.state.serializers import serialize_state
from chrys.service.state.store import JsonFileStateStore, atomic_copy_file
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY
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
from tests.support.event_capture import assert_display_message
from tests.support.loaded_agents import install_loaded_agent

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

        async def _fake_restore(event: Any) -> None:
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
        engine.session.workspace = Workspace.from_cwd(str(workspace_root))
        tracker = engine_services(engine).workspace_change_tracker
        tracker.retarget_roots(engine.session.workspace)
        original_baseline = tracker.capture_baseline(1)

        session_dir = tmp_path / "rb_test"
        _write_session_json(session_dir / "session.json")
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()
        save_payloads: list[dict[str, Any] | None] = []

        async def _fake_restore(_event: Any) -> None:
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
        engine.session.workspace = Workspace.from_cwd(str(workspace_root))
        tracker = engine_services(engine).workspace_change_tracker
        tracker.retarget_roots(engine.session.workspace)

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

        async def _fake_restore(event: Any) -> None:
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

        async def _fake_restore(_event: Any) -> None: ...

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

        async def _fake_restore(_event: Any) -> None: ...

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
        """The restore after the swap discards the sidecar, which may be the only record of the last surface."""
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

        async def _restore_ignoring_recovery(event: Any) -> None:
            assert event.ignore_recovery is True
            store.delete_recovery_session(event.session_id)

        engine.lifecycle.on_session_restore = _restore_ignoring_recovery  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert not (session_dir / "session.recovery.json").exists()
        meta = await store.load_session_meta("rb_test", prefer_recovery=True)
        assert meta is not None
        assert meta.last_surface == SessionSurface.ACP
        loaded = await store.load_session("rb_test")
        assert loaded is not None
        assert [message.text for message in loaded["messages"]] == ["rolled back"]

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

        async def _fake_restore(event: Any) -> None:
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

        real_atomic_copy_file = engine_module.atomic_copy_file

        def fail_backup_copy(source: Path, dest: Path) -> None:
            if dest.name == "session.json.bak":
                raise OSError("simulated backup failure")
            real_atomic_copy_file(source, dest)

        async def _fake_restore(_event: Any) -> None: ...

        monkeypatch.setattr(engine_module, "atomic_copy_file", fail_backup_copy)
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

    async def test_rollback_swap_reports_copy_failure(
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
        errors: list[Error] = []
        await _collect_events(engine.event_bus, Error, errors)

        def _fail_snapshot_copy(_source: Path, _destination: Path) -> None:
            raise OSError("snapshot copy failed")

        monkeypatch.setattr(engine_module, "atomic_copy_file", _fail_snapshot_copy)

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert len(errors) == 1
        assert errors[0].message == "Failed to restore snapshot: snapshot copy failed"
        assert_display_message(errors[0], "rollback.swap_failed", {"detail": DisplayBlock("snapshot copy failed")})

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

    async def test_snapshot_disappearing_after_validation_does_not_commit_transition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(
            {"messages": [Message(role="user", contents=["hello"])], "compressed_msgs": []}
        )

        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        _write_session_json(session_file)
        original_session = session_file.read_text(encoding="utf-8")
        engine.session.turn_number = 2
        engine.rollback.write_snapshot()
        snapshot_path = session_dir / "snapshots" / "turn_2.json"
        assert snapshot_path.exists()

        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)
        generation = engine.session_generation
        real_file_lock = rollback_module.FileLock

        class _DeletingFileLock:
            def __init__(self, path: Path | str, timeout: float | None = None) -> None:
                self._inner = real_file_lock(path, timeout=timeout)

            def __enter__(self) -> _DeletingFileLock:
                self._inner.acquire()
                snapshot_path.unlink()
                return self

            def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
                self._inner.release()

        monkeypatch.setattr(rollback_module, "FileLock", _DeletingFileLock)

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert [warning.code for warning in warnings] == ["rollback_snapshot_missing"]
        assert warnings[0].message == "Snapshot for turn 1 is missing."
        assert_display_message(warnings[0], "rollback.snapshot_missing", {"target_turn": 1})
        assert engine.session_generation == generation
        assert session_file.read_text(encoding="utf-8") == original_session
        assert engine.turns.turn_state.lease.prompt_admission_closed is False

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

        async def _fake_restore(_event: Any) -> None: ...

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
        async def _fake_restore(_event: Any) -> None: ...

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

    async def test_legacy_numeric_snapshot_without_counter_can_restore_turn_one(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """Bare ``1.json`` snapshots without metadata still mean legacy turn 1."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        session_dir = tmp_path / "rb_test"
        session_file = session_dir / "session.json"
        snap_dir = session_dir / "snapshots"
        snap_dir.mkdir(parents=True)

        (snap_dir / "1.json").write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": {"messages": ["legacy turn 1"]}}),
            encoding="utf-8",
        )
        session_file.write_text(
            json.dumps({"meta": {"session_id": "rb_test"}, "state": {"messages": ["current"]}}),
            encoding="utf-8",
        )
        engine_services(engine).history.bind({"messages": [Message("user", ["current"])], "compressed_msgs": []})
        engine.session.turn_number = 2

        async def _fake_restore(_event: Any) -> None: ...

        engine.lifecycle.on_session_restore = _fake_restore  # type: ignore[assignment]

        assert engine.available_rollback_turns() == [0, 1]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        restored = json.loads(session_file.read_text(encoding="utf-8"))
        assert restored["state"]["messages"] == ["legacy turn 1"]

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
