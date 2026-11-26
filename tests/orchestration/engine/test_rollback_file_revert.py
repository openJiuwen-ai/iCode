# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Welcome-reset and workspace file-revert notices raised by ``_on_user_rollback``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import chrys.orchestration.engine.engine as engine_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, RollbackResult, UserRollback
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.util.lock import FileLock
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.state.machine import Trigger
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine._rollback_helpers import (
    _collect_events,
    _FakeMutationTracker,
    _make_engine,
    _state_after_turns,
    _write_session_json,
    fake_reset,
    fake_restore_factory,
    seed_turns,
)
from tests.support.components import plan_workspace
from tests.support.event_capture import assert_display_message, collect_events

# ===========================================================================
# _on_user_rollback — welcome reset and workspace file revert notices
# ===========================================================================


class TestRollbackFileRevert:
    async def test_welcome_rollback_result_carries_first_discarded_user_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(2))

        results: list[RollbackResult] = []
        await _collect_events(engine.event_bus, RollbackResult, results)

        lifecycle: list[str] = []

        async def _fake_reset(
            session_id: str,
            *,
            write_lock_held: bool = False,
            after_delete: Any = None,
            before_restart: Any = None,
            failure_hint: Any = None,
        ) -> bool:
            _ = write_lock_held, failure_hint
            assert after_delete is not None
            assert before_restart is not None
            await after_delete()
            lifecycle.append(f"reset:{session_id}")
            engine_services(engine).history.bind({"messages": [], "compressed_msgs": [], "turn_counter": 0})
            engine.session.turn_number = 0
            return True

        async def _fake_trajectory_rollback(*, superseded_turns: list[Any], target_turns: list[Any]) -> None:
            # The reset replaced the state with an empty one, and the bound
            # state carried no registry to begin with, so the session is Unspecified.
            assert superseded_turns == []
            assert target_turns == []
            lifecycle.append("trajectory.rollback")

        engine.lifecycle.reset_session_to_welcome = _fake_reset  # type: ignore[assignment]
        monkeypatch.setattr(engine_services(engine).trajectory_recorder, "rollback", _fake_trajectory_rollback)

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=False))

        assert lifecycle == ["reset:rb_test", "trajectory.rollback"]
        assert len(results) == 1
        assert results[0].rolled_back_user_text == "user 1"
        assert engine.session_generation == 1

    async def test_welcome_conversation_rollback_queues_retained_files_once(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        engine_services(engine).history.bind(_state_after_turns(1))
        target = tmp_path / "retained.txt"
        target.write_text("before", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(1)
        mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
        assert mutation is not None
        target.write_text("after", encoding="utf-8")
        tracker.record_after(mutation)
        engine.session.mutation_tracker = tracker

        engine.lifecycle.reset_session_to_welcome = fake_reset(engine, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=False))

        notice = engine_services(engine).workspace_change_tracker.take_pending_notice()
        assert notice is not None
        assert notice.startswith("Files retained from the discarded conversation:")
        assert 'modified: "retained.txt"' in notice
        assert engine_services(engine).workspace_change_tracker.take_pending_notice() is None

    async def test_welcome_file_rollback_queues_no_retained_notice(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(1))

        engine.lifecycle.reset_session_to_welcome = fake_reset(engine, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True))

        assert engine_services(engine).workspace_change_tracker.take_pending_notice() is None

    @staticmethod
    def _record_modify(tracker: MutationTracker, path: Path) -> None:
        mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
        assert mutation is not None
        path.write_text(f"after-{path.stem}", encoding="utf-8")
        tracker.record_after(mutation)

    async def test_welcome_partial_file_rollback_reports_unselected_paths(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        engine_services(engine).history.bind(_state_after_turns(1))
        selected = tmp_path / "selected.txt"
        selected.write_text("before-selected", encoding="utf-8")
        skipped = tmp_path / "skipped.txt"
        skipped.write_text("before-skipped", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(1)
        self._record_modify(tracker, selected)
        self._record_modify(tracker, skipped)
        engine.session.mutation_tracker = tracker
        engine.lifecycle.reset_session_to_welcome = fake_reset(engine, engine_services=engine_services)  # type: ignore[assignment]

        selected_norm = os.path.normpath(os.path.abspath(str(selected)))
        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True, selected_paths=[selected_norm]))

        assert selected.read_text(encoding="utf-8") == "before-selected"
        assert skipped.read_text(encoding="utf-8") == "after-skipped"
        notice = engine_services(engine).workspace_change_tracker.take_pending_notice()
        assert notice is not None
        assert notice.startswith("Files not reverted by the rollback")
        assert '"skipped.txt"' in notice
        assert "selected.txt" not in notice
        assert engine_services(engine).workspace_change_tracker.take_pending_notice() is None

    async def test_welcome_failed_restore_reports_retained_paths(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        engine_services(engine).history.bind(_state_after_turns(1))
        target = tmp_path / "blocked.txt"
        target.write_text("before", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(1)
        self._record_modify(tracker, target)
        engine.session.mutation_tracker = tracker
        engine.lifecycle.reset_session_to_welcome = fake_reset(engine, engine_services=engine_services)  # type: ignore[assignment]
        # A directory at the target path makes the snapshot restore fail.
        target.unlink()
        target.mkdir()

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True))

        notice = engine_services(engine).workspace_change_tracker.take_pending_notice()
        assert notice is not None
        assert notice.startswith("Files not reverted by the rollback")
        assert '"blocked.txt"' in notice

    async def test_welcome_full_file_rollback_queues_no_partial_notice(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        engine_services(engine).history.bind(_state_after_turns(1))
        first = tmp_path / "first.txt"
        first.write_text("before-first", encoding="utf-8")
        second = tmp_path / "second.txt"
        second.write_text("before-second", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(1)
        self._record_modify(tracker, first)
        self._record_modify(tracker, second)
        engine.session.mutation_tracker = tracker
        engine.lifecycle.reset_session_to_welcome = fake_reset(engine, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True))

        assert first.read_text(encoding="utf-8") == "before-first"
        assert second.read_text(encoding="utf-8") == "before-second"
        assert engine_services(engine).workspace_change_tracker.take_pending_notice() is None

    async def test_nonzero_partial_file_rollback_reports_unselected_paths(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        store = await seed_turns(engine, 2, engine_services=engine_services)

        selected = tmp_path / "selected.txt"
        selected.write_text("before-selected", encoding="utf-8")
        skipped = tmp_path / "skipped.txt"
        skipped.write_text("before-skipped", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(2)
        self._record_modify(tracker, selected)
        self._record_modify(tracker, skipped)
        engine.session.mutation_tracker = tracker

        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]

        selected_norm = os.path.normpath(os.path.abspath(str(selected)))
        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=True, selected_paths=[selected_norm]))

        assert selected.read_text(encoding="utf-8") == "before-selected"
        assert skipped.read_text(encoding="utf-8") == "after-skipped"
        assert engine_services(engine).workspace_change_tracker.baseline is None
        notice = engine_services(engine).workspace_change_tracker.take_pending_notice()
        assert notice is not None
        assert notice.startswith("Files not reverted by the rollback")
        assert '"skipped.txt"' in notice
        assert "selected.txt" not in notice

    async def test_welcome_revert_with_truncated_detection_warns_incomplete(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """A rolled-back turn with truncated detection and zero recorded rows still warns."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        engine_services(engine).history.bind(_state_after_turns(1))
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(1)
        tracker.mark_detection_truncated()
        engine.session.mutation_tracker = tracker
        engine.lifecycle.reset_session_to_welcome = fake_reset(engine, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True))

        notice = engine_services(engine).workspace_change_tracker.take_pending_notice()
        assert notice is not None
        assert notice.startswith("Files not reverted by the rollback")
        assert "could not be determined completely" in notice
        assert engine_services(engine).workspace_change_tracker.take_pending_notice() is None

    async def test_nonzero_revert_with_truncated_detection_warns_incomplete(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """The incomplete-detection caveat fires even when every known candidate restored."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        store = await seed_turns(engine, 2, engine_services=engine_services)

        target = tmp_path / "restored.txt"
        target.write_text("before", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(2)
        self._record_modify(tracker, target)
        tracker.mark_detection_truncated()
        engine.session.mutation_tracker = tracker

        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=True))

        assert target.read_text(encoding="utf-8") == "before"
        notice = engine_services(engine).workspace_change_tracker.take_pending_notice()
        assert notice is not None
        assert "could not be determined completely" in notice
        assert "restored.txt" not in notice

    async def test_nonzero_revert_ignores_truncation_on_retained_turns(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        """Truncated detection on a turn the rollback keeps does not trigger the caveat."""
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path))))
        store = await seed_turns(engine, 2, engine_services=engine_services)

        target = tmp_path / "restored.txt"
        target.write_text("before", encoding="utf-8")
        tracker = MutationTracker(SnapshotStore(tmp_path / "mutation_store"))
        tracker.start_turn(1)
        tracker.mark_detection_truncated()
        tracker.start_turn(2)
        self._record_modify(tracker, target)
        engine.session.mutation_tracker = tracker

        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=True))

        assert target.read_text(encoding="utf-8") == "before"
        assert engine_services(engine).workspace_change_tracker.take_pending_notice() is None

    async def test_welcome_rollback_reports_error_when_reset_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(2))
        tracker = _FakeMutationTracker([1, 3])
        engine.session.mutation_tracker = tracker  # type: ignore[assignment]

        results: list[RollbackResult] = []
        errors: list[Error] = []
        await _collect_events(engine.event_bus, RollbackResult, results)
        await _collect_events(engine.event_bus, Error, errors)

        async def _fake_reset(
            _session_id: str,
            *,
            write_lock_held: bool = False,
            after_delete: Any = None,
            before_restart: Any = None,
            failure_hint: Any = None,
        ) -> bool:
            _ = write_lock_held, after_delete, before_restart, failure_hint
            return False

        # Record the call itself, not what it was told: a welcome rollback passes
        # an empty target list, so inspecting that would pass whether the audit
        # ran or not.
        trajectory_rollback_calls: list[tuple[int, int]] = []

        async def _fake_trajectory_rollback(*, superseded_turns: list[Any], target_turns: list[Any]) -> None:
            trajectory_rollback_calls.append((len(superseded_turns), len(target_turns)))

        engine.lifecycle.reset_session_to_welcome = _fake_reset  # type: ignore[assignment]
        monkeypatch.setattr(engine_services(engine).trajectory_recorder, "rollback", _fake_trajectory_rollback)

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True, selected_paths=["src/a.py"]))

        assert results == []
        assert tracker.rollback_calls == []
        assert [error.code for error in errors] == ["rollback_reset_failed"]
        assert errors[0].message == (
            "Rollback to welcome could not reset the session because the session state is busy."
        )
        # The failed reset never commits, so the branch was never superseded.
        assert trajectory_rollback_calls == []
        assert_display_message(errors[0], "rollback.reset_failed")


# ===========================================================================
# _on_user_rollback — welcome reset gating and explicit turn-id reverts
# ===========================================================================


async def test_rollback_welcome_reverts_selected_paths_and_reports_changed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    events: list[RollbackResult] = []
    bus = EventBus()
    await bus.subscribe(RollbackResult, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=JsonFileStateStore(tmp_path))
    engine.session.session_id = "rb_test"
    engine_services(engine).fsm.try_transition(Trigger.START)
    tracker = _FakeMutationTracker([1, 3])
    engine.session.mutation_tracker = tracker  # type: ignore[assignment]
    reset_calls: list[str] = []

    async def fake_reset(
        session_id: str,
        *,
        write_lock_held: bool = False,
        after_delete: Any = None,
        before_restart: Any = None,
        failure_hint: Any = None,
    ) -> bool:
        _ = write_lock_held, before_restart, failure_hint
        reset_calls.append(session_id)
        if after_delete is not None:
            await after_delete()
        return True

    monkeypatch.setattr(engine.lifecycle, "reset_session_to_welcome", fake_reset)

    await engine._on_user_rollback(
        UserRollback(target_turn=0, revert_changes=True, selected_paths=["src/a.py", "src/b.py"])
    )

    assert tracker.rollback_calls == [({1, 3}, {"src/a.py", "src/b.py"})]
    assert reset_calls == ["rb_test"]
    assert len(events) == 1
    assert events[0].target_turn == 0
    assert events[0].files_reverted == 1


async def test_rollback_welcome_lock_failure_does_not_revert_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    events: list[RollbackResult] = []
    errors: list[Error] = []
    bus = EventBus()
    await bus.subscribe(RollbackResult, lambda event: collect_events(events, event))
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=JsonFileStateStore(tmp_path))
    engine.session.session_id = "rb_test"
    engine_services(engine).fsm.try_transition(Trigger.START)
    tracker = _FakeMutationTracker([1, 3])
    engine.session.mutation_tracker = tracker  # type: ignore[assignment]
    lock_path = engine.session.session_write_lock_path("rb_test")
    assert lock_path is not None
    monkeypatch.setattr(engine_module, "SESSION_WRITE_LOCK_TIMEOUT_SECONDS", 0.0)

    with FileLock(lock_path):
        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True, selected_paths=["src/a.py"]))

    assert tracker.rollback_calls == []
    assert events == []
    assert [event.code for event in errors] == ["rollback_reset_failed"]
    assert errors[0].message == ("Rollback to welcome could not reset the session because the session state is busy.")
    assert_display_message(errors[0], "rollback.reset_failed")
    assert engine.session_generation == 0
    assert engine.turns.turn_state.lease.prompt_admission_closed is False


async def test_rollback_welcome_unmaterialized_session_dir_still_reverts_files(
    tmp_path: Path, *, engine_services
) -> None:
    events: list[RollbackResult] = []
    bus = EventBus()
    await bus.subscribe(RollbackResult, lambda event: collect_events(events, event))
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    engine.session.session_id = "rb_test"
    engine_services(engine).fsm.try_transition(Trigger.START)
    tracker = _FakeMutationTracker([1, 3])
    engine.session.mutation_tracker = tracker  # type: ignore[assignment]

    session_dir = store.session_dir("rb_test")
    assert not session_dir.exists()

    await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=True, selected_paths=["src/a.py"]))

    assert tracker.rollback_calls == [({1, 3}, {"src/a.py"})]
    assert len(events) == 1
    assert events[0].files_reverted == 1
    assert not session_dir.exists()


async def test_rollback_reverts_explicit_turn_ids_above_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=JsonFileStateStore(tmp_path))
    engine.session.session_id = "rb_test"
    engine_services(engine).fsm.try_transition(Trigger.START)
    tracker = _FakeMutationTracker([1, 3, 2])
    engine.session.mutation_tracker = tracker  # type: ignore[assignment]

    session_dir = tmp_path / "rb_test"
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    _write_session_json(snap_dir / "turn_3.json")

    restore_calls: list[tuple[str, bool]] = []

    async def fake_restore(event, *, for_rollback: bool = False) -> None:
        restore_calls.append((event.session_id, event.apply_saved_model))

    monkeypatch.setattr(engine.lifecycle, "on_session_restore", fake_restore)

    await engine._on_user_rollback(UserRollback(target_turn=2, revert_changes=True))

    assert tracker.rollback_calls == [({3}, None)]
    assert restore_calls == [("rb_test", False)]
