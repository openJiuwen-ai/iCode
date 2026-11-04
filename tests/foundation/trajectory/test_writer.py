# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Writer contract tests: write-through, accounted prefix, bounded wait, closure."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import statistics
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation.platform.files import secure_open_owner_only_append
from chrys.foundation.trajectory import writer as writer_module
from chrys.foundation.trajectory.envelope import (
    INT64_MAX,
    LINE_BUDGET_BYTES,
    EventDraft,
    build_event,
    encode_event_line,
)
from chrys.foundation.trajectory.event_types import EventType, GapReason, RuntimeFinishReason
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.foundation.trajectory.sequences import SequenceRange
from chrys.foundation.trajectory.writer import (
    EmitResult,
    FdWriteBackend,
    TrajectoryWriter,
    WriterState,
)
from tests.support.gc_isolation import earlier_tests_heap_frozen
from tests.support.trajectory_invariants import assert_trajectory_accounted
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT as WAIT_TIMEOUT
from tests.support.waiting import wait_until

SESSION_ID = "12345678-1234-1234-1234-123456789abc"


@dataclass
class ScriptedBackend:
    """Real file backend with fault injection for the worker thread."""

    inner: FdWriteBackend
    fail_writes_remaining: int = 0
    fail_fsync: bool = False
    short_write_bytes: int | None = None
    short_write_on_call: int | None = None
    block_writes: threading.Event | None = None
    block_entered: threading.Event = field(default_factory=threading.Event)
    write_calls: int = 0
    fsync_calls: int = 0
    truncate_calls: list[int] = field(default_factory=list)
    closed: bool = False

    def write(self, data: memoryview) -> int:
        self.write_calls += 1
        if self.block_writes is not None:
            self.block_entered.set()
            self.block_writes.wait()
        if self.fail_writes_remaining > 0:
            self.fail_writes_remaining -= 1
            raise OSError("injected write failure")
        if self.short_write_bytes is not None and self.short_write_on_call in (None, self.write_calls):
            # A real short write returns the partial count; the retry then fails (disk full).
            count = self.inner.write(data[: self.short_write_bytes])
            self.short_write_bytes = None
            self.fail_writes_remaining += 1
            return count
        return self.inner.write(data)

    def fsync(self) -> None:
        self.fsync_calls += 1
        if self.fail_fsync:
            raise OSError(errno.EIO, "injected fsync failure")
        self.inner.fsync()

    def truncate(self, size: int) -> None:
        self.truncate_calls.append(size)
        self.inner.truncate(size)

    def close(self) -> None:
        self.closed = True
        self.inner.close()


def _open(tmp_path: Path) -> tuple[Path, ScriptedBackend]:
    path = tmp_path / "events.jsonl"
    handle = secure_open_owner_only_append(path)
    return path, ScriptedBackend(FdWriteBackend(handle.fd))


def _writer(backend: ScriptedBackend, **kwargs) -> TrajectoryWriter:
    return TrajectoryWriter(
        backend=backend,
        session_id=SESSION_ID,
        runtime_id=new_analytics_id(),
        coverage_id=new_analytics_id(),
        branch_id=new_analytics_id(),
        **kwargs,
    )


def _writer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(f"chrys-trajectory-writer-{SESSION_ID[:8]}")]


def _draft(n: int, **extra: object) -> EventDraft:
    return EventDraft(event_type=EventType.TURN_STARTED, payload={"turn_number": n, **extra})


# --------------------------------------------------------------------------- basics


async def test_submitted_event_preserves_sequence_or_returns_immediate_refusal(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    try:
        submitted = writer.submit(_draft(1))
        assert not isinstance(submitted.result, EmitResult)
        assert submitted.result.sequence == 1
        assert await writer.emit(_draft(2)) is EmitResult.WRITTEN
        assert await writer.wait(submitted) is EmitResult.WRITTEN
        assert [event.payload["turn_number"] for event in read_trajectory(path).events] == [1, 2]
    finally:
        await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)

    next_sequence = writer.snapshot().next_sequence
    refused = writer.submit(_draft(3))
    assert refused.result is EmitResult.DEGRADED
    assert await writer.wait(refused) is EmitResult.DEGRADED
    assert writer.snapshot().next_sequence == next_sequence


async def test_emit_is_written_through_and_readable_immediately(tmp_path: Path) -> None:
    """Acceptance 15 (in-process half): WRITTEN means the line is already in the file."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    for n in range(5):
        assert await writer.emit(_draft(n)) is EmitResult.WRITTEN
        read = read_trajectory(path)
        assert [e.payload["turn_number"] for e in read.events] == list(range(n + 1))
        assert read.torn_tail_bytes == 0
    snap = writer.snapshot()
    assert snap.last_written_sequence == 5
    assert snap.next_sequence == 6
    assert snap.last_durable_sequence == 0
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    assert writer.state is WriterState.CLOSED
    assert backend.closed
    events = read_trajectory(path).events
    assert [e.event_type for e in events[-2:]] == [EventType.COVERAGE_ENDED, EventType.RUNTIME_FINISHED]
    assert events[-2].payload == {"coverage_id": writer.coverage_id, "last_sequence": 5}
    assert events[-1].payload == {"reason": "graceful_shutdown"}
    assert_trajectory_accounted(read_trajectory(path))
    for event in events:
        assert event.runtime_id == writer.runtime_id
        assert event.coverage_id == writer.coverage_id
        assert event.session_id == SESSION_ID
        assert event.branch_id == writer.branch_id


async def test_event_id_from_draft_is_preserved_and_file_is_owner_only(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    draft = _draft(1)
    await writer.emit(draft)
    events = read_trajectory(path).events
    assert events[0].event_id == draft.event_id
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)


async def test_branch_id_switch_applies_to_later_events(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    first_branch = writer.branch_id
    await writer.emit(_draft(1))
    second_branch = new_analytics_id()
    writer.set_branch_id(second_branch)
    await writer.emit(_draft(2))
    events = read_trajectory(path).events
    assert [e.branch_id for e in events] == [first_branch, second_branch]
    await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)


async def test_initial_sequence_and_offset_resume_an_existing_file(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    await writer.emit(_draft(1))
    await writer.emit(_draft(2))
    await writer.close(reason=RuntimeFinishReason.SESSION_SWITCH)
    size = path.stat().st_size
    last = read_trajectory(path).events[-1].sequence

    handle = secure_open_owner_only_append(path)
    resumed = _writer(ScriptedBackend(FdWriteBackend(handle.fd)), initial_sequence=last, initial_offset=size)
    await resumed.emit(_draft(3))
    events = read_trajectory(path).events
    assert [e.sequence for e in events] == list(range(1, last + 2))
    assert_trajectory_accounted(read_trajectory(path))
    await resumed.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)


def test_write_ack_timeout_must_be_positive(tmp_path: Path) -> None:
    _path, backend = _open(tmp_path)
    with pytest.raises(ValueError):
        _writer(backend, write_ack_timeout=0)
    backend.close()


async def test_completed_worker_ack_wins_over_delayed_event_loop_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late future-bridge callback must not degrade an event the worker already wrote."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    worker_resolved = threading.Event()
    original_resolve = writer_module._resolve

    def resolve_and_signal(future: Any, result: EmitResult) -> None:
        original_resolve(future, result)
        worker_resolved.set()

    async def timeout_after_worker_ack(awaitable: object, timeout: float) -> None:
        del awaitable, timeout
        assert await asyncio.to_thread(worker_resolved.wait, WAIT_TIMEOUT)
        raise TimeoutError

    with monkeypatch.context() as patch:
        patch.setattr(writer_module, "_resolve", resolve_and_signal)
        patch.setattr(writer_module.asyncio, "wait_for", timeout_after_worker_ack)
        assert await writer.emit(_draft(1)) is EmitResult.WRITTEN

    assert writer.state is WriterState.ACTIVE
    assert [event.payload["turn_number"] for event in read_trajectory(path).events] == [1]
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)


# ---------------------------------------------------------------- concurrency / prefix


async def test_concurrent_producers_keep_sequence_strictly_increasing(tmp_path: Path) -> None:
    """Acceptance 4 / 15a: async tasks plus foreign threads; no duplicate or reordered sequence."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)

    async def producer(tag: int) -> list[EmitResult]:
        return [await writer.emit(_draft(n, tag=tag)) for n in range(25)]

    thread_results: list[EmitResult] = []

    def thread_producer(tag: int) -> None:
        for n in range(25):
            thread_results.append(writer.emit_blocking(_draft(n, tag=tag)))

    threads = [threading.Thread(target=thread_producer, args=(100 + i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    results = await asyncio.gather(*(producer(i) for i in range(4)))
    for thread in threads:
        await asyncio.to_thread(thread.join)
    assert all(r is EmitResult.WRITTEN for batch in results for r in batch)
    assert all(r is EmitResult.WRITTEN for r in thread_results)
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    read = read_trajectory(path)
    sequences = [e.sequence for e in read.events]
    assert sequences == list(range(1, len(sequences) + 1))
    assert len(read.events) == 4 * 25 + 3 * 25 + 2
    assert_trajectory_accounted(read)


async def test_no_file_lock_is_taken_per_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance 24: the per-event path never touches FileLock."""
    from chrys.foundation.util import lock as lock_module

    def forbidden(self, *args, **kwargs):
        raise AssertionError("FileLock must not be used on the per-event path")

    monkeypatch.setattr(lock_module.FileLock, "__init__", forbidden)
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    for n in range(50):
        assert await writer.emit(_draft(n)) is EmitResult.WRITTEN
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    assert len(read_trajectory(path).events) == 52


async def test_written_ack_latency_stays_small(tmp_path: Path, record_property: Callable[[str, object], None]) -> None:
    """Acceptance 15d: local-disk ack latency is sub-millisecond in the common case.

    The plan treats this as an alert, not a hard gate, so the bound is generous
    and on the 95th percentile. The 99th is recorded, not asserted: each ack
    crosses to the worker thread and back, and on a loaded runner the few
    slowest wait for the scheduler at one of those hand-offs.
    """
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    await writer.emit(_draft(0))  # warm the thread
    results: list[EmitResult] = []
    samples: list[float] = []
    for n in range(300):
        started = time.perf_counter()
        results.append(await writer.emit(_draft(n)))
        samples.append((time.perf_counter() - started) * 1000.0)
    fsyncs_while_acking = backend.fsync_calls
    await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95)]
    record_property("written_ack_p50_ms", round(statistics.median(samples), 3))
    record_property("written_ack_p95_ms", round(p95, 3))
    record_property("written_ack_p99_ms", round(samples[int(len(samples) * 0.99)], 3))
    assert results == [EmitResult.WRITTEN] * 300
    # What keeps an ack this fast: it waits for the write, never for an fsync.
    assert fsyncs_while_acking == 0
    assert p95 < 25.0
    assert path.stat().st_size > 0


# --------------------------------------------------------------------- encode failures


async def test_oversized_line_is_gapped_without_degrading_the_writer(tmp_path: Path) -> None:
    """Acceptance 22/25: over-budget event → slot + gap(line_budget_exceeded); writer stays ACTIVE."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.emit(_draft(1)) is EmitResult.WRITTEN
    assert writer.measure_line(_draft(2, blob="x" * LINE_BUDGET_BYTES)) > LINE_BUDGET_BYTES
    assert await writer.emit(_draft(2, blob="x" * LINE_BUDGET_BYTES)) is EmitResult.DEGRADED
    assert writer.state is WriterState.ACTIVE
    assert await writer.emit(_draft(3)) is EmitResult.WRITTEN
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    events = read_trajectory(path).events
    assert [e.sequence for e in events] == [1, 3, 4, 5, 6]
    gap = events[1]
    assert gap.event_type == EventType.GAP
    assert gap.payload == {
        "first_sequence": 2,
        "last_sequence": 2,
        "dropped_count": 1,
        "reason": GapReason.LINE_BUDGET_EXCEEDED,
    }
    assert_trajectory_accounted(read_trajectory(path))
    assert "x" * 100 not in path.read_text()


async def test_out_of_range_integer_is_gapped(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.emit(_draft(1, huge=2**63)) is EmitResult.DEGRADED
    assert writer.state is WriterState.ACTIVE
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    events = read_trajectory(path).events
    assert events[0].event_type == EventType.GAP
    assert events[0].payload["reason"] == GapReason.VALUE_OUT_OF_RANGE
    assert_trajectory_accounted(read_trajectory(path))


async def test_a_gap_no_reader_could_decode_is_not_written_either(tmp_path: Path) -> None:
    """Past the last slot a line may carry, the file ends instead of growing garbage."""
    path, backend = _open(tmp_path)
    writer = _writer(backend, initial_sequence=INT64_MAX - 1)
    assert await writer.emit(_draft(1)) is EmitResult.WRITTEN
    # Its slot is out of range, so the line is refused — and so is the gap that
    # would have explained it, which would carry a sequence just as unwritable.
    assert await writer.emit(_draft(2)) is EmitResult.DEGRADED
    await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    result = read_trajectory(path)
    assert result.corrupt_lines == []
    assert [event.sequence for event in result.events] == [INT64_MAX]


async def test_malformed_identifier_is_gapped_rather_than_written(tmp_path: Path) -> None:
    """A line no reader could decode is never written: the slot becomes a gap."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.emit(_draft(1, batch_id=7)) is EmitResult.DEGRADED
    assert writer.state is WriterState.ACTIVE
    assert await writer.emit(_draft(2)) is EmitResult.WRITTEN
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    result = read_trajectory(path)
    # The reader sees a clean file with a declared hole, not corruption.
    assert result.corrupt_lines == []
    assert result.events[0].event_type == EventType.GAP
    assert result.events[0].payload["reason"] == GapReason.ENCODE_FAILURE
    assert_trajectory_accounted(result)


async def test_unencodable_payload_is_gapped(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.emit(_draft(1, bad=object())) is EmitResult.DEGRADED
    assert writer.state is WriterState.ACTIVE
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    events = read_trajectory(path).events
    assert events[0].payload["reason"] == GapReason.ENCODE_FAILURE


async def test_a_payload_factory_that_raises_is_gapped_like_any_unwritable_line(tmp_path: Path) -> None:
    """The slot is spent when the sequence is handed out, so the factory cannot take it away with it."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.emit(_draft(1)) is EmitResult.WRITTEN

    def exploding(_sequence: int) -> Mapping[str, Any]:
        raise RuntimeError("payload unavailable")

    assert await writer.emit(_draft(2), payload_factory=exploding) is EmitResult.DEGRADED
    assert writer.state is WriterState.ACTIVE
    assert await writer.emit(_draft(3)) is EmitResult.WRITTEN
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    result = read_trajectory(path)
    gap = result.events[1]
    assert gap.event_type == EventType.GAP
    assert gap.payload == {
        "first_sequence": 2,
        "last_sequence": 2,
        "dropped_count": 1,
        "reason": GapReason.ENCODE_FAILURE,
    }
    assert_trajectory_accounted(result)


async def test_an_abandoned_worker_needs_no_second_thread_to_hand_its_file_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck worker outlives its close, and what frees the file is the worker's own return.

    Waiting it out from a helper thread only works while threads can still be
    made — and the condition that abandons a worker is exactly the one that
    can refuse the helper.
    """
    _path, backend = _open(tmp_path)
    handed_back: list[bool] = []
    block = threading.Event()
    backend.block_writes = block
    writer = _writer(backend, write_ack_timeout=0.05, on_worker_exit=lambda: handed_back.append(True))
    assert await writer.emit(_draft(1)) is EmitResult.DEGRADED
    assert await asyncio.to_thread(backend.block_entered.wait, WAIT_TIMEOUT)

    def refused(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("can't start new thread")

    # From here the system has no thread left to give. The module's own name
    # is replaced, never the stdlib module the event loop also runs on.
    monkeypatch.setattr(
        writer_module,
        "threading",
        SimpleNamespace(Thread=refused, Lock=threading.Lock, Condition=threading.Condition),
    )
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN, join_timeout=0.05) is False
    assert writer.thread_alive is True
    assert handed_back == []

    block.set()
    assert await wait_until(lambda: handed_back == [True])


async def test_a_worker_the_system_refuses_to_start_still_gives_its_resources_back(tmp_path: Path) -> None:
    """A thread that never started must not be claimed as started: it can never be joined.

    Claiming it makes ``close`` raise on the join and strand both the
    descriptor and the session's writer lease — the file would stay leased for
    the rest of the process.
    """
    _path, backend = _open(tmp_path)
    handed_back: list[bool] = []
    writer = _writer(backend, on_worker_exit=lambda: handed_back.append(True))

    class _RefusedThread:
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            raise AssertionError("a thread that never started must not be joined")

    writer._thread = _RefusedThread()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        writer.start()

    # The close still runs to the end, and the worker's resources come back.
    with contextlib.suppress(RuntimeError):
        await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    assert handed_back == [True]
    assert backend.closed is True


# ------------------------------------------------------------------------ write failure


async def test_write_failure_degrades_with_warning_and_session_continues(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Acceptance 25 / 15a: failure → TERMINAL_DEGRADED + warning; no larger sequence acked before the gap."""
    path, backend = _open(tmp_path)
    degraded_reasons: list[str] = []
    writer = _writer(backend, on_degraded=degraded_reasons.append)
    assert await writer.emit(_draft(1)) is EmitResult.WRITTEN
    backend.fail_writes_remaining = 1
    with caplog.at_level(logging.WARNING):
        assert await writer.emit(_draft(2)) is EmitResult.DEGRADED
    assert writer.state is WriterState.TERMINAL_DEGRADED
    assert writer.is_degraded
    assert degraded_reasons == [GapReason.WRITE_FAILURE]
    assert any("TERMINAL_DEGRADED" in record.getMessage() for record in caplog.records)
    # Later emits get slots only, never an ack; the session is not blocked.
    with earlier_tests_heap_frozen():
        started = time.monotonic()
        later = [await writer.emit(_draft(n)) for n in range(3, 8)]
        elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert later == [EmitResult.DEGRADED] * 5
    # The writer recovered (backend works again) and wrote the gap for slot 2 on its own.
    assert await wait_until(lambda: any(e.event_type == EventType.GAP for e in read_trajectory(path).events))
    events = read_trajectory(path).events
    assert events[0].sequence == 1
    assert events[1].event_type == EventType.GAP
    assert events[1].payload["first_sequence"] == 2
    assert events[1].payload["reason"] == GapReason.WRITE_FAILURE
    # Graceful degraded close: gaps cover every virtual slot, then the closure markers.
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    events = read_trajectory(path).events
    types = [e.event_type for e in events]
    assert types[-2:] == [EventType.COVERAGE_ENDED, EventType.RUNTIME_FINISHED]
    business = [e for e in events if e.event_type == EventType.TURN_STARTED]
    assert [e.sequence for e in business] == [1]
    gaps = [e for e in events if e.event_type == EventType.GAP]
    covered = sorted(s for g in gaps for s in range(g.payload["first_sequence"], g.payload["last_sequence"] + 1))
    physical = [e.sequence for e in events]
    # Six slots were never written (2 plus the five later emits); gaps may interleave
    # with the slots because the worker gaps eagerly, so assert the partition, not the layout.
    assert len(covered) == 6
    assert covered[0] == 2
    assert not set(covered) & set(physical)
    assert sorted([*covered, *physical]) == list(range(1, physical[-1] + 1))
    assert sum(g.payload["dropped_count"] for g in gaps) == 6
    assert {g.payload["reason"] for g in gaps} <= {GapReason.WRITE_FAILURE, GapReason.DEGRADED_CLOSE}
    assert_trajectory_accounted(read_trajectory(path))
    for event in events[1:]:
        assert event.event_type in {EventType.GAP, EventType.COVERAGE_ENDED, EventType.RUNTIME_FINISHED}


async def test_short_write_tears_tail_then_writer_repairs_it(tmp_path: Path) -> None:
    """Acceptance 15e: a half-written batch is truncated to the last complete line before any gap lands."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.emit(_draft(1)) is EmitResult.WRITTEN
    clean_size = path.stat().st_size
    backend.short_write_bytes = 20
    assert await writer.emit(_draft(2)) is EmitResult.DEGRADED
    assert writer.state is WriterState.TERMINAL_DEGRADED
    assert await wait_until(lambda: backend.truncate_calls == [clean_size])
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    read = read_trajectory(path)
    assert read.torn_tail_bytes == 0
    assert read.corrupt_lines == []
    assert [e.sequence for e in read.events][:2] == [1, 3]
    assert read.events[1].event_type == EventType.GAP
    assert read.events[1].payload["first_sequence"] == 2
    assert_trajectory_accounted(read)


async def test_short_write_inside_a_batch_confirms_the_complete_prefix(tmp_path: Path) -> None:
    """Items fully written before the fault are WRITTEN; the torn one and the rest are DEGRADED."""
    path, backend = _open(tmp_path)
    block = threading.Event()
    backend.block_writes = block
    writer = _writer(backend, write_ack_timeout=5.0)
    first = asyncio.ensure_future(writer.emit(_draft(1)))
    await asyncio.to_thread(backend.block_entered.wait)
    # Queue three more while the first batch is blocked; they form one fan-in batch.
    others = [asyncio.ensure_future(writer.emit(_draft(n))) for n in (2, 3, 4)]
    await asyncio.sleep(0.05)
    backend.block_writes = None
    line_2 = len(
        encode_event_line(
            build_event(
                _draft(2),
                sequence=2,
                runtime_id=writer.runtime_id,
                coverage_id=writer.coverage_id,
                session_id=SESSION_ID,
                branch_id=writer.branch_id,
            )
        )
    )
    backend.short_write_bytes = line_2 + 5  # second line complete, third torn
    backend.short_write_on_call = 2  # the batch after the blocked first write
    block.set()
    assert await first is EmitResult.WRITTEN
    results = await asyncio.gather(*others)
    assert results == [EmitResult.WRITTEN, EmitResult.DEGRADED, EmitResult.DEGRADED]
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    read = read_trajectory(path)
    assert read.torn_tail_bytes == 0
    assert [e.sequence for e in read.events][:3] == [1, 2, 5]
    assert read.events[2].payload == {
        "first_sequence": 3,
        "last_sequence": 4,
        "dropped_count": 2,
        "reason": GapReason.WRITE_FAILURE,
    }
    assert_trajectory_accounted(read)


# ------------------------------------------------------------------------- hung backend


async def test_hung_backend_bounded_wait_and_terminal_degraded(tmp_path: Path) -> None:
    """Acceptance 15b ①②②a③④⑥ and 15f ③: bounded wait, queue classification, no second writer."""
    path, backend = _open(tmp_path)
    block = threading.Event()
    backend.block_writes = block
    # Earlier tests on this xdist worker may have left their writers' threads, of the same name, alive.
    earlier_threads = set(_writer_threads())
    writer = _writer(backend, write_ack_timeout=0.3)
    with earlier_tests_heap_frozen():
        started = time.monotonic()
        first = asyncio.ensure_future(writer.emit(_draft(1)))
        await asyncio.to_thread(backend.block_entered.wait)
        # Queued-not-started events: enqueued while the first batch hangs in write().
        queued = [asyncio.ensure_future(writer.emit(_draft(n))) for n in (2, 3, 4)]
        await asyncio.sleep(0.02)
        assert await first is EmitResult.DEGRADED  # ① bounded by write_ack_timeout
        elapsed = time.monotonic() - started
    assert 0.25 <= elapsed < 2.0
    assert writer.state is WriterState.TERMINAL_DEGRADED
    # ②a: the queued events resolved immediately with the transition, payload released.
    assert await asyncio.wait_for(asyncio.gather(*queued), 0.5) == [EmitResult.DEGRADED] * 3
    snap = writer.snapshot()
    assert snap.queued_sequences == ()
    assert snap.inflight_sequences == (1,)
    assert snap.uncovered_slots == (2, 3, 4)
    # ②: later emits do not wait the timeout again; ④ the caller is never blocked.
    with earlier_tests_heap_frozen():
        started = time.monotonic()
        assert await writer.emit(_draft(5)) is EmitResult.DEGRADED
        elapsed = time.monotonic() - started
    assert elapsed < 0.1
    # ③ no second writer / fd: still exactly one backend, one worker thread.
    assert [thread for thread in _writer_threads() if thread not in earlier_threads] == [writer._thread]
    assert writer.thread_alive
    # Release the hang: event 1 lands late (it is NOT part of any gap, 15f ③); 2..5 never land.
    block.set()
    assert await wait_until(lambda: any(e.event_type == EventType.GAP for e in read_trajectory(path).events))
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    events = read_trajectory(path).events
    assert events[0].sequence == 1
    assert events[0].payload["turn_number"] == 1
    business = [e for e in events if e.event_type == EventType.TURN_STARTED]
    assert [e.sequence for e in business] == [1]
    gaps = [e for e in events if e.event_type == EventType.GAP]
    covered = sorted(s for g in gaps for s in range(g.payload["first_sequence"], g.payload["last_sequence"] + 1))
    assert covered == [2, 3, 4, 5]
    assert sum(g.payload["dropped_count"] for g in gaps) == 4
    assert [e.event_type for e in events[-2:]] == [EventType.COVERAGE_ENDED, EventType.RUNTIME_FINISHED]
    assert_trajectory_accounted(read_trajectory(path))
    assert backend.write_calls >= 2


async def test_a_failed_recovered_gap_keeps_its_interval_for_degraded_close(tmp_path: Path) -> None:
    """A corrupt header may claim a huge range; retry it as an interval, never per slot."""
    path, backend = _open(tmp_path)
    recovered_last = 10**12
    backend.fail_writes_remaining = 1
    writer = _writer(
        backend,
        initial_sequence=recovered_last,
        recovered_gap=SequenceRange(1, recovered_last),
    )

    assert await writer.emit(_draft(1)) is EmitResult.DEGRADED
    snapshot = writer.snapshot()
    assert len(snapshot.uncovered_ranges) == 1
    assert snapshot.uncovered_ranges[0].first_sequence == 1
    assert snapshot.uncovered_ranges[0].last_sequence == recovered_last

    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    result = read_trajectory(path)
    assert_trajectory_accounted(result)
    gap = result.events[0]
    assert gap.event_type == EventType.GAP
    assert gap.payload["first_sequence"] == 1
    assert gap.payload["last_sequence"] == recovered_last + 2
    assert gap.payload["dropped_count"] == recovered_last + 2


async def test_hung_backend_close_does_not_write_runtime_finished_and_joins_bounded(tmp_path: Path) -> None:
    """Acceptance 15b ⑤⑥ / 15f ④: no gap → no closure markers; close returns after a bounded join."""
    path, backend = _open(tmp_path)
    block = threading.Event()
    backend.block_writes = block
    writer = _writer(backend, write_ack_timeout=0.2)
    first = asyncio.ensure_future(writer.emit(_draft(1)))
    await asyncio.to_thread(backend.block_entered.wait)
    assert await first is EmitResult.DEGRADED
    assert await writer.emit(_draft(2)) is EmitResult.DEGRADED
    with earlier_tests_heap_frozen():
        started = time.monotonic()
        closed = await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN, join_timeout=0.2)
        elapsed = time.monotonic() - started
    assert closed is False
    assert elapsed < 3.0
    assert writer.state is WriterState.CLOSED
    assert writer.runtime_closed is False
    # The worker is stuck in the backend: the fd is deliberately left open (no close underneath write()).
    assert writer.thread_alive
    assert backend.closed is False
    assert read_trajectory(path).events == []
    # A late emit after close is refused without waiting.
    assert await writer.emit(_draft(3)) is EmitResult.DEGRADED
    block.set()
    await asyncio.to_thread(writer._thread.join, 5.0)
    assert not writer.thread_alive
    events = read_trajectory(path).events
    assert all(e.event_type != EventType.RUNTIME_FINISHED for e in events)
    assert all(e.event_type != EventType.COVERAGE_ENDED for e in events)


async def test_a_late_worker_records_the_slots_a_timed_out_close_left_uncovered(tmp_path: Path) -> None:
    """The close gave up on this thread, so this thread is the only one that can explain the hole."""
    path, backend = _open(tmp_path)
    block = threading.Event()
    backend.block_writes = block
    writer = _writer(backend, write_ack_timeout=0.2)
    first = asyncio.ensure_future(writer.emit(_draft(1)))
    await asyncio.to_thread(backend.block_entered.wait)
    assert await first is EmitResult.DEGRADED
    assert await writer.emit(_draft(2)) is EmitResult.DEGRADED  # queued behind the hang, never written
    assert writer.snapshot().uncovered_slots == (2,)
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN, join_timeout=0.2) is False
    assert writer.state is WriterState.CLOSED  # the close moved on without this thread

    block.set()
    await asyncio.to_thread(writer._thread.join, 5.0)

    events = read_trajectory(path).events
    assert [event.sequence for event in events] == [1, 3]
    gap = events[1]
    assert gap.event_type == EventType.GAP
    assert (gap.payload["first_sequence"], gap.payload["last_sequence"]) == (2, 2)
    # Slot 2 is spent and said to be spent, so the next runtime resumes past
    # it instead of handing the same slot out for a different event.
    assert_trajectory_accounted(read_trajectory(path))


async def test_cancelled_producer_still_gets_its_line_written(tmp_path: Path) -> None:
    """Acceptance 15c: the write request is shielded from the producer's cancellation."""
    path, backend = _open(tmp_path)
    block = threading.Event()
    backend.block_writes = block
    writer = _writer(backend, write_ack_timeout=5.0)
    draft = _draft(1, marker="cancelled-producer")
    task = asyncio.ensure_future(writer.emit(draft))
    await asyncio.to_thread(backend.block_entered.wait)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.state is WriterState.ACTIVE
    block.set()
    assert await wait_until(lambda: len(read_trajectory(path).events) == 1)
    event = read_trajectory(path).events[0]
    assert event.event_id == draft.event_id
    assert event.payload["marker"] == "cancelled-producer"
    assert await writer.emit(_draft(2)) is EmitResult.WRITTEN
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)


# ---------------------------------------------------------------------------- checkpoint


async def test_checkpoint_fsyncs_and_advances_last_durable_only_there(tmp_path: Path) -> None:
    """Acceptance 16."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    await writer.emit(_draft(1))
    await writer.emit(_draft(2))
    assert backend.fsync_calls == 0
    assert writer.snapshot().last_durable_sequence == 0
    assert await writer.checkpoint() is EmitResult.WRITTEN
    assert backend.fsync_calls == 1
    assert writer.snapshot().last_durable_sequence == 3
    await writer.emit(_draft(3))
    assert backend.fsync_calls == 1
    assert writer.snapshot().last_durable_sequence == 3
    assert writer.snapshot().last_written_sequence == 4
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    assert backend.fsync_calls == 2  # runtime.finished is fsynced too
    events = read_trajectory(path).events
    checkpoint = events[2]
    assert checkpoint.event_type == EventType.CHECKPOINT
    assert checkpoint.payload == {"last_assigned": 3, "last_written": 2, "last_durable": 0}
    assert_trajectory_accounted(read_trajectory(path))


async def test_close_is_refused_for_late_emits_and_idempotent(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    await writer.emit(_draft(1))
    assert await writer.close(reason=RuntimeFinishReason.SESSION_SWITCH)
    assert await writer.emit(_draft(2)) is EmitResult.DEGRADED
    assert await writer.close(reason=RuntimeFinishReason.SESSION_SWITCH) is False
    events = read_trajectory(path).events
    assert [e.event_type for e in events] == [
        EventType.TURN_STARTED,
        EventType.COVERAGE_ENDED,
        EventType.RUNTIME_FINISHED,
    ]
    assert events[-1].payload["reason"] == "session_switch"


async def test_close_without_any_emit_still_closes_cleanly(tmp_path: Path) -> None:
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    events = read_trajectory(path).events
    assert [e.sequence for e in events] == [1, 2]
    assert events[0].payload["last_sequence"] == 0


# ------------------------------------------------------------------------- SIGKILL


_KILL_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys, os
    from pathlib import Path
    from chrys.foundation.platform.files import secure_open_owner_only_append
    from chrys.foundation.trajectory.envelope import EventDraft
    from chrys.foundation.trajectory.ids import new_analytics_id
    from chrys.foundation.trajectory.writer import EmitResult, FdWriteBackend, TrajectoryWriter

    async def main() -> None:
        path = Path(sys.argv[1])
        handle = secure_open_owner_only_append(path)
        writer = TrajectoryWriter(
            backend=FdWriteBackend(handle.fd),
            session_id="12345678-1234-1234-1234-123456789abc",
            runtime_id=new_analytics_id(),
            coverage_id=new_analytics_id(),
            branch_id=new_analytics_id(),
        )
        for n in range(int(sys.argv[2])):
            result = await writer.emit(EventDraft(event_type="turn.started", payload={"turn_number": n}))
            assert result is EmitResult.WRITTEN
        sys.stdout.write("WRITTEN\\n")
        sys.stdout.flush()
        await asyncio.sleep(30)

    asyncio.run(main())
    """
)


def test_events_acked_written_survive_sigkill(tmp_path: Path) -> None:
    """Acceptance 15: WRITTEN + immediate kill (TerminateProcess on Windows) → event is in the file."""
    path = tmp_path / "events.jsonl"
    count = 7
    proc = subprocess.Popen(
        [sys.executable, "-c", _KILL_SCRIPT, str(path), str(count)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.strip() == "WRITTEN"
        proc.kill()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    read = read_trajectory(path)
    assert [e.payload["turn_number"] for e in read.events] == list(range(count))
    assert read.torn_tail_bytes == 0
    assert_trajectory_accounted(read)


async def test_a_failed_fsync_denies_the_durability_it_was_asked_for(tmp_path: Path) -> None:
    """A checkpoint whose fsync fails is not WRITTEN, and the close it fronts is not clean."""
    path, backend = _open(tmp_path)
    writer = _writer(backend)
    assert await writer.emit(_draft(1)) is EmitResult.WRITTEN
    backend.fail_fsync = True
    # The line itself is on disk — only the durability the checkpoint asked
    # for is missing, so the ordinary event around it stays WRITTEN.
    assert await writer.checkpoint() is EmitResult.DEGRADED
    assert await writer.emit(_draft(2)) is EmitResult.WRITTEN
    assert await writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is False
    read = read_trajectory(path)
    assert read.corrupt_lines == []
    assert [e.event_type for e in read.events].count(EventType.TURN_STARTED) == 2
