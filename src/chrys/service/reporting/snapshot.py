# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``session.json`` snapshot tap: changed 256 KiB blocks plus a manifest.

The store replaces the file atomically, so one open always sees one consistent
version. Only the blocks whose hash changed since the last capture are queued,
block by block as they are read; the manifest always lists every block, which
is the completeness anchor the server assembles against.

Two opens are involved (hash the whole file, then read the changed blocks), so
a save that lands between them is detected rather than shipped: a re-read block
whose digest no longer matches abandons the pass — the manifest never names
bytes it does not hold, and the next round taps the newer version. Blocks a
replaced file leaves unread were never re-read, so the manifest stays
self-consistent either way.
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

READ_ATTEMPTS: Final = 2
"""How many times a mid-read file replacement is retried within one tap."""


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
        for _attempt in range(READ_ATTEMPTS):
            appended = self._collect_once(session_id, session_path, cursor, stat.st_mtime_ns)
            if appended is not None:
                return appended
        return 0

    def _collect_once(
        self,
        session_id: str,
        session_path: Path,
        cursor: SessionCursor,
        mtime_ns: int,
    ) -> int | None:
        """One hash-then-read pass; ``None`` asks for a retry after a mid-read save."""
        wal = self._wal
        chunks, size, content_hash = self._hash_chunks(session_path)
        if content_hash is None:
            return 0
        appended = 0
        previous = cursor.snapshot_chunk_hashes
        try:
            with session_path.open("rb") as handle:
                for chunk in chunks:
                    if chunk.index < len(previous) and previous[chunk.index] == chunk.sha256:
                        continue
                    handle.seek(chunk.index * CHUNK_SIZE_BYTES)
                    block = handle.read(CHUNK_SIZE_BYTES)
                    if hashlib.sha256(block).hexdigest() != chunk.sha256:
                        return None
                    wal.append(
                        [
                            Frame.build(
                                KIND_SNAPSHOT_CHUNK,
                                session_id,
                                scope_snapshot_chunk(content_hash, chunk.index),
                                block,
                            )
                        ]
                    )
                    appended += 1
        except OSError:
            return 0
        manifest = {
            "content_hash": content_hash,
            "size": size,
            "chunk_size": CHUNK_SIZE_BYTES,
            "chunks": [chunk.sha256 for chunk in chunks],
        }
        manifest_frames = [
            Frame.build(
                KIND_SNAPSHOT_MANIFEST,
                session_id,
                scope_snapshot_manifest(content_hash),
                canonical_json_bytes(manifest),
            )
        ]
        wal.append(manifest_frames)
        cursor.snapshot_size = size
        cursor.snapshot_mtime_ns = mtime_ns
        cursor.snapshot_content_hash = content_hash
        cursor.snapshot_chunk_hashes = [chunk.sha256 for chunk in chunks]
        return appended + len(manifest_frames)

    def _hash_chunks(self, path: Path) -> tuple[list[_Chunk], int, str | None]:
        """Hash the file block by block, streaming, without loading it whole.

        The byte count returned beside the hashes is the size of exactly the
        version that was hashed — the manifest never mixes facts from two opens.
        """
        digest = hashlib.sha256()
        chunks: list[_Chunk] = []
        size = 0
        index = 0
        try:
            with path.open("rb") as handle:
                while True:
                    block = handle.read(CHUNK_SIZE_BYTES)
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
                    chunks.append(_Chunk(index=index, sha256=hashlib.sha256(block).hexdigest()))
                    index += 1
        except OSError:
            return [], 0, None
        return chunks, size, digest.hexdigest()
