# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path

from chrys.foundation.reporting.frames import KIND_SNAPSHOT_CHUNK, KIND_SNAPSHOT_MANIFEST, Frame
from chrys.foundation.reporting.wal import DurableWAL
from chrys.service.reporting.cursors import SessionCursor
from chrys.service.reporting.snapshot import CHUNK_SIZE_BYTES, SnapshotTap


def _blocks(count: int) -> list[bytes]:
    return [bytes([0x41 + index]) * CHUNK_SIZE_BYTES for index in range(count)]


def test_a_first_capture_sends_every_block_and_the_manifest(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    session = tmp_path / "session.json"
    session.write_bytes(b"".join(_blocks(2)))
    cursor = SessionCursor()

    appended = SnapshotTap(queue).collect("session-1", session, cursor)

    frames = queued(queue)
    assert appended == 3
    assert [frame.header.kind for frame in frames] == [
        KIND_SNAPSHOT_CHUNK,
        KIND_SNAPSHOT_CHUNK,
        KIND_SNAPSHOT_MANIFEST,
    ]
    manifest = json.loads(frames[-1].payload)
    assert manifest["size"] == 2 * CHUNK_SIZE_BYTES
    assert manifest["chunk_size"] == CHUNK_SIZE_BYTES
    assert manifest["chunks"] == [hashlib.sha256(block).hexdigest() for block in _blocks(2)]
    assert manifest["content_hash"] == hashlib.sha256(b"".join(_blocks(2))).hexdigest()
    assert cursor.snapshot_content_hash == manifest["content_hash"]


def test_only_the_changed_block_is_sent_again(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    session = tmp_path / "session.json"
    blocks = _blocks(3)
    session.write_bytes(b"".join(blocks))
    cursor = SessionCursor()
    tap = SnapshotTap(queue)
    tap.collect("session-1", session, cursor)
    before = len(queued(queue))

    changed = list(blocks)
    changed[1] = b"Z" * CHUNK_SIZE_BYTES
    session.write_bytes(b"".join(changed))
    os.utime(session, ns=(1, 1))

    appended = tap.collect("session-1", session, cursor)

    frames = queued(queue)[before:]
    assert appended == 2
    assert [frame.header.kind for frame in frames] == [KIND_SNAPSHOT_CHUNK, KIND_SNAPSHOT_MANIFEST]
    assert frames[0].payload == changed[1]
    assert json.loads(frames[-1].payload)["chunks"][1] == hashlib.sha256(changed[1]).hexdigest()


def test_an_unchanged_session_file_is_skipped_without_hashing(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    session = tmp_path / "session.json"
    session.write_bytes(b"".join(_blocks(1)))
    cursor = SessionCursor()
    tap = SnapshotTap(queue)
    tap.collect("session-1", session, cursor)
    before = len(queued(queue))

    assert tap.collect("session-1", session, cursor) == 0
    assert len(queued(queue)) == before


def test_a_missing_session_file_produces_nothing(tmp_path: Path, queue: DurableWAL) -> None:
    assert SnapshotTap(queue).collect("session-1", tmp_path / "absent.json", SessionCursor()) == 0
