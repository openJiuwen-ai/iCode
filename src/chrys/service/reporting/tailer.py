# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Incremental reader of one session's ``trajectory/events.jsonl``.

The tailer ships the file's own bytes and nothing else: it never decodes an
event, never decides what a line means and never writes. It reads from the last
acknowledged offset up to the last complete newline (a torn tail is never sent),
splits the run into frame-sized units, and splits on the line boundaries the
foundation reader already produces.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from chrys.foundation.reporting.frames import (
    KIND_TRAJECTORY_LINES,
    Frame,
    scope_lines,
)
from chrys.foundation.reporting.wal import DurableWAL
from chrys.foundation.trajectory.envelope import iter_complete_lines, peek_envelope_header
from chrys.service.reporting.cursors import SessionCursor

logger = logging.getLogger(__name__)

READ_BUDGET_BYTES: Final = 4 << 20
"""Most bytes one collection round reads from the file."""

FRAME_PAYLOAD_MAX_BYTES: Final = 1 << 20
"""Most raw bytes one ``trajectory.lines`` unit carries."""


class TrajectoryTailer:
    """Read complete lines past the cursor and queue them as units."""

    def __init__(self, wal: DurableWAL) -> None:
        self._wal = wal

    def collect(self, session_id: str, events_path: Path, cursor: SessionCursor) -> int:
        """Queue every new complete line; return how many units were appended."""
        try:
            stat = events_path.stat()
        except OSError:
            return 0
        if stat.st_size == cursor.trajectory_size and stat.st_mtime_ns == cursor.trajectory_mtime_ns:
            return 0
        offset = cursor.trajectory_offset
        reset = False
        if stat.st_size < offset:
            # The file shrank: the offset no longer names a position in it.
            offset = 0
            cursor.trajectory_offset = 0
            reset = True
        try:
            with events_path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(READ_BUDGET_BYTES)
        except OSError:
            logger.debug("Could not read the trajectory of %s", session_id, exc_info=True)
            return 0
        lines, _pending = iter_complete_lines(data)
        if not lines:
            cursor.trajectory_size = stat.st_size
            cursor.trajectory_mtime_ns = stat.st_mtime_ns
            return 0
        frames = self._frames(session_id, lines, reset=reset)
        self._wal.append(frames)
        consumed = sum(len(line) + 1 for line in lines)
        cursor.trajectory_offset = offset + consumed
        cursor.trajectory_size = stat.st_size
        cursor.trajectory_mtime_ns = stat.st_mtime_ns
        return len(frames)

    def _frames(self, session_id: str, lines: Sequence[bytes], *, reset: bool) -> list[Frame]:
        """Group raw lines into payload-bounded units, tracking their sequence span."""
        frames: list[Frame] = []
        chunk: list[bytes] = []
        size = 0
        first_sequence = 0
        last_sequence = 0
        for line in lines:
            header = peek_envelope_header(line)
            if header is not None:
                last_sequence = header.sequence
                if not chunk:
                    first_sequence = header.sequence
            line_size = len(line) + 1
            if chunk and size + line_size > FRAME_PAYLOAD_MAX_BYTES:
                frames.append(self._frame(session_id, chunk, first_sequence, last_sequence, reset=reset))
                chunk = []
                size = 0
                first_sequence = last_sequence
            chunk.append(line)
            size += line_size
        if chunk:
            frames.append(self._frame(session_id, chunk, first_sequence, last_sequence, reset=reset))
        return frames

    def _frame(
        self,
        session_id: str,
        chunk: list[bytes],
        first_sequence: int,
        last_sequence: int,
        *,
        reset: bool,
    ) -> Frame:
        payload = b"".join(line + b"\n" for line in chunk)
        return Frame.build(
            KIND_TRAJECTORY_LINES,
            session_id,
            scope_lines(first_sequence, last_sequence),
            payload,
            reset=reset,
        )
