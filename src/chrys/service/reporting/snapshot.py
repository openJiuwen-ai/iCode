# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``session.json`` snapshot tap: changed 256 KiB blocks plus a manifest.

The store replaces the file atomically, so a read is always one consistent
version. Only the blocks whose hash changed since the last capture are queued;
the manifest always lists every block, which is the completeness anchor the
server assembles against.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chrys.foundation.reporting.frames import (
    KIND_SNAPSHOT_CHUNK,
    KIND_SNAPSHOT_MANIFEST,
    Frame,
    canonical_json_bytes,
    scope_snapshot_chunk,
    scope_snapshot_manifest,
)
from chrys.foundation.reporting.wal import DurableWAL
from chrys.service.reporting.cursors import SessionCursor

CHUNK_SIZE_BYTES: Final = 256 * 1024


@dataclass(frozen=True, slots=True)
class _Chunk:
    index: int
    sha256: str


class SnapshotTap:
    """Queue changed snapshot blocks and their manifest."""

    def __init__(self, wal: DurableWAL) -> None:
        self._wal = wal

    def collect(self, session_id: str, session_path: Path, cursor: SessionCursor) -> int:
        """Queue the changed blocks and the manifest; return the units appended."""
        try:
            stat = session_path.stat()
        except OSError:
            return 0
        if stat.st_size == cursor.snapshot_size and stat.st_mtime_ns == cursor.snapshot_mtime_ns:
            return 0
        chunks, content_hash = self._hash_chunks(session_path)
        if content_hash is None:
            return 0
        frames: list[Frame] = []
        previous = cursor.snapshot_chunk_hashes
        for chunk in chunks:
            unchanged = chunk.index < len(previous) and previous[chunk.index] == chunk.sha256
            if unchanged:
                continue
            payload = self._read_chunk(session_path, chunk.index)
            if payload is None:
                continue
            frames.append(
                Frame.build(
                    KIND_SNAPSHOT_CHUNK,
                    session_id,
                    scope_snapshot_chunk(content_hash, chunk.index),
                    payload,
                )
            )
        manifest = {
            "content_hash": content_hash,
            "size": stat.st_size,
            "chunk_size": CHUNK_SIZE_BYTES,
            "chunks": [chunk.sha256 for chunk in chunks],
        }
        frames.append(
            Frame.build(
                KIND_SNAPSHOT_MANIFEST,
                session_id,
                scope_snapshot_manifest(content_hash),
                canonical_json_bytes(manifest),
            )
        )
        self._wal.append(frames)
        cursor.snapshot_size = stat.st_size
        cursor.snapshot_mtime_ns = stat.st_mtime_ns
        cursor.snapshot_content_hash = content_hash
        cursor.snapshot_chunk_hashes = [chunk.sha256 for chunk in chunks]
        return len(frames)

    def _hash_chunks(self, path: Path) -> tuple[list[_Chunk], str | None]:
        """Hash the file block by block, streaming, without loading it whole."""
        digest = hashlib.sha256()
        chunks: list[_Chunk] = []
        index = 0
        try:
            with path.open("rb") as handle:
                while True:
                    block = handle.read(CHUNK_SIZE_BYTES)
                    if not block:
                        break
                    digest.update(block)
                    chunks.append(_Chunk(index=index, sha256=hashlib.sha256(block).hexdigest()))
                    index += 1
        except OSError:
            return [], None
        return chunks, digest.hexdigest()

    def _read_chunk(self, path: Path, index: int) -> bytes | None:
        try:
            with path.open("rb") as handle:
                handle.seek(index * CHUNK_SIZE_BYTES)
                return handle.read(CHUNK_SIZE_BYTES)
        except OSError:
            return None
