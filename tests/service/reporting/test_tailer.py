# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

from chrys.foundation.reporting.frames import KIND_TRAJECTORY_LINES, Frame
from chrys.foundation.reporting.wal import DurableWAL
from chrys.service.reporting.cursors import SessionCursor
from chrys.service.reporting.tailer import FRAME_PAYLOAD_MAX_BYTES, TrajectoryTailer


def _line(sequence: int, text: str = "") -> bytes:
    payload = {"schema_version": 1, "sequence": sequence, "event_type": "event.turn"}
    if text:
        payload["payload"] = {"text": text}
    return json.dumps(payload).encode() + b"\n"


def test_only_complete_lines_become_units(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    events = tmp_path / "events.jsonl"
    first, second = _line(1), _line(2)
    events.write_bytes(first + second + b'{"schema_version":1,"sequence":3')
    cursor = SessionCursor()

    appended = TrajectoryTailer(queue).collect("session-1", events, cursor)

    assert appended == 1
    frames = queued(queue)
    assert [frame.header.kind for frame in frames] == [KIND_TRAJECTORY_LINES]
    assert frames[0].header.scope == "lines:1-2"
    assert frames[0].payload == first + second
    assert cursor.trajectory_offset == len(first + second)


def test_an_unchanged_file_is_not_read_again(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_bytes(_line(1))
    cursor = SessionCursor()
    tailer = TrajectoryTailer(queue)
    tailer.collect("session-1", events, cursor)

    assert tailer.collect("session-1", events, cursor) == 0
    assert len(queued(queue)) == 1


def test_a_new_line_is_appended_from_the_saved_offset(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    events = tmp_path / "events.jsonl"
    first = _line(1)
    events.write_bytes(first)
    cursor = SessionCursor()
    tailer = TrajectoryTailer(queue)
    tailer.collect("session-1", events, cursor)
    with events.open("ab") as handle:
        handle.write(_line(2))

    assert tailer.collect("session-1", events, cursor) == 1
    frames = queued(queue)
    assert frames[-1].header.scope == "lines:2-2"
    assert frames[-1].payload == _line(2)
    assert frames[-1].header.reset is False


def test_a_shrunken_file_restarts_from_zero_and_says_so(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_bytes(_line(1) + _line(2) + _line(3))
    cursor = SessionCursor()
    tailer = TrajectoryTailer(queue)
    tailer.collect("session-1", events, cursor)

    rewritten = _line(7)
    events.write_bytes(rewritten)
    os.utime(events, ns=(1, 1))

    assert tailer.collect("session-1", events, cursor) == 1
    frames = queued(queue)
    assert frames[-1].header.reset is True
    assert frames[-1].header.scope == "lines:7-7"
    assert cursor.trajectory_offset == len(rewritten)


def test_a_torn_tail_is_left_for_the_next_round(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_bytes(_line(1))
    cursor = SessionCursor()
    tailer = TrajectoryTailer(queue)
    tailer.collect("session-1", events, cursor)
    with events.open("ab") as handle:
        handle.write(b'{"schema_version":1,"sequence":2')

    assert tailer.collect("session-1", events, cursor) == 0
    assert cursor.trajectory_offset == len(_line(1))


def test_a_missing_file_produces_nothing(tmp_path: Path, queue: DurableWAL) -> None:
    assert TrajectoryTailer(queue).collect("session-1", tmp_path / "absent.jsonl", SessionCursor()) == 0


def test_lines_are_split_into_payload_bounded_units(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    events = tmp_path / "events.jsonl"
    body = b"".join(_line(sequence, text="x" * 4096) for sequence in range(1, 400))
    events.write_bytes(body)
    cursor = SessionCursor()

    appended = TrajectoryTailer(queue).collect("session-1", events, cursor)

    frames = queued(queue)
    assert appended == len(frames) > 1
    assert all(len(frame.payload) <= FRAME_PAYLOAD_MAX_BYTES for frame in frames)
    assert b"".join(frame.payload for frame in frames) == body
    assert cursor.trajectory_offset == len(body)
