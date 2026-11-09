# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import os
from pathlib import Path

from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import (
    KIND_SNAPSHOT_CHUNK,
    Frame,
    encode_frames,
    scope_snapshot_chunk,
    sha256_hex,
)
from chrys.foundation.reporting.wal import (
    DROPPED_LOG_MAX_BYTES,
    DROPPED_LOG_NAME,
    DurableWAL,
    segment_ack_name,
    segment_log_name,
)


def _frame(payload: bytes) -> Frame:
    return Frame.build(KIND_SNAPSHOT_CHUNK, "session-1", scope_snapshot_chunk(sha256_hex(payload), 0), payload)


def _open(
    directory: Path,
    *,
    owner_pid: int | None = None,
    pid_alive=None,
    diagnostics: ReportDiagnostics | None = None,
) -> DurableWAL:
    queue = DurableWAL(
        directory,
        queue_bytes_max=4 << 20,
        diagnostics=diagnostics or ReportDiagnostics(),
        owner_pid=owner_pid,
        pid_alive=pid_alive,
    )
    queue.open()
    return queue


def test_the_batch_body_is_the_bytes_that_were_appended(wal: DurableWAL) -> None:
    frames = [_frame(b"first"), _frame(b"second")]
    wal.append(frames)
    wal.flush()

    batch = wal.next_batch(max_bytes=8 << 20)

    assert batch is not None
    assert wal.read(batch) == encode_frames(frames)
    assert batch.unit_ids == tuple(frame.header.unit_id for frame in frames)


def test_advancing_to_the_batch_end_leaves_nothing_pending(wal: DurableWAL) -> None:
    wal.append([_frame(b"first")])
    batch = wal.next_batch(max_bytes=8 << 20)
    assert batch is not None

    wal.advance(batch.end)

    assert wal.stats().pending_bytes == 0
    assert wal.next_batch(max_bytes=8 << 20) is None


def test_a_partial_acknowledgement_resumes_at_the_first_unsettled_frame(wal: DurableWAL) -> None:
    wal.append([_frame(b"first"), _frame(b"second")])
    batch = wal.next_batch(max_bytes=8 << 20)
    assert batch is not None

    wal.advance(batch.ends[0])

    resumed = wal.next_batch(max_bytes=8 << 20)
    assert resumed is not None
    assert resumed.headers == batch.headers[1:]


def test_a_batch_never_exceeds_the_byte_budget(tmp_path: Path) -> None:
    queue = _open(tmp_path / "reporting")
    try:
        frames = [_frame(b"x" * 512), _frame(b"y" * 512)]
        queue.append(frames)

        batch = queue.next_batch(max_bytes=1)

        assert batch is not None
        assert len(batch.headers) == 1
        assert batch.size <= 1024
    finally:
        queue.close()


def test_unacknowledged_frames_survive_a_reopen(tmp_path: Path) -> None:
    directory = tmp_path / "reporting"
    frames = [_frame(b"first"), _frame(b"second")]
    first = _open(directory)
    try:
        first.append(frames)
        first.flush()
    finally:
        first.close()

    reopened = _open(directory)
    try:
        batch = reopened.next_batch(max_bytes=8 << 20)
        assert batch is not None
        assert reopened.read(batch) == encode_frames(frames)
    finally:
        reopened.close()


def test_a_torn_tail_is_truncated_before_the_next_append(tmp_path: Path) -> None:
    directory = tmp_path / "reporting"
    frame = _frame(b"first")
    first = _open(directory)
    try:
        first.append([frame])
        first.flush()
    finally:
        first.close()
    log = directory / segment_log_name(first._owner_pid)
    with log.open("ab") as handle:
        handle.write(frame.encode()[:12])

    reopened = _open(directory)
    try:
        assert reopened.stats().end_offset == len(frame.encode())
        reopened.append([_frame(b"second")])
        batch = reopened.next_batch(max_bytes=8 << 20)
        assert batch is not None
        assert len(batch.headers) == 2
    finally:
        reopened.close()


def test_an_acknowledged_offset_past_the_log_restarts_from_zero(tmp_path: Path) -> None:
    directory = tmp_path / "reporting"
    directory.mkdir(parents=True)
    (directory / segment_log_name(111)).write_bytes(b"")
    (directory / segment_ack_name(111)).write_text("999\n", encoding="ascii")

    queue = _open(directory, owner_pid=111)
    try:
        assert queue.stats().ack_offset == 0
    finally:
        queue.close()


def test_capacity_drops_the_oldest_frame_with_evidence(tmp_path: Path, report_diagnostics: ReportDiagnostics) -> None:
    directory = tmp_path / "reporting"
    large = _frame(b"x" * 4096)
    queue = DurableWAL(directory, queue_bytes_max=len(large.encode()), diagnostics=report_diagnostics)
    queue.open()
    try:
        queue.append([large, _frame(b"small")])

        batch = queue.next_batch(max_bytes=8 << 20)

        assert batch is not None
        assert batch.unit_ids == (_frame(b"small").header.unit_id,)
        records = [json.loads(line) for line in (directory / DROPPED_LOG_NAME).read_text(encoding="utf-8").splitlines()]
        assert [record["reason"] for record in records] == ["queue_overflow"]
        assert records[0]["unit_id"] == large.header.unit_id
        assert report_diagnostics.snapshot().dropped_units == 1
    finally:
        queue.close()


def test_the_evidence_log_starts_over_past_its_cap(wal: DurableWAL) -> None:
    record = {"unit_id": "x" * 100_000, "reason": "server_rejected"}
    line_len = len(json.dumps(record, ensure_ascii=True, sort_keys=True)) + 1
    written = 0
    while written < DROPPED_LOG_MAX_BYTES * 3:
        wal.record_evidence(record)
        written += line_len

    size = wal.directory.joinpath(DROPPED_LOG_NAME).stat().st_size

    # The log restarts at the cap, so it never grows past one record over it
    # however much is discarded.
    assert size < DROPPED_LOG_MAX_BYTES + line_len
    assert size < written


def test_the_frames_of_a_dead_owner_are_adopted_at_the_next_open(tmp_path: Path) -> None:
    directory = tmp_path / "reporting"
    first, second = _frame(b"first"), _frame(b"second")
    dead = _open(directory, owner_pid=111)
    try:
        dead.append([first])
        dead.flush()
        batch = dead.next_batch(max_bytes=8 << 20)
        assert batch is not None
        dead.advance(batch.ends[0])  # the first frame was sent before the crash
        dead.append([second])
        dead.flush()
    finally:
        dead.close()

    # Owner 111 is gone; owner 222 takes over its unacknowledged frames.
    alive = _open(directory, owner_pid=222, pid_alive=lambda pid: pid != 111)
    try:
        batch = alive.next_batch(max_bytes=8 << 20)
        assert batch is not None
        assert batch.unit_ids == (second.header.unit_id,)
        assert alive.read(batch) == second.encode()
        assert not (directory / segment_log_name(111)).exists()
        assert not (directory / segment_ack_name(111)).exists()
    finally:
        alive.close()


def test_a_live_owner_is_never_adopted(tmp_path: Path) -> None:
    directory = tmp_path / "reporting"
    busy = _open(directory, owner_pid=111)
    try:
        busy.append([_frame(b"mine")])
        busy.flush()
    finally:
        busy.close()

    other = _open(directory, owner_pid=222, pid_alive=lambda pid: True)
    try:
        assert other.next_batch(max_bytes=8 << 20) is None
        assert (directory / segment_log_name(111)).exists()
    finally:
        other.close()


def test_a_leftover_compaction_of_a_dead_owner_is_removed(tmp_path: Path) -> None:
    directory = tmp_path / "reporting"
    directory.mkdir(parents=True)
    leftover = directory / "wal-111.compact"
    leftover.write_bytes(b"half a rewrite")

    _open(directory, owner_pid=222, pid_alive=lambda pid: pid != 111).close()

    assert not leftover.exists()


def test_the_segment_is_named_after_this_process(wal: DurableWAL) -> None:
    assert wal.directory.joinpath(segment_log_name(os.getpid())).exists()
