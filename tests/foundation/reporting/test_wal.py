# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import (
    KIND_SNAPSHOT_CHUNK,
    Frame,
    encode_frames,
    scope_snapshot_chunk,
    sha256_hex,
)
from chrys.foundation.reporting.wal import DROPPED_LOG_NAME, WAL_LOG_NAME, DurableWAL


def _frame(payload: bytes) -> Frame:
    return Frame.build(KIND_SNAPSHOT_CHUNK, "session-1", scope_snapshot_chunk(sha256_hex(payload), 0), payload)


def _open(directory: Path) -> DurableWAL:
    queue = DurableWAL(directory, queue_bytes_max=4 << 20, diagnostics=ReportDiagnostics())
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
    finally:
        queue.close()


def test_unacknowledged_frames_survive_a_reopen(tmp_path: Path) -> None:
    frames = [_frame(b"first"), _frame(b"second")]
    first = _open(tmp_path / "reporting")
    try:
        first.append(frames)
        first.flush()
    finally:
        first.close()

    reopened = _open(tmp_path / "reporting")
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
    log = directory / WAL_LOG_NAME
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
    (directory / WAL_LOG_NAME).write_bytes(b"")
    (directory / "wal.ack").write_text("999\n", encoding="ascii")

    queue = _open(directory)
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


def test_compaction_rewrites_the_log_from_the_acknowledged_offset(tmp_path: Path) -> None:
    directory = tmp_path / "reporting"
    queue = _open(directory)
    try:
        first, second = _frame(b"first"), _frame(b"second")
        queue.append([first, second])
        batch = queue.next_batch(max_bytes=8 << 20)
        assert batch is not None
        queue.advance(batch.ends[0])

        queue.compact()

        assert queue.stats().ack_offset == 0
        remaining = queue.next_batch(max_bytes=8 << 20)
        assert remaining is not None
        assert remaining.unit_ids == (second.header.unit_id,)
        assert (directory / WAL_LOG_NAME).stat().st_size == len(second.encode())
    finally:
        queue.close()
