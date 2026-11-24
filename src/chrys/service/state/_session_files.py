# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leaf filesystem primitives for session persistence."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeGuard

if TYPE_CHECKING:
    from collections.abc import Callable

from chrys.foundation.models.workdir_migration import BackupOriginal, ReportReadError, source_edit
from chrys.foundation.platform.files import _atomic_write_bytes as _common_atomic_write_bytes
from chrys.foundation.platform.files import atomic_write_text as _common_atomic_write_text
from chrys.foundation.platform.files import require_writable_file
from chrys.foundation.util.lock import FileLock as FileLock
from chrys.foundation.util.session_ids import session_short_id as _session_short_id
from chrys.service.state.session_mru import SESSION_MRU_FILE_NAME


def _is_string_keyed_dict(value: object) -> TypeGuard[dict[str, Any]]:
    """Narrow serialized state objects, whose JSON keys are strings."""
    return isinstance(value, dict)


logger = logging.getLogger(__name__)

# ``meta.schema_version`` is the on-disk layout version of
# ``session.json``.  Bump whenever the meta shape or serialized state
# shape changes in a way older readers can't cope with.  Pre-versioned
# files (written before this field existed) read back with
# ``schema_version == 0`` via ``meta.get("schema_version", 0)`` and
# used the keys ``display_name`` / ``profile_history``; readers still
# accept those names and surface them via the v1 attributes.
SESSION_SCHEMA_VERSION = 1

SESSION_WRITE_LOCK_TIMEOUT_SECONDS = 10.0
SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS = 5.0
# Folder-mtime slack for the post-lookup MRU sweep: covers coarse filesystem
# timestamps and the gap between an envelope's ``updated_at`` and its write.
_MRU_SWEEP_SLACK = timedelta(seconds=2)
SESSION_FILE_NAME = "session.json"
SESSION_BACKUP_FILE_NAME = "session.json.bak"
SESSION_RECOVERY_FILE_NAME = "session.recovery.json"
SESSION_FORK_MAX_ID_ATTEMPTS = 10
RAW_HTTP_LOG_FILE_NAME = "llm_raw_http.jsonl"
SESSION_CHECKPOINT_ID_KEY = "session_checkpoint_id"


@dataclass(frozen=True)
class SessionCheckpoint:
    """Identity of one persisted ``session.json`` revision.

    ``session_checkpoint_id`` is minted per save and written at the top level
    of the envelope; ``content_hash`` digests the exact bytes written, so a
    derived trajectory summary can name the revision it was computed from.
    """

    session_checkpoint_id: str
    content_hash: str


class SessionNotFoundError(FileNotFoundError):
    """Raised when a requested session does not exist."""


class SessionForkError(RuntimeError):
    """Raised when a session cannot be forked safely."""


def _session_lock_path(sessions_dir: Path, session_id: str, suffix: str, *, follow_session_links: bool = True) -> Path:
    name = _session_short_id(session_id)
    directory = sessions_dir / name
    # Root-level locks survive a session delete/recreate. All directory links
    # and Windows junctions to a writable archive must use its physical root.
    if follow_session_links and name and (directory.is_symlink() or directory.is_junction()):
        target = directory.resolve()
        sessions_dir, name = target.parent, target.name
    return sessions_dir / ".locks" / f"{name}.{suffix}"


def session_write_lock_path(sessions_dir: Path, session_id: str, *, follow_session_links: bool = True) -> Path:
    """Return the root-level write lock path for *session_id*."""
    return _session_lock_path(sessions_dir, session_id, "write.lock", follow_session_links=follow_session_links)


def session_active_lock_path(sessions_dir: Path, session_id: str, *, follow_session_links: bool = True) -> Path:
    """Return the root-level active-session lock path for *session_id*."""
    return _session_lock_path(sessions_dir, session_id, "active.lock", follow_session_links=follow_session_links)


def session_active_owner_path(sessions_dir: Path, session_id: str) -> Path:
    """Return the owner metadata path paired with the active-session lock."""
    return _session_lock_path(sessions_dir, session_id, "active.json")


def linked_recovery_active_lock(path: Path, target: Path) -> Path | None:
    """Find the physical archive that writes an individually linked recovery file."""
    candidate = target.with_name(SESSION_RECOVERY_FILE_NAME)
    if (
        target.name == candidate.name or (candidate.is_file() and target.samefile(candidate))
    ) and not path.parent.samefile(target.parent):
        # A relocated session directory may have any name. Use that whole name
        # just as a writer reaching it through a directory link would.
        return target.parent.parent / ".locks" / f"{target.parent.name}.active.lock"
    return None


def parse_snapshot_turn(path: Path) -> int:
    """Parse rollback snapshot names into ``N``; return -1 on malformed names.

    Current snapshots are named ``turn_{N}.json``.  Older development
    builds used bare numeric names (``{N}.json``), so readers accept both
    forms while writers continue producing the prefixed shape.
    """
    try:
        stem = path.stem
        raw = stem.split("_", 1)[1] if stem.startswith("turn_") else stem
        return int(raw)
    except IndexError, ValueError:
        return -1


def _ensure_lock_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def atomic_copy_file(source: Path, dest: Path) -> None:
    """Copy *source* to *dest* without ever exposing a partial destination."""
    _common_atomic_write_bytes(dest, source.read_bytes())


def _atomic_write_text(path: Path, payload: str, *, encoding: str = "utf-8") -> bytes:
    return _common_atomic_write_text(path, payload, encoding=encoding)


def dump_session_envelope(envelope: dict[str, Any]) -> str:
    """Serialize a session checkpoint for the atomic text writer."""
    return json.dumps(envelope, indent=2, ensure_ascii=False)


def read_session_envelope(path: Path, *, probe_legacy_source: bool = False) -> tuple[bytes, dict[str, Any] | None]:
    """Read a session file using normal filesystem access, including symlink resolution."""
    raw = path.read_bytes()
    # Session writers spell both tokens literally. Escapes can hide either token in imported JSON.
    if probe_legacy_source and (b'"source_kind"' not in raw or b'"project"' not in raw) and b"\\u" not in raw:
        return raw, None
    envelope = json.loads(raw.decode("utf-8"))
    if probe_legacy_source and not isinstance(envelope, dict):
        raise ValueError("Expected a JSON object")
    return raw, envelope if isinstance(envelope, dict) else None


def migrate_session_envelopes(session_dir: Path, *, backup: BackupOriginal, on_read_error: ReportReadError) -> bool:
    """Upgrade each saved envelope without touching the other checkpoint or recovery files."""

    def candidate(path: Path) -> tuple[bytes, dict[str, Any]] | None:
        try:
            # Leave linked checkpoints in place. Normal restore can read either spelling through the link.
            if path.is_symlink():
                return None
            raw, envelope = read_session_envelope(path, probe_legacy_source=True)
            if envelope is None:
                return None
        except (OSError, ValueError, RecursionError) as error:
            if not isinstance(error, OSError) or error.errno not in {errno.ENOENT, errno.ENOTDIR}:
                on_read_error(path, error)
            return None
        meta, state = envelope.get("meta"), envelope.get("state")
        if (
            isinstance(meta, dict)
            and meta.get("kind") == "workflow"
            and isinstance(state, dict)
            and source_edit(state.get("identity"))
        ):
            return raw, envelope
        return None

    changed = False
    for name in (SESSION_FILE_NAME, SESSION_BACKUP_FILE_NAME):
        path = session_dir / name
        if candidate(path) is None:
            continue
        try:
            lock = session_write_lock_path(session_dir.parent, session_dir.name)
            _ensure_lock_parent(lock)
            with FileLock(lock, timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
                record = candidate(path)
                if record is None:
                    continue
                raw, envelope = record
                require_writable_file(path)
                backup(path, raw)
                _common_atomic_write_text(path, dump_session_envelope(envelope), create_parents=False)
                changed = True
        except OSError, ValueError, TypeError, RecursionError:
            continue
    return changed


def session_checkpoint_of(envelope: dict[str, Any], written: bytes) -> SessionCheckpoint:
    """The checkpoint identity of *envelope* as the bytes *written* to disk.

    The digest covers what the atomic writer produced rather than a second
    encoding of the same string: the two disagree on a lone surrogate, and a
    hash of bytes that never landed would never match the file it names.
    """
    checkpoint_id = envelope.get(SESSION_CHECKPOINT_ID_KEY)
    if not isinstance(checkpoint_id, str) or not checkpoint_id:
        checkpoint_id = ""
    digest = hashlib.sha256(written).hexdigest()
    return SessionCheckpoint(session_checkpoint_id=checkpoint_id, content_hash=digest)


def session_dir_has_artifacts(session_dir: Path) -> bool:
    """Whether *session_dir* holds a primary, backup, recovery or snapshot envelope."""
    return (
        (session_dir / SESSION_FILE_NAME).exists()
        or (session_dir / SESSION_BACKUP_FILE_NAME).exists()
        or (session_dir / SESSION_RECOVERY_FILE_NAME).exists()
        or any(parse_snapshot_turn(snap) >= 1 for snap in (session_dir / "snapshots").glob("*.json"))
    )


def session_dir_candidates(sessions_dir: Path) -> list[Path]:
    """Session folders under *sessions_dir* that plausibly hold a restorable session.

    Dot-prefixed entries (``.locks``, temp/partial dirs) are never sessions.
    """
    # New format: {session_id}/session.json
    return sorted(
        p
        for p in sessions_dir.iterdir()
        if p.is_dir() and p.name != ".locks" and not p.name.startswith(".") and session_dir_has_artifacts(p)
    )


def legacy_session_files(sessions_dir: Path) -> list[Path]:
    """Root-level ``*.json`` legacy flat-file sessions (never the MRU index)."""
    return sorted(p for p in sessions_dir.glob("*.json") if p.name != SESSION_MRU_FILE_NAME)


def make_junction_dropping_ignore() -> Callable[[str, list[str]], set[str]]:
    """Return a ``shutil.copytree`` ignore callback that drops NT junctions at EVERY level.

    ``copytree(symlinks=True)`` preserves POSIX symlinks verbatim but FOLLOWS
    NT directory junctions (``os.path.islink()`` is False for them), so a
    junction planted anywhere the copy reaches would be materialized as a
    real directory or, if it points at an ancestor, recurse to disk
    exhaustion. chrys never creates junctions, so excluding every one is
    loss-free; ``os.path.isjunction`` is always False on POSIX and for real
    dirs.
    """

    def _ignore(path: str, names: list[str]) -> set[str]:
        return {name for name in names if os.path.isjunction(os.path.join(path, name))}

    return _ignore
