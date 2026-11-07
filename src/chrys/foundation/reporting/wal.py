# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The durable queue: an append-only frame log with a single acknowledged offset.

``wal.log`` holds exactly the bytes a ``POST /v1/batch`` body carries, so the
pump slices a continuous run of frames out of it and never re-encodes. Only the
acknowledged offset lives outside the log (``wal.ack``), which is why a crash
mid-send is safe: the request is idempotent and the frames are still there to
resend.

Three guarantees the rest of the subsystem relies on:

* a frame is only "taken" once it is on disk (the caller flushes before
  anything else can read it);
* a torn tail — a crash in the middle of an append — is truncated on open, so
  the next append never concatenates onto half a frame;
* the file never exceeds ``queue_bytes_max``: the oldest unacknowledged frames
  are dropped with per-frame evidence in ``dropped.log``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import (
    MAX_BATCH_FRAMES,
    Frame,
    FrameError,
    FrameHeader,
    read_frame_header,
)
from chrys.foundation.util.time import utc_iso

WAL_LOG_NAME: Final = "wal.log"
WAL_ACK_NAME: Final = "wal.ack"
DROPPED_LOG_NAME: Final = "dropped.log"
CURSORS_DIR_NAME: Final = "cursors"

COMPACT_THRESHOLD_BYTES: Final = 8 << 20
"""Acknowledged prefix size at which the log is rewritten to a fresh file."""

_COPY_BLOCK_BYTES: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class WalStats:
    """Byte watermarks of the queue."""

    ack_offset: int
    end_offset: int

    @property
    def pending_bytes(self) -> int:
        """Unacknowledged bytes still waiting to be sent."""
        return max(0, self.end_offset - self.ack_offset)


@dataclass(frozen=True, slots=True)
class WalBatch:
    """A continuous run of frames starting at the acknowledged offset."""

    start: int
    end: int
    headers: tuple[FrameHeader, ...]
    ends: tuple[int, ...]
    """Byte offset just past each header's frame, so a partial ack can advance."""

    @property
    def size(self) -> int:
        """Bytes this batch occupies in the log."""
        return self.end - self.start

    @property
    def unit_ids(self) -> tuple[str, ...]:
        """The batch's units, in log order."""
        return tuple(header.unit_id for header in self.headers)


class DurableWAL:
    """Append frames, hand the pump a batch, advance the acknowledged offset."""

    def __init__(self, directory: Path, *, queue_bytes_max: int, diagnostics: ReportDiagnostics) -> None:
        self._directory = directory
        self._queue_bytes_max = max(1, queue_bytes_max)
        self._diagnostics = diagnostics
        self._log_path = directory / WAL_LOG_NAME
        self._ack_path = directory / WAL_ACK_NAME
        self._dropped_path = directory / DROPPED_LOG_NAME
        self._lock = threading.RLock()
        self._log: BinaryIO | None = None
        self._ack = 0
        self._end = 0
        self._closed = False

    @property
    def directory(self) -> Path:
        """Where the queue and its cursors live."""
        return self._directory

    @property
    def cursors_dir(self) -> Path:
        """Per-session cursor files."""
        return self._directory / CURSORS_DIR_NAME

    def open(self) -> None:
        """Open the log, recover the acknowledged offset and trim a torn tail."""
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._log = self._log_path.open("a+b")
            self._end = self._log_path.stat().st_size
            self._ack = self._read_ack()
            if self._ack > self._end:
                self._ack = 0
                self._write_ack()
            self._end = self._truncate_torn_tail()
            self._closed = False
            self._diagnostics.set_queued_bytes(self._end - self._ack)

    def close(self) -> None:
        """Flush and release the log handle."""
        with self._lock:
            self._closed = True
            if self._log is not None:
                with contextlib.suppress(OSError):
                    self._log.flush()
                    self._log.close()
                self._log = None

    def append(self, frames: Sequence[Frame]) -> int:
        """Append *frames*, returning the bytes written.

        The bytes reach the file's buffer at once so the pump can read them;
        the caller's :meth:`flush` makes them durable.
        """
        if not frames:
            return 0
        blob = b"".join(frame.encode() for frame in frames)
        with self._lock:
            handle = self._require_log()
            handle.seek(0, 2)
            handle.write(blob)
            handle.flush()
            self._end += len(blob)
            self._enforce_capacity()
            self._diagnostics.set_queued_bytes(self._end - self._ack)
        return len(blob)

    def flush(self) -> None:
        """Push buffered bytes to the OS and fsync them (one call per collection round)."""
        with self._lock:
            if self._log is None:
                return
            self._log.flush()
            with contextlib.suppress(OSError, ValueError):
                os.fsync(self._log.fileno())

    def stats(self) -> WalStats:
        """Current watermarks."""
        with self._lock:
            return WalStats(ack_offset=self._ack, end_offset=self._end)

    def next_batch(self, *, max_bytes: int) -> WalBatch | None:
        """The next continuous run of unacknowledged frames, or ``None`` when caught up."""
        with self._lock:
            handle = self._require_log()
            offset = self._ack
            if offset >= self._end:
                return None
            headers: list[FrameHeader] = []
            ends: list[int] = []
            limit = max(1, max_bytes)
            while offset < self._end and len(headers) < MAX_BATCH_FRAMES:
                try:
                    header, frame_end = read_frame_header(handle, offset)
                except FrameError:
                    # Damaged bytes in front of the queue: skip past them so one
                    # bad write cannot wedge the pump forever.
                    self._drop_to(self._end)
                    return None
                if frame_end > self._end:
                    self._drop_to(self._end)
                    return None
                if headers and frame_end - self._ack > limit:
                    break
                headers.append(header)
                ends.append(frame_end)
                offset = frame_end
            if not headers:
                return None
            return WalBatch(start=self._ack, end=offset, headers=tuple(headers), ends=tuple(ends))

    def read(self, batch: WalBatch) -> bytes:
        """The batch's raw bytes, as one request body."""
        with self._lock:
            handle = self._require_log()
            handle.seek(batch.start)
            return handle.read(batch.end - batch.start)

    def advance(self, offset: int) -> None:
        """Acknowledge everything before *offset*."""
        with self._lock:
            target = min(max(offset, self._ack), self._end)
            if target == self._ack:
                return
            self._ack = target
            self._write_ack()
            self._diagnostics.set_queued_bytes(self._end - self._ack)
            self._compact_if_due()

    def _read_ack(self) -> int:
        try:
            text = self._ack_path.read_text(encoding="ascii").strip()
        except OSError, UnicodeDecodeError:
            return 0
        try:
            return max(0, int(text))
        except ValueError:
            return 0

    def _write_ack(self) -> None:
        atomic_write_text(self._ack_path, f"{self._ack}\n", encoding="ascii")

    def _require_log(self) -> BinaryIO:
        if self._log is None:
            self.open()
        log = self._log
        if log is None:  # pragma: no cover - open() always sets it or raises
            msg = "the report queue is not open"
            raise RuntimeError(msg)
        return log

    def _truncate_torn_tail(self) -> int:
        """Cut the log back to its last complete frame, returning the new size."""
        handle = self._require_log()
        offset = self._ack
        while offset < self._end:
            try:
                _header, frame_end = read_frame_header(handle, offset)
            except FrameError:
                break
            if frame_end > self._end:
                break
            offset = frame_end
        if offset != self._end:
            handle.truncate(offset)
            handle.flush()
        return offset

    def _enforce_capacity(self) -> None:
        """Drop the oldest unacknowledged frames until the queue fits its budget."""
        while self._end - self._ack > self._queue_bytes_max and self._ack < self._end:
            _header, frame_end = self._next_frame_end(self._ack)
            self._drop_to(frame_end)

    def _next_frame_end(self, offset: int) -> tuple[FrameHeader | None, int]:
        handle = self._require_log()
        try:
            header, frame_end = read_frame_header(handle, offset)
        except FrameError:
            return None, self._end
        return header, frame_end

    def _drop_to(self, offset: int) -> None:
        """Acknowledge past *offset*, leaving evidence for each dropped frame."""
        start = self._ack
        while start < offset:
            header, frame_end = self._next_frame_end(start)
            end = min(offset, frame_end)
            if header is not None:
                self._record_drop(header, end - start)
                self._diagnostics.record_drop(1, end - start)
            start = max(end, start + 1)
        self._ack = min(offset, self._end)
        self._write_ack()
        self._diagnostics.set_queued_bytes(self._end - self._ack)

    def _record_drop(self, header: FrameHeader, byte_count: int) -> None:
        record: dict[str, object] = {
            "unit_id": header.unit_id,
            "kind": header.kind,
            "scope": header.scope,
            "reason": "queue_overflow",
            "bytes": byte_count,
            "at": utc_iso(),
        }
        self.record_evidence(record)

    def record_evidence(self, record: dict[str, object]) -> None:
        """Append one explicit discard record to ``dropped.log``."""
        try:
            with self._dropped_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True))
                handle.write("\n")
        except OSError:
            # Losing the evidence must not lose the queue: the counters already
            # hold the same fact.
            return

    def _compact_if_due(self) -> None:
        if self._ack < COMPACT_THRESHOLD_BYTES:
            return
        self.compact()

    def compact(self) -> None:
        """Rewrite the log from its acknowledged offset, so the file can shrink.

        Streamed in fixed blocks: the surviving suffix may be hundreds of
        megabytes and must never be materialised whole.
        """
        with self._lock:
            if self._ack <= 0:
                return
            handle = self._require_log()
            temp_path = self._log_path.with_name(f"{WAL_LOG_NAME}.compact")
            handle.flush()
            with self._log_path.open("rb") as source, temp_path.open("wb") as sink:
                source.seek(self._ack)
                shutil.copyfileobj(source, sink, _COPY_BLOCK_BYTES)
                sink.flush()
            handle.close()
            temp_path.replace(self._log_path)
            self._log = self._log_path.open("a+b")
            self._end -= self._ack
            self._ack = 0
            self._write_ack()
            self._diagnostics.set_queued_bytes(self._end - self._ack)
