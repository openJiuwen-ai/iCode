# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Refusal paths of ``_on_user_rollback``: state gates, stale picker projections, and lifecycle waits."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.events.types import RollbackResult, UserMessage, UserRollback, Warning
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.util.lock import FileLock
from chrys.orchestration.engine.state.machine import EngineState, Trigger
from chrys.service.state.store import JsonFileStateStore, atomic_copy_file
from tests.orchestration.engine._rollback_helpers import (
    _collect_events,
    _make_engine,
    _state_after_turns,
    fake_restore_factory,
)
from tests.support.components import plan_workspace
from tests.support.event_capture import assert_display_message
from tests.support.loaded_agents import install_loaded_agent, reminder_resources

# ===========================================================================
# _on_user_rollback — refusal paths
# ===========================================================================


class TestRollbackRefusals:
    async def test_welcome_preflight_exception_releases_session_write_lock(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)

        class _ExplodingTracker:
            calls = 0

            def get_all_turns(self) -> list[object]:
                self.calls += 1
                if self.calls == 1:
                    return [object()]
                raise RuntimeError("rollback plan scan failed")

        engine.session.mutation_tracker = _ExplodingTracker()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="rollback plan scan failed"):
            await engine._on_user_rollback(UserRollback(target_turn=0, session_id="rb_test"))

        lock_path = engine.session.session_write_lock_path("rb_test")
        assert lock_path is not None
        with FileLock(lock_path, timeout=0.1):
            pass
        assert engine.session_generation == 0
        assert engine.turns.turn_state.lease.prompt_admission_closed is False

    async def test_prompt_winning_queued_gate_is_refused_without_invalidating_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)
        run_release = asyncio.Event()

        class _PromptExecutor:
            running = False

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

        async def _accept_prepared_contents(*_args: Any, **_kwargs: Any) -> bool:
            return False

        async def run_and_save(*_args: Any, **_kwargs: Any) -> None:
            await run_release.wait()

        install_loaded_agent(engine, bindings=_PromptExecutor())  # type: ignore[assignment]
        install_loaded_agent(engine, **reminder_resources())
        monkeypatch.setattr(engine._turns, "reject_text_only_prepared_contents", _accept_prepared_contents)
        monkeypatch.setattr(engine.turns, "run_and_save", run_and_save)

        await engine.permits.gate_lock.acquire()
        try:
            rollback_task = asyncio.create_task(
                engine._on_user_rollback(UserRollback(target_turn=0, session_id="rb_test"))
            )
            await asyncio.sleep(0)
            assert rollback_task.done() is False

            # A prompt does not need the rebuild gate after it has completed
            # admission, so it can promote while rollback is queued on it.
            await engine._on_user_message(
                UserMessage(text="concurrent ACP prompt", prepared_contents=["concurrent ACP prompt"])
            )
            run_task = engine.turn_lifecycle_task
            assert run_task is not None
        finally:
            engine.permits.gate_lock.release()

        try:
            await asyncio.wait_for(rollback_task, timeout=10)
            assert [warning.code for warning in warnings] == ["rollback_refused"]
            assert engine.session_generation == 0
            assert engine.state is EngineState.RUNNING
            assert engine.turns.turn_state.lease.injection_admission_open is True
            assert engine.turns.turn_state.lease.run_task is run_task
            assert run_task.done() is False
        finally:
            run_release.set()
            await asyncio.gather(run_task, return_exceptions=True)

    async def test_waits_for_reserved_prompt_before_refusing_without_invalidating_run(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)
        admission = engine.turns.turn_state.lease.reserve_prompt_admission(
            kind="fresh",
            session_generation=engine.session_generation,
            build_generation=engine.build_generation,
        )
        assert admission is not None
        run_release = asyncio.Event()

        async def _run() -> None:
            await run_release.wait()

        run_task = asyncio.create_task(_run())
        rollback_task = asyncio.create_task(engine._on_user_rollback(UserRollback(target_turn=0, session_id="rb_test")))
        await asyncio.sleep(0)

        assert rollback_task.done() is False
        assert engine.turns.turn_state.lease.prompt_admission_closed is True
        assert engine.turns.turn_state.lease.active_admission_count() == 1
        assert engine.session_generation == 0

        # Mirror the prompt handler's synchronous promotion boundary: install
        # the task/injection window, release its admission, then advance FSM.
        engine.turns.turn_state.lease.run_task = run_task
        engine.turns.turn_state.lease.injection_admission_open = True
        engine.turns.turn_state.lease.release_prompt_admission(admission)
        engine_services(engine).fsm.try_transition(Trigger.USER_MESSAGE)

        try:
            await asyncio.wait_for(rollback_task, timeout=10)
            assert [warning.code for warning in warnings] == ["rollback_refused"]
            assert engine.session_generation == 0
            assert engine.state is EngineState.RUNNING
            assert engine.turns.turn_state.lease.injection_admission_open is True
            assert engine.turns.turn_state.lease.run_task is run_task
            assert run_task.done() is False
        finally:
            run_release.set()
            await asyncio.gather(run_task, return_exceptions=True)

    async def test_holds_prompt_admission_closed_through_welcome_reset(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(1))
        flush_started = asyncio.Event()
        release_flush = asyncio.Event()
        reset_observations: list[tuple[EngineState, bool]] = []

        async def _blocked_flush() -> None:
            flush_started.set()
            await release_flush.wait()

        async def _fake_reset(
            _session_id: str,
            *,
            write_lock_held: bool = False,
            after_delete: Any = None,
            before_restart: Any = None,
        ) -> bool:
            _ = write_lock_held, after_delete, before_restart
            reset_observations.append((engine.state, engine.turns.turn_state.lease.prompt_admission_closed))
            return True

        engine.writer.flush = _blocked_flush  # type: ignore[assignment]
        engine.lifecycle.reset_session_to_welcome = _fake_reset  # type: ignore[assignment]

        rollback_task = asyncio.create_task(engine._on_user_rollback(UserRollback(target_turn=0, session_id="rb_test")))
        await asyncio.wait_for(flush_started.wait(), timeout=10)

        prompt_task = asyncio.create_task(engine._on_user_message(UserMessage(text="concurrent ACP prompt")))
        await asyncio.sleep(0)
        assert engine.turns.turn_state.lease.prompt_admission_closed is True
        assert prompt_task.done() is False
        assert engine.state is EngineState.IDLE

        release_flush.set()
        await asyncio.wait_for(rollback_task, timeout=10)
        await asyncio.wait_for(prompt_task, timeout=10)

        assert reset_observations == [(EngineState.IDLE, True)]
        assert engine.turns.turn_state.lease.prompt_admission_closed is False

    async def test_waits_for_exact_turn_lifecycle_before_validating_target(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)
        lifecycle_release = asyncio.Event()
        replacement_release = asyncio.Event()

        async def wait_for_release(release: asyncio.Event) -> None:
            await release.wait()

        captured_task = asyncio.create_task(wait_for_release(lifecycle_release))
        replacement_task = asyncio.create_task(wait_for_release(replacement_release))
        engine.turns.turn_state.lease.run_task = captured_task

        try:
            rollback_task = asyncio.create_task(
                engine._on_user_rollback(UserRollback(target_turn=5, session_id="rb_test"))
            )
            await asyncio.sleep(0)
            engine.turns.turn_state.lease.run_task = replacement_task
            assert warnings == []

            lifecycle_release.set()
            await asyncio.wait_for(rollback_task, timeout=10)
            assert [warning.code for warning in warnings] == ["rollback_unavailable"]
            assert not replacement_task.done()
        finally:
            replacement_task.cancel()
            await asyncio.gather(replacement_task, return_exceptions=True)

    async def test_relative_target_is_resolved_after_captured_lifecycle(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        store = JsonFileStateStore(tmp_path)
        engine_services(engine).history.bind(_state_after_turns(2))
        engine.session.turn_number = 2
        lifecycle_release = asyncio.Event()
        results: list[RollbackResult] = []
        await _collect_events(engine.event_bus, RollbackResult, results)

        async def _finalize_third_turn() -> None:
            await lifecycle_release.wait()
            await store.save_session("rb_test", _state_after_turns(2))
            session_file = store.session_dir("rb_test") / "session.json"
            snapshot_dir = session_file.parent / "snapshots"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            atomic_copy_file(session_file, snapshot_dir / "turn_3.json")
            await store.save_session("rb_test", _state_after_turns(3))
            live = await store.load_session("rb_test")
            assert live is not None
            engine_services(engine).history.bind(live)
            engine.session.turn_number = 3

        lifecycle_task = asyncio.create_task(_finalize_third_turn())
        engine.turns.turn_state.lease.run_task = lifecycle_task
        engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]
        rollback_task = asyncio.create_task(
            engine._on_user_rollback(
                UserRollback(
                    target_turn=1,
                    relative_turns=1,
                    session_id="rb_test",
                )
            )
        )
        await asyncio.sleep(0)
        assert results == []

        lifecycle_release.set()
        await asyncio.wait_for(rollback_task, timeout=10)

        assert len(results) == 1
        assert results[0].target_turn == 2
        assert results[0].rolled_back_user_text == "user 3"
        assert engine.session.turn_number == 2

    async def test_rejects_stale_picker_projection_before_committing_transition(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(4))
        engine.session.turn_number = 4
        engine.turns.turn_state.lease.injection_admission_open = True
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(
            UserRollback(
                target_turn=2,
                expected_current_turn=3,
                session_id="rb_test",
            )
        )

        assert [warning.code for warning in warnings] == ["rollback_conversation_changed"]
        assert warnings[0].message == "Rollback cancelled because the conversation advanced from turn 3 to turn 4."
        assert_display_message(
            warnings[0],
            "rollback.conversation_advanced",
            {"expected_turn": 3, "current_turn": 4},
        )
        assert engine.session_generation == 0
        assert engine.session.turn_number == 4
        assert engine.state is EngineState.IDLE
        assert engine.turns.turn_state.lease.injection_admission_open is True
        assert engine.turns.turn_state.lease.prompt_admission_closed is False

    async def test_rejects_stale_picker_projection_after_same_turn_retry(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(4))
        engine.session.turn_number = 4
        picker_revision = engine.conversation_revision
        # A retry lifecycle advances the conversation without increasing the
        # logical turn number or necessarily allocating another run scope.
        engine.turns.turn_state.lease.advance_conversation_revision()
        engine.turns.turn_state.lease.injection_admission_open = True
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(
            UserRollback(
                target_turn=2,
                expected_current_turn=4,
                expected_conversation_revision=picker_revision,
                session_id="rb_test",
            )
        )

        assert [warning.code for warning in warnings] == ["rollback_conversation_changed"]
        assert warnings[0].message == (
            "Rollback cancelled because the conversation changed after the picker was loaded."
        )
        assert_display_message(warnings[0], "rollback.conversation_changed")
        assert engine.session_generation == 0
        assert engine.session.turn_number == 4
        assert engine.conversation_revision == picker_revision + 1
        assert engine.state is EngineState.IDLE
        assert engine.turns.turn_state.lease.injection_admission_open is True
        assert engine.turns.turn_state.lease.prompt_admission_closed is False

    @pytest.mark.parametrize("drift", ["build", "workspace"])
    async def test_rejects_picker_projection_after_runtime_rebuild_drift(
        self, tmp_path: Path, drift: str, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine_services(engine).history.bind(_state_after_turns(4))
        engine.session.turn_number = 4
        picker_build_generation = engine.build_generation
        picker_workspace_cwd = engine.workspace_primary_cwd
        if drift == "build":
            engine.permits.advance_build_generation()
        else:
            engine.session.install_workspace(plan_workspace(Workspace.from_cwd(str(tmp_path / "rebuilt-workspace"))))
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(
            UserRollback(
                target_turn=2,
                expected_current_turn=4,
                expected_conversation_revision=engine.conversation_revision,
                expected_build_generation=picker_build_generation,
                expected_workspace_cwd=picker_workspace_cwd,
                session_id="rb_test",
            )
        )

        assert [warning.code for warning in warnings] == ["rollback_runtime_changed"]
        assert warnings[0].message == (
            "Rollback cancelled because the workspace or runtime changed after the picker was loaded."
        )
        assert_display_message(warnings[0], "rollback.runtime_changed")
        assert engine.session_generation == 0
        assert engine.session.turn_number == 4
        assert engine.state is EngineState.IDLE
        assert engine.turns.turn_state.lease.prompt_admission_closed is False

    async def test_revalidates_session_generation_after_turn_lifecycle_wait(
        self, tmp_path: Path, *, engine_services
    ) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)
        lifecycle_release = asyncio.Event()

        async def wait_for_release() -> None:
            await lifecycle_release.wait()

        lifecycle_task = asyncio.create_task(wait_for_release())
        engine.turns.turn_state.lease.run_task = lifecycle_task
        rollback_task = asyncio.create_task(engine._on_user_rollback(UserRollback(target_turn=5, session_id="rb_test")))
        await asyncio.sleep(0)

        # A switch away and back may restore the same visible ID; the
        # monotonic generation still invalidates the deferred request.
        engine.permits.advance_session_generation()
        lifecycle_release.set()
        await asyncio.wait_for(rollback_task, timeout=10)

        assert [warning.code for warning in warnings] == ["rollback_session_changed"]
        assert warnings[0].message == "Rollback cancelled because the active session changed."
        assert_display_message(warnings[0], "rollback.session_changed")

    async def test_refused_when_running(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, fsm_state=EngineState.RUNNING, engine_services=engine_services)
        assert engine.state is EngineState.RUNNING
        lifecycle_release = asyncio.Event()

        async def wait_for_release() -> None:
            await lifecycle_release.wait()

        lifecycle_task = asyncio.create_task(wait_for_release())
        engine.turns.turn_state.lease.run_task = lifecycle_task

        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        try:
            await asyncio.wait_for(
                engine._on_user_rollback(UserRollback(target_turn=2, revert_changes=False)),
                timeout=10,
            )
        finally:
            lifecycle_task.cancel()
            await asyncio.gather(lifecycle_task, return_exceptions=True)

        await asyncio.sleep(0)  # let the bus dispatch
        refused = next(warning for warning in warnings if warning.code == "rollback_refused")
        assert refused.message == "Rollback is not allowed in state RUNNING."
        assert_display_message(refused, "rollback.refused", {"state": "RUNNING"})

    async def test_refused_when_pending_retry(self, tmp_path: Path, *, engine_services) -> None:
        """Retry already queued → reject rollback.

        PENDING_RETRY means the user already asked us to resume after
        the current run winds down; letting a rollback race that would
        leave the engine in an inconsistent state where the retry
        lands on a session that's been swapped out from under it.
        """
        engine = _make_engine(tmp_path, fsm_state=EngineState.PENDING_RETRY, engine_services=engine_services)
        assert engine.state is EngineState.PENDING_RETRY

        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(UserRollback(target_turn=2, revert_changes=False))
        await asyncio.sleep(0)
        assert any(w.code == "rollback_refused" for w in warnings)

    async def test_refused_when_awaiting_sub_agents(self, tmp_path: Path, *, engine_services) -> None:
        """Parent run is pinned on a paused sub-agent decision → reject rollback.

        Mutating history while a ``pending_decision`` future is
        outstanding would detach the sub-agent's saved record from
        the conversation state the user ends up on.  Matches the
        FSM's ``is_running()`` contract that covers this state.
        """
        engine = _make_engine(tmp_path, fsm_state=EngineState.AWAITING_SUB_AGENTS, engine_services=engine_services)
        assert engine.state is EngineState.AWAITING_SUB_AGENTS

        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(UserRollback(target_turn=2, revert_changes=False))
        await asyncio.sleep(0)
        assert any(w.code == "rollback_refused" for w in warnings)

    async def test_refused_without_session(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.session_id = None

        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(UserRollback(target_turn=2))

        await asyncio.sleep(0)
        warning = next(warning for warning in warnings if warning.code == "rollback_no_session")
        assert warning.message == "No active session to roll back."
        assert_display_message(warning, "rollback.no_session")

    async def test_refused_when_relative_turn_count_is_non_positive(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(UserRollback(target_turn=0, relative_turns=0))

        assert len(warnings) == 1
        assert warnings[0].message == "relative_turns must be positive."
        assert_display_message(warnings[0], "rollback.relative_turns_invalid")

    async def test_refused_when_relative_turn_count_exceeds_session(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        engine.session.turn_number = 2
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(UserRollback(target_turn=0, relative_turns=3))

        assert len(warnings) == 1
        assert warnings[0].message == "Cannot roll back 3 turns; the session currently has 2."
        assert_display_message(
            warnings[0],
            "rollback.turns_unavailable",
            {"requested_turns": 3, "current_turns": 2},
        )

    async def test_refused_invalid_turn(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        # Negative values are invalid under the "keep N turns" convention
        # (``0`` is the welcome target).
        await engine._on_user_rollback(UserRollback(target_turn=-1))

        await asyncio.sleep(0)
        warning = next(warning for warning in warnings if warning.code == "rollback_invalid_turn")
        assert warning.message == "target_turn must be >= 0."
        assert_display_message(warning, "rollback.target_turn_invalid")
        assert engine.session_generation == 0

    async def test_refused_when_target_unavailable(self, tmp_path: Path, *, engine_services) -> None:
        engine = _make_engine(tmp_path, engine_services=engine_services)
        # No snapshots, no turns
        warnings: list[Warning] = []
        await _collect_events(engine.event_bus, Warning, warnings)

        await engine._on_user_rollback(UserRollback(target_turn=5))

        await asyncio.sleep(0)
        warning = next(warning for warning in warnings if warning.code == "rollback_unavailable")
        assert warning.message == "Cannot roll back to turn 5; available turns: []"
        assert_display_message(
            warning,
            "rollback.turn_unavailable",
            {"target_turn": 5, "available": "[]"},
        )
        assert engine.session_generation == 0
