# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session collection cursors.

One small JSON file per session holds everything the collectors must remember
between runs: the trajectory byte offset and last sequence, the snapshot's chunk
hashes, the workspace signature and the block hashes already sent. Losing it is
recoverable by design — the trajectory replays from zero with ``reset`` and the
manifest endpoint names the blocks the server still wants.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.util.session_ids import session_short_id

logger = logging.getLogger(__name__)

CURSOR_FILE_SUFFIX = ".json"


@dataclass(slots=True)
class SessionCursor:
    """What one session's collectors have already handled."""

    trajectory_offset: int = 0
    trajectory_last_sequence: int = 0
    trajectory_size: int = 0
    trajectory_mtime_ns: int = 0
    snapshot_size: int = 0
    snapshot_mtime_ns: int = 0
    snapshot_content_hash: str = ""
    snapshot_chunk_hashes: list[str] = field(default_factory=list)
    workspace_signature: str = ""
    workspace_manifest_sha256: str = ""
    workspace_file_shas: list[str] = field(default_factory=list)
    workspace_kind: str = ""
    repository_identities: list[str] = field(default_factory=list)
    agent_config_sha256: str = ""
    agent_config_size: int = 0
    environment_sent: bool = False
    repository_sent: bool = False


_STRING_FIELDS = frozenset(
    {
        "snapshot_content_hash",
        "workspace_signature",
        "workspace_manifest_sha256",
        "workspace_kind",
        "agent_config_sha256",
    }
)
_INT_FIELDS = frozenset(
    {
        "trajectory_offset",
        "trajectory_last_sequence",
        "trajectory_size",
        "trajectory_mtime_ns",
        "snapshot_size",
        "snapshot_mtime_ns",
        "agent_config_size",
    }
)
_BOOL_FIELDS = frozenset({"environment_sent", "repository_sent"})
_LIST_FIELDS = frozenset({"snapshot_chunk_hashes", "workspace_file_shas", "repository_identities"})


def cursor_from_document(document: dict[str, object]) -> SessionCursor:
    """Rebuild a cursor from a JSON document, ignoring fields this build cannot trust."""
    cursor = SessionCursor()
    for name in _STRING_FIELDS:
        value = document.get(name)
        if isinstance(value, str):
            setattr(cursor, name, value)
    for name in _INT_FIELDS:
        value = document.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            setattr(cursor, name, value)
    for name in _BOOL_FIELDS:
        value = document.get(name)
        if isinstance(value, bool):
            setattr(cursor, name, value)
    for name in _LIST_FIELDS:
        value = document.get(name)
        if isinstance(value, list):
            setattr(cursor, name, [item for item in value if isinstance(item, str)])
    return cursor


class SessionCursorStore:
    """Load and save :class:`SessionCursor` files beside the queue."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path_for(self, session_id: str) -> Path:
        """The cursor file of *session_id*."""
        return self._directory / f"{session_short_id(session_id)}{CURSOR_FILE_SUFFIX}"

    def load(self, session_id: str) -> SessionCursor:
        """Read the cursor, or a fresh one when it is absent or unusable."""
        path = self.path_for(session_id)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return SessionCursor()
        if not isinstance(document, dict):
            return SessionCursor()
        return cursor_from_document(document)

    def save(self, session_id: str, cursor: SessionCursor) -> None:
        """Write the cursor durably; failure is logged, never raised."""
        try:
            atomic_write_text(self.path_for(session_id), json.dumps(asdict(cursor), ensure_ascii=True) + "\n")
        except OSError:
            logger.warning("Could not persist the reporting cursor for %s", session_id, exc_info=True)

    def delete(self, session_id: str) -> None:
        """Remove the cursor of a deleted session."""
        try:
            self.path_for(session_id).unlink()
        except OSError:
            return
