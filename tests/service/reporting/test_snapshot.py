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


class _StaleTap(SnapshotTap):
    """A tap whose hash pass reports an older version than the file holds.

    That is exactly the state a save landing between the two opens leaves, so
    this is the red path for the per-block digest check.
    """

    def __init__(self, wal: DurableWAL, stale: tuple) -> None:
        super().__init__(wal)
        self._stale = stale

    def _hash_chunks(self, path: Path) -> tuple[list, int, str | None]:
        return self._stale


def test_a_save_that_lands_between_the_two_opens_is_not_shipped(
    tmp_path: Path, queue: DurableWAL, queued: Callable[[DurableWAL], list[Frame]]
) -> None:
    session = tmp_path / "session.json"
    stale_file = tmp_path / "stale.json"
    stale_file.write_bytes(b"A" * CHUNK_SIZE_BYTES)
    # The hashed version of the file, captured before the save replaced it.
    stale = SnapshotTap(queue)._hash_chunks(stale_file)
    session.write_bytes(b"B" * CHUNK_SIZE_BYTES)
    os.utime(session, ns=(1, 1))  # defeat the (size, mtime) fast path
    tap = _StaleTap(queue, stale)
    cursor = SessionCursor()

    assert tap.collect("session-1", session, cursor) == 0
    assert queued(queue) == []
    assert cursor.snapshot_content_hash == ""

    # The next tap hashes the current version and ships it whole.
    assert SnapshotTap(queue).collect("session-1", session, cursor) == 2
    frames = queued(queue)
    assert frames[0].payload == b"B" * CHUNK_SIZE_BYTES
