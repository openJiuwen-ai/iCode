# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the engine-side :class:`TrajectoryRecorder` (no full engine).

The recorder is bound to a ``tmp_path`` session directory and driven through
its lifecycle anchors; every assertion reads the events back from the
session's ``trajectory/events.jsonl`` through the foundation reader.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.foundation.trajectory.envelope import (
    ActorKind,
    ActorRole,
    MeasurementSource,
    TrajectoryEvent,
    malformed_id_pointers,
)
from chrys.foundation.trajectory.event_types import (
    EventType,
    ProfileKind,
    RuntimeFinishReason,
    SourceRefKind,
    TurnEndReason,
    TurnSuspendReason,
)
from chrys.foundation.trajectory.ids import is_valid_analytics_id, new_analytics_id
from chrys.foundation.trajectory.lease import WriterLease
from chrys.foundation.trajectory.reader import last_event_of_type, read_trajectory
from chrys.foundation.trajectory.segments import reassemble_array_slice
from chrys.orchestration.engine.trajectory import (
    ROLLBACK_REASON_FRESH_TURN_RETIRED,
    ROLLBACK_REASON_USER,
    ActiveTurnTrace,
    TrajectoryRecorder,
    exchange_facts,
)
from chrys.service.mutations.types import FileHashDiff, SnapshotSkipReason
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import SessionCheckpoint
from chrys.service.trajectory.session import (
    SessionStartInfo,
    SessionTrajectory,
    trajectory_dir,
    trajectory_events_path,
)
from chrys.service.trajectory.state import (
    TRAJECTORY_STATE_KEY,
    TurnRecord,
    append_turn,
    confirm_resumed_turn,
    discard_last_turn,
    fork_turn_registry,
    legacy_turn_records,
    mark_turn_sequence,
    turn_records,
)
from tests.support.trajectory_invariants import assert_trajectory_accounted
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_until

SESSION_ID = "12345678-1234-1234-1234-123456789abc"
FORK_SESSION_ID = "87654321-4321-4321-4321-cba987654321"
AGENT_FP = "agent-fingerprint-0001"
MODEL_FP = "model-fingerprint-0001"


# --------------------------------------------------------------------- helpers


@dataclass(slots=True)
class BoundRecorder:
    """A recorder bound to a fresh session directory plus the paths to read it back."""

    recorder: TrajectoryRecorder
    trajectory: SessionTrajectory
    session_dir: Path
    primary_cwd: str
    history_state: dict[str, Any]

    @property
    def events_path(self) -> Path:
        return trajectory_events_path(self.session_dir)

    def events(self) -> list[TrajectoryEvent]:
        result = read_trajectory(self.events_path)
        assert result.corrupt_lines == []
        assert result.torn_tail_bytes == 0
        assert result.unsupported_event_count == 0
        return result.events

    def of_type(self, event_type: str) -> list[TrajectoryEvent]:
        return [event for event in self.events() if event.event_type == event_type]

    def last(self, event_type: str) -> TrajectoryEvent:
        event = last_event_of_type(self.events(), event_type)
        assert event is not None, f"no {event_type} event recorded"
        return event

    async def settle(self) -> None:
        """Wait out the acks an ``emit_soon`` left in flight before reading the file.

        ``emit_soon`` takes its sequence synchronously and waits for the ack in
        a background task, so the line is queued but not necessarily on disk
        when the call returns. Awaiting those tasks is the same signal
        ``close`` uses, without polling a file mid-append.
        """
        waits = tuple(self.trajectory._background_waits)
        if waits:
            await asyncio.gather(*waits, return_exceptions=True)

    async def start_turn(
        self,
        turn_number: int,
        *,
        is_retry: bool = False,
        turn_id: str | None = None,
        register: bool = True,
        **overrides: Any,
    ) -> str:
        """Open a pass and register it the way the runner does; returns the pass id."""
        pass_id = new_analytics_id()
        logical_id = turn_id or pass_id
        if register and not is_retry:
            append_turn(
                self.history_state,
                TurnRecord(
                    turn_number=turn_number,
                    turn_id=logical_id,
                    opening_item_id=overrides.get("opening_item_id"),
                ),
            )
        kwargs: dict[str, Any] = {
            "turn_id": pass_id,
            "logical_turn_id": logical_id,
            "turn_number": turn_number,
            "is_retry": is_retry,
            "agent_profile_fingerprint": AGENT_FP,
            "model_profile_fingerprint": MODEL_FP,
            "primary_cwd": self.primary_cwd,
            "history_state": self.history_state,
        }
        kwargs.update(overrides)
        opened = await self.recorder.turn_started(**kwargs)
        assert opened == pass_id
        return pass_id

    def registry(self) -> list[TurnRecord]:
        return turn_records(self.history_state)

    def rollback_to(self, turn_number: int | None) -> tuple[list[TurnRecord], list[TurnRecord]]:
        """Split the registry the way ``rollback.py`` does for a snapshot at *turn_number*.

        ``None`` is the welcome-page branch: nothing is kept, so every
        registered turn is superseded.
        """
        kept = [record for record in self.registry() if turn_number is not None and record.turn_number <= turn_number]
        kept_ids = {record.turn_id for record in kept}
        superseded = [record for record in self.registry() if record.turn_id not in kept_ids]
        return superseded, kept


def _start_info_factory(primary_cwd: str) -> Callable[[], SessionStartInfo | None]:
    return lambda: SessionStartInfo(
        primary_cwd=primary_cwd,
        agent_profile_fingerprint=AGENT_FP,
        model_profile_fingerprint=MODEL_FP,
    )


def _bind(tmp_path: Path, *, name: str = "sess", session_id: str = SESSION_ID) -> BoundRecorder:
    session_dir = tmp_path / name
    session_dir.mkdir(parents=True, exist_ok=True)
    primary_cwd = str(tmp_path / "workspace")
    recorder = TrajectoryRecorder()
    trajectory = recorder.bind_session(
        session_id=session_id,
        session_dir=session_dir,
        write_lock_path=tmp_path / f"{name}.write.lock",
        session_start_info=_start_info_factory(primary_cwd),
    )
    return BoundRecorder(
        recorder=recorder,
        trajectory=trajectory,
        session_dir=session_dir,
        primary_cwd=primary_cwd,
        history_state={},
    )


@pytest.fixture
async def bound(tmp_path: Path) -> AsyncIterator[BoundRecorder]:
    """A bound recorder, closed at teardown even when the test leaves it open."""
    recorder = _bind(tmp_path)
    yield recorder
    # An activated recorder owns a writer thread, the session's writer lease
    # and two descriptors. Left open they outlive the test and are still there
    # when a later module counts live writers.
    await recorder.recorder.close(reason=RuntimeFinishReason.SESSION_SWITCH)


def _diff(before: str | None, after: str | None, **kwargs: Any) -> FileHashDiff:
    return FileHashDiff(before=before, after=after, **kwargs)


def _assert_no_substring(value: Any, needle: str) -> None:
    """Recursively assert *needle* appears nowhere in *value* (strings, keys, nested containers)."""
    assert needle not in json.dumps(value, ensure_ascii=False)


# -------------------------------------------------------------- 1. unbound


class TestUnboundRecorder:
    def test_inspection_is_empty(self) -> None:
        recorder = TrajectoryRecorder()
        assert recorder.trajectory is None
        assert recorder.current_turn is None
        assert recorder.current_turn_id is None
        assert recorder.context() is None

    @pytest.mark.asyncio
    async def test_every_lifecycle_method_is_a_safe_no_op(self, tmp_path: Path) -> None:
        recorder = TrajectoryRecorder()
        state: dict[str, Any] = {}
        assert (
            await recorder.turn_started(
                turn_id=new_analytics_id(),
                logical_turn_id=new_analytics_id(),
                turn_number=1,
                is_retry=False,
                agent_profile_fingerprint=AGENT_FP,
                model_profile_fingerprint=MODEL_FP,
                primary_cwd=str(tmp_path),
                history_state=state,
            )
            is None
        )
        assert state == {}
        await recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        recorder.interrupt_requested_soon(source="user", scope="turn")
        await recorder.turn_suspended()
        await recorder.turn_resumed()
        await recorder.checkpoint()
        await recorder.mutation_summary({"a.txt": _diff(None, "h1")}, checkpoint=None)
        await recorder.profile_switched(kind=ProfileKind.MODEL, from_fingerprint="a", to_fingerprint="b")
        await recorder.rollback(superseded_turns=[], target_turns=[])
        await recorder.close()
        await recorder.close(reason=RuntimeFinishReason.SESSION_SWITCH)
        assert recorder.trajectory is None
        assert recorder.current_turn is None
        assert recorder.current_turn_id is None
        assert recorder.context() is None
        # Nothing was ever materialized on disk.
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.asyncio
    async def test_fork_from_unbound_recorder_writes_fork_prelude_at_sequence_zero(self, tmp_path: Path) -> None:
        recorder = TrajectoryRecorder()
        fork_dir = tmp_path / "fork"
        fork_dir.mkdir()
        await recorder.fork(
            origin_session_id=SESSION_ID,
            fork_session_id=FORK_SESSION_ID,
            fork_session_dir=fork_dir,
            fork_write_lock_path=tmp_path / "fork.write.lock",
            session_start_info=_start_info_factory(str(tmp_path / "ws"))(),
        )
        events = read_trajectory(trajectory_events_path(fork_dir)).events
        forked = last_event_of_type(events, EventType.SESSION_FORKED)
        assert forked is not None
        assert forked.payload["origin_session_id"] == SESSION_ID
        assert forked.payload["forked_at_sequence"] == 0
        # The recorder itself stays unbound.
        assert recorder.trajectory is None


# ---------------------------------------------------------------- 2. binding


class TestBindSession:
    def test_bind_is_idempotent_for_the_same_open_session(self, bound: BoundRecorder) -> None:
        again = bound.recorder.bind_session(
            session_id=SESSION_ID,
            session_dir=bound.session_dir,
            write_lock_path=None,
            session_start_info=_start_info_factory(bound.primary_cwd),
        )
        assert again is bound.trajectory
        assert bound.recorder.trajectory is bound.trajectory

    def test_bind_to_another_session_replaces_the_trajectory(self, bound: BoundRecorder, tmp_path: Path) -> None:
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        other = bound.recorder.bind_session(
            session_id=FORK_SESSION_ID,
            session_dir=other_dir,
            write_lock_path=None,
            session_start_info=_start_info_factory(bound.primary_cwd),
        )
        assert other is not bound.trajectory
        assert other.session_id == FORK_SESSION_ID
        assert bound.recorder.trajectory is other

    @pytest.mark.asyncio
    async def test_rebinding_the_same_session_after_close_creates_a_fresh_trajectory(
        self, bound: BoundRecorder
    ) -> None:
        await bound.start_turn(1)
        await bound.recorder.close()
        assert bound.recorder.trajectory is None
        fresh = bound.recorder.bind_session(
            session_id=SESSION_ID,
            session_dir=bound.session_dir,
            write_lock_path=None,
            session_start_info=_start_info_factory(bound.primary_cwd),
        )
        assert fresh is not bound.trajectory
        assert not fresh.is_closed
        assert bound.recorder.current_turn is None

    @pytest.mark.asyncio
    async def test_rebinding_drops_the_traced_turn_and_pending_interrupt(
        self, bound: BoundRecorder, tmp_path: Path
    ) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        bound.recorder.bind_session(
            session_id=FORK_SESSION_ID,
            session_dir=other_dir,
            write_lock_path=None,
            session_start_info=_start_info_factory(bound.primary_cwd),
        )
        assert bound.recorder.current_turn is None
        assert bound.recorder.current_turn_id is None
        # A turn finish against the new session has no turn to close: no events at all.
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        assert not trajectory_events_path(other_dir).exists()
        # The rebind dropped the first session's recorder without closing it —
        # the engine shuts down before every cross-session bind, so only this
        # test reaches that state, and only this test has to reap the writer.
        await bound.trajectory.close(reason=RuntimeFinishReason.SESSION_SWITCH)

    def test_binding_alone_materializes_nothing(self, bound: BoundRecorder) -> None:
        assert not trajectory_dir(bound.session_dir).exists()
        assert not bound.trajectory.is_active

    def test_context_before_any_turn_has_main_actor_and_no_turn(self, bound: BoundRecorder) -> None:
        context = bound.recorder.context()
        assert isinstance(context, TrajectoryContext)
        assert context.sink is bound.trajectory
        assert context.session_id == SESSION_ID
        assert context.actor == bound.trajectory.main_actor
        assert context.actor.kind == ActorKind.AGENT
        assert context.actor.role == ActorRole.MAIN
        assert context.turn_id is None
        assert context.run_operation_id is None


# ------------------------------------------------------------------ 2. turns


class TestTurns:
    @pytest.mark.asyncio
    async def test_turn_started_emits_turn_started_with_payload_and_state_record(self, bound: BoundRecorder) -> None:
        turn_id = await bound.start_turn(1)
        assert is_valid_analytics_id(turn_id)

        trace = bound.recorder.current_turn
        assert isinstance(trace, ActiveTurnTrace)
        assert trace.turn_id == turn_id
        assert trace.turn_number == 1
        assert trace.suspended is False
        assert trace.finished is False
        assert bound.recorder.current_turn_id == turn_id

        context = bound.recorder.context()
        assert context is not None
        assert context.turn_id == turn_id

        started = bound.last(EventType.TURN_STARTED)
        assert started.turn_id == turn_id
        assert started.actor == bound.trajectory.main_actor
        payload = started.payload
        assert payload["turn_id"] == turn_id
        assert payload["turn_number"] == 1
        assert payload["is_retry"] is False
        assert payload["agent_profile_fingerprint"] == AGENT_FP
        assert payload["model_profile_fingerprint"] == MODEL_FP
        assert payload["opened_at"] == trace.opened_at
        assert payload["opened_at"] == started.occurred_at
        assert "opening_item_id" not in payload
        # The workspace path itself never reaches the log: only its keyed fingerprint.
        assert payload["workspace_revision"]
        assert payload["workspace_revision"] != bound.primary_cwd
        _assert_no_substring(dict(payload), bound.primary_cwd)

        # The turn registry in the history state round-trips the id and the landing sequence.
        records = bound.registry()
        assert len(records) == 1
        record = records[0]
        assert record.turn_id == turn_id
        assert record.turn_number == 1
        assert record.started_sequence == started.sequence
        assert payload["logical_turn_id"] == turn_id

    @pytest.mark.asyncio
    async def test_first_event_activates_the_runtime_prelude(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        types = [event.event_type for event in bound.events()]
        assert types == [
            EventType.COVERAGE_STARTED,
            EventType.RUNTIME_STARTED,
            EventType.SESSION_STARTED,
            EventType.TURN_STARTED,
        ]
        assert bound.trajectory.is_active
        session_started = bound.last(EventType.SESSION_STARTED)
        assert session_started.payload["agent_profile_fingerprint"] == AGENT_FP
        assert session_started.payload["model_profile_fingerprint"] == MODEL_FP
        assert (
            session_started.payload["workspace_fingerprint"]
            == bound.last(EventType.TURN_STARTED).payload["workspace_revision"]
        )
        _assert_no_substring(dict(session_started.payload), bound.primary_cwd)

    @pytest.mark.asyncio
    async def test_opening_item_id_and_retry_flag_are_carried(self, bound: BoundRecorder) -> None:
        item_id = "0123456789abcdef0123456789abcdef"
        preparation_id = "fedcba9876543210fedcba9876543210"
        await bound.start_turn(
            3,
            is_retry=True,
            opening_item_id=item_id,
            preparation_scope_operation_id=preparation_id,
        )
        payload = bound.last(EventType.TURN_STARTED).payload
        assert payload["is_retry"] is True
        assert payload["opening_item_id"] == item_id
        assert payload["preparation_scope_operation_id"] == preparation_id
        assert payload["turn_number"] == 3

    @pytest.mark.asyncio
    async def test_turn_started_without_history_state_records_nothing(self, bound: BoundRecorder) -> None:
        turn_id = new_analytics_id()
        append_turn(bound.history_state, TurnRecord(turn_number=1, turn_id=turn_id))
        pass_id = await bound.start_turn(1, turn_id=turn_id, register=False, history_state=None)

        assert [record.started_sequence for record in bound.registry()] == [None]
        assert bound.last(EventType.TURN_STARTED).payload["turn_id"] == pass_id

    @pytest.mark.asyncio
    async def test_a_turn_whose_start_was_committed_survives_an_interrupted_ack(
        self, bound: BoundRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupt on the write ack must not orphan a start that is already on disk."""
        real_emit = bound.trajectory.emit

        async def _committed_then_interrupted(draft: Any, **kwargs: Any) -> Any:
            # The line takes its sequence and is queued; only the wait for its
            # ack is interrupted.
            await real_emit(draft, **kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(bound.trajectory, "emit", _committed_then_interrupted)
        with pytest.raises(asyncio.CancelledError):
            await bound.start_turn(1)
        monkeypatch.setattr(bound.trajectory, "emit", real_emit)

        assert bound.recorder.current_turn is not None
        await bound.recorder.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)

        assert bound.of_type(EventType.TURN_STARTED)[0].payload["turn_number"] == 1
        assert bound.last(EventType.TURN_FINISHED).payload["end_reason"] == TurnEndReason.PROCESS_EXIT

    @pytest.mark.asyncio
    async def test_a_turn_whose_start_lands_after_the_cancel_is_still_closed(
        self, bound: BoundRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lazy activation outlives its caller: cancelling the emit does not stop the thread."""
        entered = threading.Event()
        release = threading.Event()
        real_activate = bound.trajectory._activate

        def _parked_activation() -> None:
            entered.set()
            release.wait(timeout=ENGINE_TURN_TIMEOUT)
            real_activate()

        monkeypatch.setattr(bound.trajectory, "_activate", _parked_activation)
        task = asyncio.create_task(bound.start_turn(1))
        await asyncio.to_thread(entered.wait, ENGINE_TURN_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The caller is gone, and the thread it left behind now opens the log
        # and writes the line anyway. The turn is that line's, so it is still
        # the recorder's.
        assert bound.recorder.current_turn is not None
        release.set()
        assert await wait_until(lambda: bool(bound.of_type(EventType.TURN_STARTED)))

        await bound.recorder.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
        assert bound.last(EventType.TURN_FINISHED).payload["end_reason"] == TurnEndReason.PROCESS_EXIT

    @pytest.mark.asyncio
    async def test_a_close_that_races_the_start_waits_for_it_instead_of_guessing(
        self, bound: BoundRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A start still in flight is not a start that failed: the close has to wait to tell."""
        entered = threading.Event()
        release = threading.Event()
        real_activate = bound.trajectory._activate

        def _parked_activation() -> None:
            entered.set()
            release.wait(timeout=ENGINE_TURN_TIMEOUT)
            real_activate()

        monkeypatch.setattr(bound.trajectory, "_activate", _parked_activation)
        task = asyncio.create_task(bound.start_turn(1))
        await asyncio.to_thread(entered.wait, ENGINE_TURN_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The close lands while the activation is still parked: nothing has
        # been written yet, and nothing has been given up on either.
        closing = asyncio.create_task(bound.recorder.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN))
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing

        assert bound.of_type(EventType.TURN_STARTED)[0].payload["turn_number"] == 1
        assert bound.last(EventType.TURN_FINISHED).payload["end_reason"] == TurnEndReason.PROCESS_EXIT

    @pytest.mark.asyncio
    async def test_turn_finished_emits_turn_finished_and_clears_current_turn(self, bound: BoundRecorder) -> None:
        turn_id = await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)

        assert bound.recorder.current_turn_id is None
        trace = bound.recorder.current_turn
        assert trace is not None and trace.finished is True
        context = bound.recorder.context()
        assert context is not None and context.turn_id is None

        finished = bound.last(EventType.TURN_FINISHED)
        assert finished.turn_id == turn_id
        assert finished.payload["turn_id"] == turn_id
        assert finished.payload["end_reason"] == TurnEndReason.COMPLETED
        assert finished.payload["closed_at"] == finished.occurred_at
        duration = finished.payload["duration_ms"]
        assert isinstance(duration, int) and duration >= 0
        assert finished.measurements["/payload/duration_ms"] == {
            "source": MeasurementSource.MONOTONIC_CLOCK,
            "method_version": 1,
        }
        assert finished.monotonic_ns >= bound.last(EventType.TURN_STARTED).monotonic_ns
        assert len(bound.of_type(EventType.INTERRUPT_OBSERVED)) == 0

    @pytest.mark.asyncio
    async def test_turn_finished_is_once_only(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.ERROR)
        finished = bound.of_type(EventType.TURN_FINISHED)
        assert len(finished) == 1
        assert finished[0].payload["end_reason"] == TurnEndReason.COMPLETED

    @pytest.mark.asyncio
    async def test_response_settled_is_a_once_only_post_turn_fence(self, bound: BoundRecorder) -> None:
        turn_id = await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        hook_id = "0123456789abcdef0123456789abcdef"

        await bound.recorder.turn_response_settled(
            outcome="settled",
            drained_scopes=["turn"],
            waited_hook_operation_ids=[hook_id],
        )
        await bound.recorder.turn_response_settled(
            outcome="partial",
            drained_scopes=[],
            waited_hook_operation_ids=[],
        )

        settled = bound.of_type(EventType.TURN_RESPONSE_SETTLED)
        assert len(settled) == 1
        assert settled[0].turn_id == turn_id
        assert settled[0].payload == {
            "outcome": "settled",
            "drained_scopes": ["turn"],
            "waited_hook_operation_count": 1,
        }
        hook_segments = [
            event.payload
            for event in bound.of_type(EventType.SEGMENT)
            if event.payload["parent_event_id"] == settled[0].event_id
            and event.payload["field_pointer"] == "/payload/waited_hook_operation_ids"
        ]
        assert reassemble_array_slice(hook_segments) == [hook_id]

    @pytest.mark.asyncio
    async def test_response_settled_bounds_hook_ids_to_the_line_budget(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        hook_ids = [f"{index:032x}" for index in range(100)]

        await bound.recorder.turn_response_settled(
            outcome="settled",
            drained_scopes=["turn"],
            waited_hook_operation_ids=hook_ids,
        )

        settled = bound.last(EventType.TURN_RESPONSE_SETTLED)
        assert settled.payload["waited_hook_operation_count"] == 100
        hook_segments = [
            event.payload
            for event in bound.of_type(EventType.SEGMENT)
            if event.payload["parent_event_id"] == settled.event_id
            and event.payload["field_pointer"] == "/payload/waited_hook_operation_ids"
        ]
        assert len(hook_segments) > 1
        assert reassemble_array_slice(hook_segments) == hook_ids
        assert bound.of_type(EventType.GAP) == []

    @pytest.mark.asyncio
    async def test_turn_finished_without_open_turn_is_a_no_op(self, bound: BoundRecorder) -> None:
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        assert not bound.events_path.exists()

    @pytest.mark.asyncio
    async def test_every_terminal_end_reason_is_recorded_verbatim(self, bound: BoundRecorder) -> None:
        reasons = [
            TurnEndReason.COMPLETED,
            TurnEndReason.ERROR,
            TurnEndReason.INTERRUPTED,
            TurnEndReason.CANCELLED,
            TurnEndReason.SUPERSEDED,
            TurnEndReason.PROCESS_EXIT,
        ]
        for number, reason in enumerate(reasons, start=1):
            await bound.start_turn(number)
            await bound.recorder.turn_finished(end_reason=reason)
        assert [event.payload["end_reason"] for event in bound.of_type(EventType.TURN_FINISHED)] == reasons

    @pytest.mark.asyncio
    async def test_turn_ids_are_unique_even_when_turn_numbers_repeat(self, bound: BoundRecorder) -> None:
        first = await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.rollback(superseded_turns=bound.registry(), target_turns=[])
        second = await bound.start_turn(1)
        assert first != second
        assert is_valid_analytics_id(first) and is_valid_analytics_id(second)
        # A number handed out again names a different turn, and both stay identifiable.
        assert [record.turn_id for record in bound.registry()] == [first, second]
        assert [record.turn_number for record in bound.registry()] == [1, 1]
        started = bound.of_type(EventType.TURN_STARTED)
        assert [event.payload["turn_number"] for event in started] == [1, 1]
        assert {event.turn_id for event in started} == {first, second}

    @pytest.mark.asyncio
    async def test_a_retry_keeps_the_sequence_the_turn_first_opened_at(self, bound: BoundRecorder) -> None:
        first = await bound.start_turn(4)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.ERROR)
        opened_at = bound.registry()[-1]
        assert opened_at.turn_id == first

        # The runner mints a pass id of its own for the retry and names the turn.
        retry_pass_id = await bound.start_turn(4, is_retry=True, turn_id=first)
        retry_started = bound.last(EventType.TURN_STARTED)

        assert retry_pass_id != first
        assert retry_started.turn_id == retry_pass_id
        assert retry_started.payload["logical_turn_id"] == first
        # The abandoned pass is part of turn 4 too: a rollback that supersedes
        # the turn has to start where it first opened, not at the retry.
        assert bound.registry() == [opened_at]

    @pytest.mark.asyncio
    async def test_starting_a_new_turn_replaces_the_traced_turn(self, bound: BoundRecorder) -> None:
        first = await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        second = await bound.start_turn(2)
        assert bound.recorder.current_turn_id == second
        assert second != first
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        finished = bound.of_type(EventType.TURN_FINISHED)
        assert [event.turn_id for event in finished] == [first, second]


# ------------------------------------------------------------- 3. interrupts


class TestInterrupts:
    @pytest.mark.asyncio
    async def test_interrupt_requested_then_interrupted_finish_emits_observed_before_finished(
        self, bound: BoundRecorder
    ) -> None:
        turn_id = await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon(source="user", scope="turn")
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)

        requested = bound.last(EventType.INTERRUPT_REQUESTED)
        # Its ack is not awaited, but its sequence is taken where it is
        # recorded: the log still says the interrupt came before everything
        # the interrupt then ended.
        assert requested.sequence < bound.last(EventType.INTERRUPT_OBSERVED).sequence
        assert requested.sequence < bound.last(EventType.TURN_FINISHED).sequence
        assert requested.turn_id == turn_id
        assert requested.actor == bound.trajectory.main_actor
        assert requested.payload["source"] == "user"
        assert requested.payload["scope"] == "turn"
        assert "target_operation_id" not in requested.payload
        assert requested.payload["target_turn_id"] == turn_id
        assert requested.payload["reason_code"] == "user_interrupt"
        assert requested.payload["requested_at"] == requested.occurred_at

        observed = bound.last(EventType.INTERRUPT_OBSERVED)
        assert observed.turn_id == turn_id
        assert "target_operation_id" not in observed.payload
        assert observed.payload["target_turn_id"] == turn_id
        assert isinstance(observed.payload["observed_after_ms"], int)
        assert observed.payload["observed_after_ms"] >= 0
        assert observed.measurements["/payload/observed_after_ms"] == {
            "source": MeasurementSource.MONOTONIC_CLOCK,
            "method_version": 1,
        }

        finished = bound.last(EventType.TURN_FINISHED)
        assert finished.payload["end_reason"] == TurnEndReason.INTERRUPTED
        # interrupt.requested < interrupt.observed < turn.finished, and the
        # observation shares the close's timestamps.
        assert requested.sequence < observed.sequence < finished.sequence
        assert observed.sequence + 1 == finished.sequence
        assert observed.payload["observed_at"] == finished.payload["closed_at"]
        assert observed.monotonic_ns == finished.monotonic_ns
        assert observed.monotonic_ns >= requested.monotonic_ns

    @pytest.mark.asyncio
    async def test_interrupt_requested_defaults(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        await bound.settle()
        requested = bound.last(EventType.INTERRUPT_REQUESTED)
        assert requested.payload["source"] == "user"
        assert requested.payload["scope"] == "turn"

    @pytest.mark.asyncio
    async def test_interrupt_requested_outside_a_turn_is_a_no_op(self, bound: BoundRecorder) -> None:
        bound.recorder.interrupt_requested_soon()
        assert not bound.events_path.exists()
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        bound.recorder.interrupt_requested_soon()
        assert bound.of_type(EventType.INTERRUPT_REQUESTED) == []

    @pytest.mark.asyncio
    async def test_repeated_requests_are_all_recorded_but_observed_once_from_the_first(
        self, bound: BoundRecorder
    ) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        bound.recorder.interrupt_requested_soon(source="user", scope="turn")
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        requested = bound.of_type(EventType.INTERRUPT_REQUESTED)
        assert len(requested) == 2
        observed = bound.of_type(EventType.INTERRUPT_OBSERVED)
        assert len(observed) == 1
        assert observed[0].monotonic_ns >= requested[0].monotonic_ns

    @pytest.mark.asyncio
    async def test_finish_without_interrupt_emits_no_observed(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        assert bound.of_type(EventType.INTERRUPT_OBSERVED) == []
        assert bound.last(EventType.TURN_FINISHED).payload["end_reason"] == TurnEndReason.INTERRUPTED

    @pytest.mark.asyncio
    async def test_a_close_interrupted_in_the_observation_still_ends_the_turn(
        self, bound: BoundRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The turn is marked closed only once its terminal line has nothing
        left to await: marked any earlier, the shutdown repair below skips a
        turn that never got one."""
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        real_emit = bound.trajectory.emit

        async def _cancel_after_the_observation(draft: Any, **kwargs: Any) -> Any:
            result = await real_emit(draft, **kwargs)
            if draft.event_type == EventType.INTERRUPT_OBSERVED:
                raise asyncio.CancelledError
            return result

        monkeypatch.setattr(bound.trajectory, "emit", _cancel_after_the_observation)
        with pytest.raises(asyncio.CancelledError):
            await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        monkeypatch.setattr(bound.trajectory, "emit", real_emit)

        await bound.recorder.close()

        assert len(bound.of_type(EventType.INTERRUPT_OBSERVED)) == 1
        assert bound.last(EventType.TURN_FINISHED).payload["end_reason"] == TurnEndReason.PROCESS_EXIT

    @pytest.mark.asyncio
    async def test_request_followed_by_non_interrupted_finish_emits_no_observed(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        assert len(bound.of_type(EventType.INTERRUPT_REQUESTED)) == 1
        assert bound.of_type(EventType.INTERRUPT_OBSERVED) == []

    @pytest.mark.asyncio
    async def test_pending_request_does_not_leak_into_the_next_turn(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        # Consumed by a non-interrupted close ...
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        # ... so an interrupted close of the next turn has nothing to observe.
        await bound.start_turn(2)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        assert bound.of_type(EventType.INTERRUPT_OBSERVED) == []

    @pytest.mark.asyncio
    async def test_observed_after_an_interrupted_turn_does_not_repeat_on_the_next(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        await bound.start_turn(2)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        observed = bound.of_type(EventType.INTERRUPT_OBSERVED)
        assert len(observed) == 1
        assert observed[0].turn_id == bound.of_type(EventType.TURN_STARTED)[0].turn_id


# ------------------------------------------------------- 4. suspend / resume


class TestSuspendResume:
    @pytest.mark.asyncio
    async def test_suspend_and_resume_emit_with_turn_id_and_keep_the_turn_current(self, bound: BoundRecorder) -> None:
        turn_id = await bound.start_turn(1)
        await bound.recorder.turn_suspended()
        trace = bound.recorder.current_turn
        assert trace is not None and trace.suspended is True and trace.finished is False
        assert bound.recorder.current_turn_id == turn_id

        suspended = bound.last(EventType.TURN_SUSPENDED)
        assert suspended.turn_id == turn_id
        assert suspended.payload == {"turn_id": turn_id, "reason": TurnSuspendReason.AWAITING_SUB_AGENTS}

        await bound.recorder.turn_resumed()
        trace = bound.recorder.current_turn
        assert trace is not None and trace.suspended is False and trace.finished is False
        assert bound.recorder.current_turn_id == turn_id
        resumed = bound.last(EventType.TURN_RESUMED)
        assert resumed.turn_id == turn_id
        assert resumed.payload == {"turn_id": turn_id}
        assert suspended.sequence < resumed.sequence
        assert bound.of_type(EventType.TURN_FINISHED) == []

    @pytest.mark.asyncio
    async def test_suspend_is_idempotent_and_resume_needs_a_suspension(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_resumed()
        assert bound.of_type(EventType.TURN_RESUMED) == []
        await bound.recorder.turn_suspended()
        await bound.recorder.turn_suspended()
        assert len(bound.of_type(EventType.TURN_SUSPENDED)) == 1
        await bound.recorder.turn_resumed()
        await bound.recorder.turn_resumed()
        assert len(bound.of_type(EventType.TURN_RESUMED)) == 1

    @pytest.mark.asyncio
    async def test_suspend_resume_without_a_turn_or_after_finish_are_no_ops(self, bound: BoundRecorder) -> None:
        await bound.recorder.turn_suspended()
        await bound.recorder.turn_resumed()
        assert not bound.events_path.exists()
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.turn_suspended()
        await bound.recorder.turn_resumed()
        assert bound.of_type(EventType.TURN_SUSPENDED) == []
        assert bound.of_type(EventType.TURN_RESUMED) == []

    @pytest.mark.asyncio
    async def test_finishing_a_suspended_turn_is_allowed(self, bound: BoundRecorder) -> None:
        turn_id = await bound.start_turn(1)
        await bound.recorder.turn_suspended()
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        assert bound.recorder.current_turn_id is None
        assert bound.last(EventType.TURN_FINISHED).turn_id == turn_id


# ---------------------------------------------------------------- 5. checkpoint


class TestCheckpoint:
    @pytest.mark.asyncio
    async def test_checkpoint_emits_trajectory_checkpoint_when_active(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        before = bound.events()
        await bound.recorder.checkpoint()
        checkpoint = bound.last(EventType.CHECKPOINT)
        assert checkpoint.sequence == before[-1].sequence + 1
        assert checkpoint.payload["last_assigned"] == checkpoint.sequence

    @pytest.mark.asyncio
    async def test_checkpoint_before_activation_does_not_activate(self, bound: BoundRecorder) -> None:
        await bound.recorder.checkpoint()
        assert not bound.trajectory.is_active
        assert not trajectory_dir(bound.session_dir).exists()


# ---------------------------------------------------------- 6. mutation summary


class TestMutationSummary:
    @pytest.mark.asyncio
    async def test_summary_counts_only_and_no_paths(self, bound: BoundRecorder, tmp_path: Path) -> None:
        turn_id = await bound.start_turn(1)
        secret_dir = str(tmp_path / "very-secret-project")
        summary = {
            f"{secret_dir}/created.py": _diff(None, "h-created"),
            f"{secret_dir}/deleted.py": _diff("h-deleted", None),
            f"{secret_dir}/modified.py": _diff("h-before", "h-after"),
            f"{secret_dir}/net-zero.py": _diff("h-same", "h-same"),
            f"{secret_dir}/inferred.py": _diff("h-x", "h-y", inferred=True),
            f"{secret_dir}/skipped.bin": _diff("h-z", None, after_skip=SnapshotSkipReason.TOO_LARGE),
        }
        await bound.recorder.mutation_summary(summary, checkpoint=None)

        event = bound.last(EventType.TOOL_MUTATION_BATCH_SUMMARY)
        assert event.turn_id == turn_id
        assert event.operation_id is not None and is_valid_analytics_id(event.operation_id)
        payload = dict(event.payload)
        derived_at = payload.pop("derived_at")
        assert isinstance(derived_at, str) and derived_at.endswith("Z")
        assert payload == {
            "turn_id": turn_id,
            "files_touched": 6,
            "create": 1,
            "modify": 4,  # modified + net-zero + inferred + skipped (exists → exists)
            "delete": 1,
            "net_zero_count": 1,
            "proven_count": 4,
            "assumed_count": 2,  # inferred + content_unavailable
        }
        assert "source_ref" not in payload
        assert event.measurements["/payload/files_touched"] == {
            "source": MeasurementSource.DERIVED_FROM_SESSION,
            "method_version": 1,
        }
        _assert_no_substring(event.to_dict(), "very-secret-project")
        _assert_no_substring(event.to_dict(), ".py")
        _assert_no_substring(event.to_dict(), "h-before")

    @pytest.mark.asyncio
    async def test_summary_with_checkpoint_carries_session_checkpoint_source_ref(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        checkpoint = SessionCheckpoint(session_checkpoint_id="0f" * 16, content_hash="ab" * 32)
        await bound.recorder.mutation_summary({"a.txt": _diff(None, "h1")}, checkpoint=checkpoint)
        payload = bound.last(EventType.TOOL_MUTATION_BATCH_SUMMARY).payload
        assert payload["source_ref"] == {
            "kind": SourceRefKind.SESSION_CHECKPOINT,
            "id": "0f" * 16,
            "hash": "ab" * 32,
        }
        assert payload["files_touched"] == 1
        assert payload["create"] == 1

    @pytest.mark.asyncio
    async def test_summary_with_blank_checkpoint_id_omits_source_ref(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        checkpoint = SessionCheckpoint(session_checkpoint_id="", content_hash="ab" * 32)
        await bound.recorder.mutation_summary({"a.txt": _diff(None, "h1")}, checkpoint=checkpoint)
        assert "source_ref" not in bound.last(EventType.TOOL_MUTATION_BATCH_SUMMARY).payload

    @pytest.mark.asyncio
    async def test_empty_summary_or_no_turn_emits_nothing(self, bound: BoundRecorder) -> None:
        await bound.recorder.mutation_summary({"a.txt": _diff(None, "h1")}, checkpoint=None)
        assert not bound.events_path.exists()
        await bound.start_turn(1)
        await bound.recorder.mutation_summary({}, checkpoint=None)
        assert bound.of_type(EventType.TOOL_MUTATION_BATCH_SUMMARY) == []

    @pytest.mark.asyncio
    async def test_summary_after_turn_finished_still_names_that_turn(self, bound: BoundRecorder) -> None:
        # The finalizer derives the summary after the turn closed; the trace is
        # still held until the next turn starts.
        turn_id = await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.mutation_summary({"a.txt": _diff(None, "h1")}, checkpoint=None)
        event = bound.last(EventType.TOOL_MUTATION_BATCH_SUMMARY)
        assert event.turn_id == turn_id
        assert event.payload["turn_id"] == turn_id


# ------------------------------------------------------------ 7. profile switch


class TestProfileSwitched:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", [ProfileKind.AGENT, ProfileKind.MODEL])
    async def test_profile_switched_emits_kind_and_fingerprints(self, bound: BoundRecorder, kind: str) -> None:
        await bound.recorder.profile_switched(kind=kind, from_fingerprint="fp-old", to_fingerprint="fp-new")
        event = bound.last(EventType.PROFILE_SWITCHED)
        assert event.actor == bound.trajectory.main_actor
        assert event.turn_id is None
        assert event.payload == {"kind": kind, "from_fingerprint": "fp-old", "to_fingerprint": "fp-new"}

    @pytest.mark.asyncio
    async def test_profile_switched_inside_a_turn_carries_the_turn_id(self, bound: BoundRecorder) -> None:
        turn_id = await bound.start_turn(1)
        await bound.recorder.profile_switched(kind=ProfileKind.MODEL, from_fingerprint="a", to_fingerprint="b")
        assert bound.last(EventType.PROFILE_SWITCHED).turn_id == turn_id

    @pytest.mark.asyncio
    async def test_same_fingerprint_or_unknown_kind_emits_nothing(self, bound: BoundRecorder) -> None:
        await bound.recorder.profile_switched(kind=ProfileKind.MODEL, from_fingerprint="same", to_fingerprint="same")
        await bound.recorder.profile_switched(kind="skill", from_fingerprint="a", to_fingerprint="b")
        assert not bound.events_path.exists()


# ------------------------------------------------------------------ 8. rollback


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_opens_a_new_branch_and_supersedes_the_old_one(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        turn2 = await bound.start_turn(2)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.start_turn(3)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        old_branch = bound.trajectory.branch_id
        assert old_branch is not None
        superseded_turns, target_turns = bound.rollback_to(2)
        assert [record.turn_number for record in target_turns] == [1, 2]
        assert [record.turn_number for record in superseded_turns] == [3]

        await bound.recorder.rollback(superseded_turns=superseded_turns, target_turns=target_turns)

        new_branch = bound.trajectory.branch_id
        assert new_branch is not None
        assert new_branch != old_branch
        assert is_valid_analytics_id(new_branch) and is_valid_analytics_id(old_branch)

        rollback = bound.last(EventType.SESSION_ROLLBACK)
        assert rollback.branch_id == new_branch
        assert rollback.turn_id is None
        assert rollback.payload["old_branch_id"] == old_branch
        assert rollback.payload["new_branch_id"] == new_branch
        assert rollback.payload["reason_code"] == ROLLBACK_REASON_USER
        assert rollback.payload["target_turn_id"] == turn2
        assert rollback.payload["superseded_from_sequence"] == superseded_turns[0].started_sequence
        assert rollback.payload["superseded_to_sequence"] == rollback.sequence - 1

        superseded = bound.last(EventType.BRANCH_SUPERSEDED)
        assert superseded.sequence == rollback.sequence + 1
        assert superseded.branch_id == new_branch
        assert superseded.payload == {"branch_id": old_branch, "superseded_by": new_branch}

        # Every event before the rollback carried the old branch; everything after, the new one.
        events = bound.events()
        assert {event.branch_id for event in events if event.sequence < rollback.sequence} == {old_branch}
        assert {event.branch_id for event in events if event.sequence >= rollback.sequence} == {new_branch}

    @pytest.mark.asyncio
    async def test_retirement_records_its_reason_and_the_same_paired_branch_transition(
        self, bound: BoundRecorder
    ) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.start_turn(2)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        superseded, kept = bound.rollback_to(1)
        old_branch = bound.trajectory.branch_id

        await bound.recorder.retire_turn(retired_turn=superseded[0], target_turns=kept)

        retirement = bound.last(EventType.SESSION_ROLLBACK)
        paired = bound.last(EventType.BRANCH_SUPERSEDED)
        assert retirement.payload["reason_code"] == ROLLBACK_REASON_FRESH_TURN_RETIRED
        assert all(event.payload.get("reason_code") != ROLLBACK_REASON_USER for event in bound.events())
        assert retirement.payload["superseded_from_sequence"] == superseded[0].started_sequence
        assert retirement.payload["superseded_to_sequence"] == retirement.sequence - 1
        assert retirement.payload["target_turn_id"] == kept[0].turn_id
        assert paired.sequence == retirement.sequence + 1
        assert paired.payload == {"branch_id": old_branch, "superseded_by": retirement.branch_id}
        assert_trajectory_accounted(read_trajectory(bound.events_path))

    @pytest.mark.asyncio
    async def test_retiring_a_turn_this_log_never_ran_records_nothing(self, bound: BoundRecorder) -> None:
        # A fork inherits its parent's last turn without its events: there is no range to supersede.
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        _, kept = bound.rollback_to(1)
        before = bound.events()
        branch = bound.trajectory.branch_id

        await bound.recorder.retire_turn(retired_turn=TurnRecord(turn_number=2, turn_id="inherited"), target_turns=kept)

        assert bound.events() == before
        assert bound.trajectory.branch_id == branch

    @pytest.mark.asyncio
    async def test_a_rollback_before_the_first_event_opens_the_branch_it_announces(self, bound: BoundRecorder) -> None:
        # The log is still cold: it opens inside the rollback itself, and the
        # branch that opening picks must not replace the one just announced.
        assert not bound.events_path.exists()

        await bound.recorder.rollback(superseded_turns=[], target_turns=[])

        new_branch = bound.trajectory.branch_id
        rollback = bound.last(EventType.SESSION_ROLLBACK)
        assert rollback.payload["new_branch_id"] == new_branch
        assert rollback.branch_id == new_branch
        old_branch = rollback.payload["old_branch_id"]
        assert old_branch != new_branch
        assert bound.last(EventType.BRANCH_SUPERSEDED).payload["branch_id"] == old_branch
        # The prelude that opening wrote belongs to the branch it superseded.
        assert {event.branch_id for event in bound.events() if event.sequence < rollback.sequence} == {old_branch}

    @pytest.mark.asyncio
    async def test_subsequent_events_are_stamped_with_the_new_branch(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.rollback(superseded_turns=[], target_turns=[])
        new_branch = bound.trajectory.branch_id
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        rollback = bound.last(EventType.SESSION_ROLLBACK)
        after = [event for event in bound.events() if event.sequence > rollback.sequence]
        assert {event.event_type for event in after} >= {
            EventType.BRANCH_SUPERSEDED,
            EventType.TURN_STARTED,
            EventType.INTERRUPT_REQUESTED,
            EventType.INTERRUPT_OBSERVED,
            EventType.TURN_FINISHED,
        }
        assert {event.branch_id for event in after} == {new_branch}

    @pytest.mark.asyncio
    async def test_rollback_to_turn_zero_names_no_target_turn(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        superseded_turns, target_turns = bound.rollback_to(None)
        assert [record.turn_number for record in superseded_turns] == [1]

        await bound.recorder.rollback(superseded_turns=superseded_turns, target_turns=target_turns)

        payload = bound.last(EventType.SESSION_ROLLBACK).payload
        assert "target_turn_id" not in payload
        assert payload["superseded_from_sequence"] == superseded_turns[0].started_sequence

    @pytest.mark.asyncio
    async def test_rollback_without_registry_omits_sequence_and_target_fields(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1, history_state=None)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.rollback(superseded_turns=[], target_turns=[])
        payload = bound.last(EventType.SESSION_ROLLBACK).payload
        assert set(payload) == {"new_branch_id", "old_branch_id", "reason_code"}

    @pytest.mark.asyncio
    async def test_rollback_abandons_the_traced_turn_and_pending_interrupt(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        await bound.recorder.rollback(superseded_turns=[], target_turns=[])
        assert bound.recorder.current_turn is None
        assert bound.recorder.current_turn_id is None
        # The superseded turn is never closed by the recorder ...
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        assert bound.of_type(EventType.TURN_FINISHED) == []
        assert bound.of_type(EventType.INTERRUPT_OBSERVED) == []
        # ... and the next turn's interrupted close observes nothing stale.
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        assert bound.of_type(EventType.INTERRUPT_OBSERVED) == []

    @pytest.mark.asyncio
    async def test_started_turn_is_appended_with_its_opening_identity(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1, opening_item_id="item-a")
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.start_turn(2, opening_item_id="item-b")

        records = turn_records(bound.history_state)
        assert [record.turn_number for record in records] == [1, 2]
        assert records[0].opening_item_id == "item-a"
        assert records[1].opening_item_id == "item-b"

    def test_append_keeps_a_turn_that_reuses_an_ordinal(self) -> None:
        # An ordinal drifts once a failed turn leaves the saved counter behind:
        # the next turn reuses it, and both identities have to survive.
        state: dict[str, Any] = {}
        append_turn(state, TurnRecord(turn_number=1, turn_id="aaa", started_sequence=3))
        append_turn(state, TurnRecord(turn_number=1, turn_id="bbb"))

        records = turn_records(state)
        assert [(record.turn_number, record.turn_id) for record in records] == [(1, "aaa"), (1, "bbb")]
        assert records[0].started_sequence == 3
        assert records[1].started_sequence is None

    def test_discard_empty_turn_ignores_a_malformed_registry_tail(self) -> None:
        state: dict[str, Any] = {}
        append_turn(state, TurnRecord(turn_number=1, turn_id="aaa"))
        target = TurnRecord(turn_number=2, turn_id="bbb")
        append_turn(state, target)
        malformed = {"turn_id": "invalid-without-number"}
        state[TRAJECTORY_STATE_KEY]["turn_log"].append(malformed)

        assert discard_last_turn(state, turn_number=2) == target
        assert turn_records(state) == [TurnRecord(turn_number=1, turn_id="aaa")]
        assert state[TRAJECTORY_STATE_KEY]["turn_log"][-1] == malformed
        assert discard_last_turn(state, turn_number=2) is None

    def test_mark_turn_sequence_only_fills_the_matching_turn_and_only_once(self) -> None:
        state: dict[str, Any] = {}
        append_turn(state, TurnRecord(turn_number=1, turn_id="aaa"))
        append_turn(state, TurnRecord(turn_number=2, turn_id="bbb"))

        mark_turn_sequence(state, "nope", 7)
        assert [record.started_sequence for record in turn_records(state)] == [None, None]

        mark_turn_sequence(state, "bbb", 9)
        assert turn_records(state)[1].started_sequence == 9

        # A retry lands later in the log; the range still opens where the turn did.
        mark_turn_sequence(state, "bbb", 20)
        assert turn_records(state)[1].started_sequence == 9

    def test_mark_turn_sequence_matches_identity_not_the_ordinal(self) -> None:
        # Two turns share ordinal 1 — a failed one and the turn that reused its
        # ordinal — so the ordinal names neither of them reliably. The turn being
        # marked was appended last, and it is the one that has to be filled.
        state: dict[str, Any] = {}
        append_turn(state, TurnRecord(turn_number=1, turn_id="aaa"))
        append_turn(state, TurnRecord(turn_number=1, turn_id="bbb"))

        mark_turn_sequence(state, "aaa", 3)

        records = turn_records(state)
        assert [(record.turn_id, record.started_sequence) for record in records] == [("aaa", 3), ("bbb", None)]

    def test_mark_turn_sequence_rewrites_a_legacy_registry_in_place(self) -> None:
        state: dict[str, Any] = {TRAJECTORY_STATE_KEY: {"turns": {"1": {"turn_id": "aaa"}}}}
        mark_turn_sequence(state, "aaa", 4)

        registry = state[TRAJECTORY_STATE_KEY]
        assert "turns" not in registry
        assert registry["turn_log"] == [{"turn_number": 1, "turn_id": "aaa", "started_sequence": 4}]

    def test_legacy_ordinal_registry_reads_as_a_log_and_rewrites_on_write(self) -> None:
        state: dict[str, Any] = {
            TRAJECTORY_STATE_KEY: {
                "turns": {
                    "2": {"turn_id": "bbb", "started_sequence": 8},
                    "1": {"turn_id": "aaa", "started_sequence": 2},
                }
            }
        }
        assert [(record.turn_number, record.turn_id) for record in turn_records(state)] == [(1, "aaa"), (2, "bbb")]
        # Reading alone leaves the stored layout untouched.
        assert "turn_log" not in state[TRAJECTORY_STATE_KEY]

        append_turn(state, TurnRecord(turn_number=3, turn_id="ccc"))

        registry = state[TRAJECTORY_STATE_KEY]
        assert "turns" not in registry
        assert [entry["turn_id"] for entry in registry["turn_log"]] == ["aaa", "bbb", "ccc"]
        assert registry["turn_log"][0]["started_sequence"] == 2

    def test_legacy_turn_records_tell_the_two_layouts_apart(self) -> None:
        legacy = {TRAJECTORY_STATE_KEY: {"turns": {"1": {"turn_id": "aaa", "started_sequence": 2}}}}
        assert [(record.turn_number, record.turn_id) for record in legacy_turn_records(legacy)] == [(1, "aaa")]

        # The append layout is not a legacy one, and neither is a state that
        # carries no registry at all: a caller judging a stored turn against the
        # ordinal it reads gets ``None`` rather than a registry it cannot trust.
        assert legacy_turn_records({TRAJECTORY_STATE_KEY: {"turn_log": [{"turn_number": 1, "turn_id": "aaa"}]}}) is None
        assert legacy_turn_records({}) is None
        assert legacy_turn_records(None) is None

    def test_forks_and_sequence_writes_preserve_legacy_retry_validation(self) -> None:
        state: dict[str, Any] = {TRAJECTORY_STATE_KEY: {"turns": {"1": {"turn_id": "aaa"}}}}

        fork_turn_registry(state, origin_session_id="parent")
        mark_turn_sequence(state, "aaa", 4)
        fork_turn_registry(state, origin_session_id="child")

        assert "turns" not in state[TRAJECTORY_STATE_KEY]
        assert legacy_turn_records(state) == [TurnRecord(turn_number=1, turn_id="aaa", origin_session_id="parent")]

    @pytest.mark.parametrize("admission", ["fresh", "retry"])
    def test_admission_makes_a_converted_registry_trustworthy(self, admission: str) -> None:
        state: dict[str, Any] = {TRAJECTORY_STATE_KEY: {"turns": {"1": {"turn_id": "aaa"}}}}
        fork_turn_registry(state, origin_session_id="parent")

        if admission == "fresh":
            append_turn(state, TurnRecord(turn_number=2, turn_id="bbb"))
        else:
            confirm_resumed_turn(state)
        fork_turn_registry(state, origin_session_id="child")

        assert legacy_turn_records(state) is None
        assert [record.turn_id for record in turn_records(state)] == (
            ["aaa", "bbb"] if admission == "fresh" else ["aaa"]
        )

    def test_malformed_entries_are_dropped_and_fields_validated(self) -> None:
        assert turn_records(None) == []
        assert turn_records({}) == []
        assert turn_records({TRAJECTORY_STATE_KEY: "bogus"}) == []
        state: dict[str, Any] = {
            TRAJECTORY_STATE_KEY: {
                "turn_log": [
                    {"turn_number": 1, "turn_id": 5},
                    {"turn_number": True, "turn_id": "bool-ordinal"},
                    {"turn_number": -1, "turn_id": "negative"},
                    {"turn_number": 2, "turn_id": ""},
                    "not-a-dict",
                    {
                        "turn_number": 3,
                        "turn_id": "kept",
                        "started_sequence": True,
                        "opening_item_id": 7,
                        "user_message_id": "",
                    },
                ]
            }
        }

        records = turn_records(state)
        assert [(record.turn_number, record.turn_id) for record in records] == [(3, "kept")]
        # A bool is not an int, and a non-str identity field becomes absent.
        assert records[0].started_sequence is None
        assert records[0].opening_item_id is None
        assert records[0].user_message_id is None

    def test_fork_clears_sequences_and_keeps_the_original_origin(self) -> None:
        state: dict[str, Any] = {}
        append_turn(
            state,
            TurnRecord(turn_number=1, turn_id="aaa", started_sequence=2, origin_session_id="grandparent"),
        )
        append_turn(state, TurnRecord(turn_number=2, turn_id="bbb", started_sequence=9, opening_item_id="item-b"))

        fork_turn_registry(state, origin_session_id="parent")

        forked = turn_records(state)
        # The parent's log sequences would name a range that never existed here.
        assert [record.started_sequence for record in forked] == [None, None]
        assert [record.origin_session_id for record in forked] == ["grandparent", "parent"]
        # Everything else about the turn rides along untouched.
        assert [(record.turn_number, record.turn_id) for record in forked] == [(1, "aaa"), (2, "bbb")]
        assert forked[1].opening_item_id == "item-b"

    def test_a_rerun_turn_claims_a_sequence_of_its_own(self) -> None:
        state: dict[str, Any] = {}
        append_turn(state, TurnRecord(turn_number=1, turn_id="aaa"))
        fork_turn_registry(state, origin_session_id="parent")

        mark_turn_sequence(state, "aaa", 5)

        record = turn_records(state)[0]
        assert record.started_sequence == 5
        assert record.origin_session_id == "parent"


# --------------------------------------------------------------------- 9. fork


class TestFork:
    @pytest.mark.asyncio
    async def test_fork_writes_session_forked_into_the_child_log(self, bound: BoundRecorder, tmp_path: Path) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        origin_last = bound.trajectory.last_assigned_sequence()
        assert origin_last == bound.events()[-1].sequence

        fork_dir = tmp_path / "fork"
        fork_dir.mkdir()
        await bound.recorder.fork(
            origin_session_id=SESSION_ID,
            fork_session_id=FORK_SESSION_ID,
            fork_session_dir=fork_dir,
            fork_write_lock_path=tmp_path / "fork.write.lock",
            session_start_info=_start_info_factory(bound.primary_cwd)(),
        )

        result = read_trajectory(trajectory_events_path(fork_dir))
        assert result.corrupt_lines == [] and result.torn_tail_bytes == 0
        types = [event.event_type for event in result.events]
        assert types == [
            EventType.COVERAGE_STARTED,
            EventType.RUNTIME_STARTED,
            EventType.SESSION_STARTED,
            EventType.SESSION_FORKED,
            EventType.COVERAGE_ENDED,
            EventType.RUNTIME_FINISHED,
        ]
        assert {event.session_id for event in result.events} == {FORK_SESSION_ID}
        forked = result.events[3]
        assert forked.payload == {"origin_session_id": SESSION_ID, "forked_at_sequence": origin_last}
        assert forked.actor.kind == ActorKind.AGENT and forked.actor.role == ActorRole.MAIN
        assert forked.actor != bound.trajectory.main_actor  # derived from the fork's own session id
        assert result.events[-1].payload["reason"] == RuntimeFinishReason.SESSION_SWITCH
        assert_trajectory_accounted(result)

        # The origin's recorder is untouched: same trajectory, nothing new in its log.
        assert bound.recorder.trajectory is bound.trajectory
        assert bound.trajectory.last_assigned_sequence() == origin_last
        assert bound.of_type(EventType.SESSION_FORKED) == []

    @pytest.mark.asyncio
    async def test_fork_before_origin_activation_reports_sequence_zero(
        self, bound: BoundRecorder, tmp_path: Path
    ) -> None:
        fork_dir = tmp_path / "fork"
        fork_dir.mkdir()
        await bound.recorder.fork(
            origin_session_id=SESSION_ID,
            fork_session_id=FORK_SESSION_ID,
            fork_session_dir=fork_dir,
            fork_write_lock_path=None,
            session_start_info=_start_info_factory(bound.primary_cwd)(),
        )
        forked = last_event_of_type(read_trajectory(trajectory_events_path(fork_dir)).events, EventType.SESSION_FORKED)
        assert forked is not None
        assert forked.payload["forked_at_sequence"] == 0
        assert not bound.trajectory.is_active


# --------------------------------------------------------------------- 10. close


class TestClose:
    @pytest.mark.asyncio
    async def test_close_finishes_an_open_turn_with_process_exit_then_runtime_finished_last(
        self, bound: BoundRecorder
    ) -> None:
        turn_id = await bound.start_turn(1)
        trajectory = bound.trajectory
        await bound.recorder.close()

        events = bound.events()
        assert events[-1].event_type == EventType.RUNTIME_FINISHED
        assert events[-1].payload["reason"] == RuntimeFinishReason.GRACEFUL_SHUTDOWN
        assert events[-2].event_type == EventType.COVERAGE_ENDED
        finished = bound.last(EventType.TURN_FINISHED)
        assert finished.turn_id == turn_id
        assert finished.payload["end_reason"] == TurnEndReason.PROCESS_EXIT
        assert finished.sequence < events[-2].sequence

        assert trajectory.is_closed
        assert bound.recorder.trajectory is None
        assert bound.recorder.current_turn is None
        assert bound.recorder.current_turn_id is None
        assert bound.recorder.context() is None

    @pytest.mark.asyncio
    async def test_close_for_session_switch_cancels_an_open_turn(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.close(reason=RuntimeFinishReason.SESSION_SWITCH)
        events = bound.events()
        assert bound.last(EventType.TURN_FINISHED).payload["end_reason"] == TurnEndReason.CANCELLED
        assert events[-1].event_type == EventType.RUNTIME_FINISHED
        assert events[-1].payload["reason"] == RuntimeFinishReason.SESSION_SWITCH

    @pytest.mark.asyncio
    async def test_close_with_a_finished_turn_adds_no_turn_finished(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.close()
        finished = bound.of_type(EventType.TURN_FINISHED)
        assert len(finished) == 1
        assert finished[0].payload["end_reason"] == TurnEndReason.COMPLETED
        assert bound.events()[-1].event_type == EventType.RUNTIME_FINISHED

    @pytest.mark.asyncio
    async def test_a_turn_whose_start_was_cancelled_is_not_closed_by_the_recorder(
        self, bound: BoundRecorder, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupt or shutdown can land while the opening line is still on
        its way to the log. Here nothing ever gave that line a sequence — the
        shape a thread call cancelled before its pool ran it leaves behind —
        so the turn does not exist and the close must not answer it with a
        terminal: a reader handles an unclosed span, but a terminal with no
        start is a shape nothing makes."""
        await bound.trajectory.ensure_active()
        emit = bound.trajectory.emit
        interrupt_next = True

        async def _cancel_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal interrupt_next
            if interrupt_next:
                interrupt_next = False
                raise asyncio.CancelledError
            return await emit(*args, **kwargs)

        monkeypatch.setattr(bound.trajectory, "emit", _cancel_once)
        with pytest.raises(asyncio.CancelledError):
            await bound.start_turn(1)
        # Held until the close decides: an opening line can still land after
        # its caller is gone, and only the terminal has to know whether it did.
        assert bound.recorder.current_turn is not None
        await bound.recorder.close()
        assert bound.of_type(EventType.TURN_STARTED) == []
        assert bound.of_type(EventType.TURN_FINISHED) == []

    @pytest.mark.asyncio
    async def test_close_twice_and_calls_after_close_are_no_ops(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        await bound.recorder.close()
        snapshot = bound.events_path.read_bytes()
        await bound.recorder.close()
        assert (
            await bound.recorder.turn_started(
                turn_id=new_analytics_id(),
                logical_turn_id=new_analytics_id(),
                turn_number=2,
                is_retry=False,
                agent_profile_fingerprint=AGENT_FP,
                model_profile_fingerprint=MODEL_FP,
                primary_cwd=bound.primary_cwd,
                history_state=bound.history_state,
            )
            is None
        )
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        bound.recorder.interrupt_requested_soon()
        await bound.recorder.turn_suspended()
        await bound.recorder.turn_resumed()
        await bound.recorder.checkpoint()
        await bound.recorder.mutation_summary({"a": _diff(None, "h")}, checkpoint=None)
        await bound.recorder.profile_switched(kind=ProfileKind.AGENT, from_fingerprint="a", to_fingerprint="b")
        await bound.recorder.rollback(superseded_turns=[], target_turns=[])
        assert bound.events_path.read_bytes() == snapshot
        assert bound.recorder.trajectory is None

    @pytest.mark.asyncio
    async def test_close_of_a_never_activated_session_materializes_nothing(self, bound: BoundRecorder) -> None:
        trajectory = bound.trajectory
        await bound.recorder.close()
        assert trajectory.is_closed
        assert bound.recorder.trajectory is None
        assert not trajectory_dir(bound.session_dir).exists()

    @pytest.mark.asyncio
    async def test_a_cancel_on_the_open_turn_terminal_still_closes_the_runtime(self, bound: BoundRecorder) -> None:
        """The terminal and the close behind it are one step: the close returns the lease."""
        await bound.start_turn(1)
        trajectory = bound.trajectory
        gate = asyncio.Event()
        entered = asyncio.Event()
        real_emit = trajectory.emit

        async def _slow_emit(draft: Any, **kwargs: Any) -> Any:
            if draft.event_type == EventType.TURN_FINISHED:
                entered.set()
                await gate.wait()
            return await real_emit(draft, **kwargs)

        trajectory.emit = _slow_emit  # type: ignore[method-assign]

        cancelled_caller = asyncio.ensure_future(bound.recorder.close(reason=RuntimeFinishReason.SESSION_SWITCH))
        assert await wait_until(entered.is_set, timeout=ENGINE_TURN_TIMEOUT)
        cancelled_caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_caller

        gate.set()
        # Nobody is waiting on it any more, and it still has to finish: the
        # cancelled caller is a shutdown that will not come back to retry.
        assert await wait_until(lambda: trajectory.is_closed, timeout=ENGINE_TURN_TIMEOUT)
        # A later caller waits on that same close rather than starting another.
        await bound.recorder.close(reason=RuntimeFinishReason.SESSION_SWITCH)

        assert bound.recorder.trajectory is None
        events = bound.events()
        assert events[-1].event_type == EventType.RUNTIME_FINISHED
        assert bound.last(EventType.TURN_FINISHED).payload["end_reason"] == TurnEndReason.CANCELLED
        writer = trajectory.writer
        assert writer is not None and writer.thread_alive is False
        assert WriterLease.is_held_elsewhere(bound.session_dir.parent / "sess.write.lock") is False

    @pytest.mark.asyncio
    async def test_close_when_the_trajectory_was_closed_underneath_skips_the_turn_close(
        self, bound: BoundRecorder
    ) -> None:
        await bound.start_turn(1)
        await bound.trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
        await bound.recorder.close()
        assert bound.of_type(EventType.TURN_FINISHED) == []
        assert bound.recorder.trajectory is None
        assert bound.recorder.current_turn is None


# ---------------------------------------------------------------- 11. ordering


class TestOrderingInvariants:
    @pytest.mark.asyncio
    async def test_sequences_are_contiguous_and_identity_is_uniform_across_a_full_scenario(
        self, bound: BoundRecorder
    ) -> None:
        await bound.recorder.profile_switched(kind=ProfileKind.MODEL, from_fingerprint="m0", to_fingerprint="m1")
        await bound.start_turn(1)
        await bound.recorder.turn_suspended()
        await bound.recorder.turn_resumed()
        await bound.recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)
        await bound.recorder.mutation_summary({"a": _diff(None, "h")}, checkpoint=None)
        await bound.recorder.checkpoint()
        await bound.start_turn(2)
        bound.recorder.interrupt_requested_soon()
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        superseded_turns, target_turns = bound.rollback_to(1)
        await bound.recorder.rollback(superseded_turns=superseded_turns, target_turns=target_turns)
        assert [record.turn_number for record in superseded_turns] == [2]
        await bound.start_turn(2)
        await bound.recorder.checkpoint()
        await bound.recorder.close()

        events = bound.events()
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert_trajectory_accounted(read_trajectory(bound.events_path))
        assert {event.session_id for event in events} == {SESSION_ID}
        assert len({event.runtime_id for event in events}) == 1
        assert len({event.coverage_id for event in events}) == 1
        assert len({event.event_id for event in events}) == len(events)
        assert all(is_valid_analytics_id(event.event_id) for event in events)
        assert events[0].event_type == EventType.COVERAGE_STARTED
        assert events[0].payload["runtime_id"] == events[0].runtime_id
        assert events[0].payload["coverage_id"] == events[0].coverage_id
        assert events[-1].event_type == EventType.RUNTIME_FINISHED
        # Exactly two branches: before and after the rollback.
        assert len({event.branch_id for event in events}) == 2
        # Drafts capture their boundary when built, so the lazily written
        # prelude may carry later stamps than the event that triggered it;
        # from the first engine event on, the monotonic clock never runs backwards.
        stamps = [event.monotonic_ns for event in events[3:]]
        assert stamps == sorted(stamps)
        expected_types = [
            EventType.COVERAGE_STARTED,
            EventType.RUNTIME_STARTED,
            EventType.SESSION_STARTED,
            EventType.PROFILE_SWITCHED,
            EventType.TURN_STARTED,
            EventType.TURN_SUSPENDED,
            EventType.TURN_RESUMED,
            EventType.TURN_FINISHED,
            EventType.TOOL_MUTATION_BATCH_SUMMARY,
            EventType.CHECKPOINT,
            EventType.TURN_STARTED,
            EventType.INTERRUPT_REQUESTED,
            EventType.INTERRUPT_OBSERVED,
            EventType.TURN_FINISHED,
            EventType.SESSION_ROLLBACK,
            EventType.BRANCH_SUPERSEDED,
            EventType.TURN_STARTED,
            EventType.CHECKPOINT,
            EventType.TURN_FINISHED,
            EventType.COVERAGE_ENDED,
            EventType.RUNTIME_FINISHED,
        ]
        assert [event.event_type for event in events] == expected_types

    @pytest.mark.asyncio
    async def test_main_actor_is_stamped_on_every_engine_event(self, bound: BoundRecorder) -> None:
        await bound.start_turn(1)
        bound.recorder.interrupt_requested_soon()
        await bound.recorder.turn_finished(end_reason=TurnEndReason.INTERRUPTED)
        await bound.recorder.rollback(superseded_turns=[], target_turns=[])
        system_types = {EventType.COVERAGE_STARTED, EventType.RUNTIME_STARTED}
        for event in bound.events():
            if event.event_type in system_types:
                assert event.actor.kind == ActorKind.SYSTEM
            else:
                assert event.actor == bound.trajectory.main_actor


# ----------------------------------------------------------- 12. exchange facts


class TestExchangeFacts:
    def test_without_model_profile_carries_profile_identity_only(self) -> None:
        facts = exchange_facts(
            agent_profile_name="coder",
            agent_profile_fingerprint="afp",
            model_profile=None,
            model_profile_fingerprint="mfp",
        )
        assert facts == {
            "agent_profile_id": "coder",
            "agent_profile_fingerprint": "afp",
            "model_profile_fingerprint": "mfp",
        }

    def test_with_model_profile_adds_provider_facts(self) -> None:
        profile = ModelProfile(
            id="0123456789abcdef0123456789abcdef",
            name="GPT Something",
            provider="openai",
            model_id="gpt-x",
            api_key="sk-should-never-appear",
            base_url="https://example.invalid/v1",
        )
        facts = exchange_facts(
            agent_profile_name="coder",
            agent_profile_fingerprint="afp",
            model_profile=profile,
            model_profile_fingerprint="mfp",
        )
        assert facts == {
            "agent_profile_id": "coder",
            "agent_profile_fingerprint": "afp",
            "model_profile_fingerprint": "mfp",
            "model_profile_id": profile.id,
            "provider": "openai",
            "api_style": profile.api_style,
            "request_model": "gpt-x",
        }
        _assert_no_substring(facts, "sk-should-never-appear")
        _assert_no_substring(facts, "example.invalid")
        _assert_no_substring(facts, "GPT Something")

    def test_empty_fingerprints_are_preserved_but_an_empty_id_is_omitted(self) -> None:
        facts = exchange_facts(
            agent_profile_name="",
            agent_profile_fingerprint="",
            model_profile=None,
            model_profile_fingerprint="",
        )
        # An empty string is not a valid identifier: recording one would make
        # the whole exchange event undecodable, so the field is left out.
        assert facts == {"agent_profile_fingerprint": "", "model_profile_fingerprint": ""}

    def test_an_unconfigured_model_profile_contributes_no_empty_id(self) -> None:
        profile = ModelProfile(id="", name="", provider="unknown", model_id="<Model Not Configured>")
        facts = exchange_facts(
            agent_profile_name="coder",
            agent_profile_fingerprint="afp",
            model_profile=profile,
            model_profile_fingerprint="mfp",
        )
        assert "model_profile_id" not in facts
        assert facts["request_model"] == "<Model Not Configured>"
        assert malformed_id_pointers({"payload": facts}) == []
