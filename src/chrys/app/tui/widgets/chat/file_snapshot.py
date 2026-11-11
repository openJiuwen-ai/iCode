# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Disk-backed file snapshot payloads for chat file-tool renderers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chrys.foundation.config.process_settings import process_settings
from chrys.foundation.text.encoding import decode_bytes


def file_snapshot_inline_char_limit() -> int:
    """Maximum combined before/after snapshot bytes kept inline by tool records.

    A call rather than the import-time constant it used to be: the value is
    still fixed for the process, but it is now read *after* bootstrap has
    resolved the settings instead of at import, which is well before.
    """
    return process_settings().chat_file_snapshot_inline_chars


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FileSnapshotRef:
    """Reference to before/after snapshot blobs stored under a session's mutations directory."""

    mutations_dir: Path
    before_hash: str | None
    after_hash: str | None

    def _read_blob(self, content_hash: str | None) -> str:
        if not content_hash or not _SHA256_RE.fullmatch(content_hash):
            return ""
        blob_path = self.mutations_dir / content_hash
        try:
            data = blob_path.read_bytes()
        except OSError:
            return ""
        return decode_bytes(data)

    def blob_size(self, content_hash: str | None) -> int:
        """Return the blob size in bytes, or 0 when the blob is absent."""
        if not content_hash or not _SHA256_RE.fullmatch(content_hash):
            return 0
        try:
            return (self.mutations_dir / content_hash).stat().st_size
        except OSError:
            return 0

    @property
    def total_blob_size(self) -> int:
        """Combined before/after blob size in bytes."""
        return self.blob_size(self.before_hash) + self.blob_size(self.after_hash)

    def resolve(self) -> tuple[str, str]:
        """Read and decode the before/after snapshot text from disk."""
        return (self._read_blob(self.before_hash), self._read_blob(self.after_hash))


FileSnapshotPayload = tuple[str, str] | FileSnapshotRef
type FileSnapshotKey = str | tuple[Literal["operation"], str]
"""Legacy provider ids are strings; operation ids have a disjoint tagged key."""


def _text_snapshot_size(snapshot: tuple[str, str]) -> int:
    """Return the UTF-8 byte size used for live decoded snapshot cap checks."""
    before, after = snapshot
    return len(before.encode("utf-8", errors="surrogatepass")) + len(after.encode("utf-8", errors="surrogatepass"))


def snapshot_payload_from_hashes(
    mutations_dir: Path,
    before_hash: str | None,
    after_hash: str | None,
    *,
    inline_char_limit: int | None = None,
) -> FileSnapshotPayload:
    """Return an inline snapshot only when it fits the configured memory cap."""
    cap = file_snapshot_inline_char_limit() if inline_char_limit is None else inline_char_limit
    ref = FileSnapshotRef(mutations_dir=mutations_dir, before_hash=before_hash, after_hash=after_hash)
    if ref.total_blob_size <= cap:
        return ref.resolve()
    return ref


def should_externalize_snapshot(snapshot: tuple[str, str]) -> bool:
    """Return true when a decoded snapshot should be stored as a disk-backed ref."""
    return _text_snapshot_size(snapshot) > file_snapshot_inline_char_limit()
