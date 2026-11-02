# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""StateStore — protocol and implementations for session persistence."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os as os
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.platform.files import atomic_write_text as _common_atomic_write_text
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.session.side_questions import SideQuestionSessionSeed, load_side_questions, write_side_questions
from chrys.service.state._fork import SessionForkMixin
from chrys.service.state._session_files import _MRU_SWEEP_SLACK as _MRU_SWEEP_SLACK
from chrys.service.state._session_files import RAW_HTTP_LOG_FILE_NAME as RAW_HTTP_LOG_FILE_NAME
from chrys.service.state._session_files import (
    SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS as SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS,
)
from chrys.service.state._session_files import SESSION_BACKUP_FILE_NAME as SESSION_BACKUP_FILE_NAME
from chrys.service.state._session_files import SESSION_CHECKPOINT_ID_KEY as SESSION_CHECKPOINT_ID_KEY
from chrys.service.state._session_files import SESSION_FILE_NAME as SESSION_FILE_NAME
from chrys.service.state._session_files import SESSION_FORK_MAX_ID_ATTEMPTS as SESSION_FORK_MAX_ID_ATTEMPTS
from chrys.service.state._session_files import SESSION_RECOVERY_FILE_NAME as SESSION_RECOVERY_FILE_NAME
from chrys.service.state._session_files import SESSION_SCHEMA_VERSION as SESSION_SCHEMA_VERSION
from chrys.service.state._session_files import SESSION_WRITE_LOCK_TIMEOUT_SECONDS as SESSION_WRITE_LOCK_TIMEOUT_SECONDS
from chrys.service.state._session_files import FileLock as FileLock
from chrys.service.state._session_files import SessionCheckpoint as SessionCheckpoint
from chrys.service.state._session_files import SessionForkError as SessionForkError
from chrys.service.state._session_files import SessionNotFoundError as SessionNotFoundError
from chrys.service.state._session_files import _atomic_write_text as _atomic_write_text
from chrys.service.state._session_files import _ensure_lock_parent as _ensure_lock_parent
from chrys.service.state._session_files import _is_string_keyed_dict as _is_string_keyed_dict
from chrys.service.state._session_files import _session_short_id as _session_short_id
from chrys.service.state._session_files import atomic_copy_file as atomic_copy_file
from chrys.service.state._session_files import legacy_session_files as legacy_session_files
from chrys.service.state._session_files import make_junction_dropping_ignore as make_junction_dropping_ignore
from chrys.service.state._session_files import parse_snapshot_turn as parse_snapshot_turn
from chrys.service.state._session_files import session_active_lock_path as session_active_lock_path
from chrys.service.state._session_files import session_active_owner_path as session_active_owner_path
from chrys.service.state._session_files import session_checkpoint_of as session_checkpoint_of
from chrys.service.state._session_files import session_dir_candidates as session_dir_candidates
from chrys.service.state._session_files import session_dir_has_artifacts as session_dir_has_artifacts
from chrys.service.state._session_files import session_write_lock_path as session_write_lock_path
from chrys.service.state._session_meta import ChatSessionMeta as ChatSessionMeta
from chrys.service.state._session_meta import SessionMeta as SessionMeta
from chrys.service.state._session_meta import SessionMetaMixin, copy_session_meta, resolve_session_kind, title_from_text
from chrys.service.state._session_meta import WorkflowSessionMeta as WorkflowSessionMeta
from chrys.service.state._session_meta import _earliest_history_created_at as _earliest_history_created_at
from chrys.service.state._session_meta import _extract_title as _extract_title
from chrys.service.state._session_meta import _first_message_created_at as _first_message_created_at
from chrys.service.state._session_meta import _is_visible_message as _is_visible_message
from chrys.service.state._session_meta import _message_created_at as _message_created_at
from chrys.service.state._session_meta import _parse_session_timestamp as _parse_session_timestamp
from chrys.service.state._session_meta import recorded_surface as recorded_surface
from chrys.service.state.serializers import deserialize_state, serialize_state
from chrys.service.state.session_catalog import (
    CatalogEntry,
    FileSignature,
    RunListing,
    RunListingKey,
    SessionCatalogFile,
    file_signature,
    is_recordable,
)
from chrys.service.state.session_listing import (
    SESSION_PAGE_SIZE,
    SessionListing,
    SessionListingEntry,
    SessionPage,
    page_slice,
)
from chrys.service.state.session_mru import SessionMruEntry, SessionMruIndex, coerce_utc, sort_entries
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.trajectory.state import TurnRecord, turn_records
from chrys.service.workflows.history import read_workflow_meta, read_workflow_run
from chrys.service.workflows.layout import EVENTS_FILE, HEADER_FILE, run_dir

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionTranscript:
    """A chat session's replayable history and the turn identity behind it.

    Both come from one read of the session envelope, so the turns always
    describe the messages beside them — a separate read could see the next
    turn's registry against this turn's messages.
    """

    messages: list[dict[str, Any]]
    turns: list[TurnRecord]


# The catalog is rewritten whole, so after its first merge a process merges a
# trickle of new entries (the session a turn just changed) at most this often;
# a batch worth sharing (a cold listing) merges at once. Unmerged entries are
# only a peer's cache miss. Pruning a gone session's entry is never deferred:
# it holds prompt excerpts.
SESSION_CATALOG_COMMIT_INTERVAL_SECONDS = 60.0
SESSION_CATALOG_EAGER_COMMIT_ENTRIES = 16


@runtime_checkable
class StateStore(Protocol):
    """Protocol for session state persistence."""

    @property
    def sessions_root(self) -> Path: ...
    def session_folder_cwds(self) -> dict[str, str]: ...
    def session_dir(self, session_id: str) -> Path: ...
    async def save_side_question(
        self,
        session_id: str,
        entry: SideQuestionEntry,
        seed: SideQuestionSessionSeed,
        *,
        cancelled: threading.Event | None = None,
    ) -> None: ...
    async def save_session(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        agent_profile: str = "",
        agent_display_name: str = "",
        agent_profile_id: str = "",
        agent_profile_fingerprint: str = "",
        primary_cwd: str = "",
        working_dirs: list[str] | None = None,
        title: str = "",
        agent_profile_history: list[str] | None = None,
        model_provider: str = "",
        model_api_style: str | None = None,
        model_id: str = "",
        model_profile_id: str = "",
        model_base_url: str = "",
        model_profile_fingerprint: str | None = None,
        service_session_id: str | None = None,
        parent_session_id: str = "",
        last_surface: SessionSurface | None = None,
    ) -> SessionCheckpoint | None: ...
    async def save_workflow_session(
        self, session_id: str, state: WorkflowSessionState, *, title: str = ""
    ) -> SessionCheckpoint | None: ...

    def save_recovery_session(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        agent_profile: str = "",
        agent_display_name: str = "",
        agent_profile_id: str = "",
        agent_profile_fingerprint: str = "",
        primary_cwd: str = "",
        working_dirs: list[str] | None = None,
        title: str = "",
        agent_profile_history: list[str] | None = None,
        model_provider: str = "",
        model_api_style: str | None = None,
        model_id: str = "",
        model_profile_id: str = "",
        model_base_url: str = "",
        model_profile_fingerprint: str | None = None,
        parent_session_id: str = "",
        last_surface: SessionSurface | None = None,
    ) -> None: ...
    async def load_recovery_session(self, session_id: str) -> dict[str, Any] | None: ...
    async def load_recovery_session_meta(self, session_id: str) -> SessionMeta | None: ...
    def delete_recovery_session(self, session_id: str) -> None: ...
    async def recovery_session_wins(self, session_id: str) -> bool: ...
    async def load_workflow_session(self, session_id: str) -> WorkflowSessionState | None: ...
    async def load_session(self, session_id: str, *, prefer_recovery: bool = False) -> dict[str, Any] | None: ...
    async def load_session_raw(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> list[dict[str, Any]] | None: ...
    async def load_session_transcript(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> SessionTranscript | None: ...
    async def load_session_meta(
        self, session_id: str, *, prefer_recovery: bool = False, strict: bool = False
    ) -> SessionMeta | None: ...

    async def list_sessions(self, *, kind: Literal["chat", "workflow"] | None = None) -> list[SessionMeta]: ...
    async def load_latest_session_id(self, *, chat_only: bool = False) -> str | None: ...
    async def open_session_listing(self, *, kind: Literal["chat", "workflow"]) -> SessionListing: ...
    async def load_session_page(
        self,
        listing: SessionListing,
        *,
        surfaces: Collection[SessionSurface],
        page: int = 1,
        page_size: int = SESSION_PAGE_SIZE,
    ) -> SessionPage: ...
    async def update_session_titles(
        self,
        session_id: str,
        *,
        custom_title: str | None = None,
        generated_title: str | None = None,
    ) -> SessionMeta | None: ...
    def fork_session(self, parent_session_id: str, *, last_surface: SessionSurface | None = None) -> str: ...
    async def delete_session(self, session_id: str, *, allow_active: bool = False) -> None: ...


def _dir_size(path: Path) -> int:
    """Total bytes of the regular files under *path*; unreadable parts count as empty.

    An explicit ``os.scandir`` stack: entry types come from the directory
    listing, so only files cost a ``stat``, and links and junctions are
    never followed.
    """
    total = 0
    pending = [os.fspath(path)]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_junction() or entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _is_run_name(run_id: str) -> bool:
    """Run ids come from storage; a corrupt envelope must not escape its session folder."""
    return bool(run_id) and Path(run_id).name == run_id and run_id not in (".", "..")


def _newer_of(current: SessionMruEntry | None, candidate: SessionMruEntry) -> SessionMruEntry:
    return candidate if current is None else sort_entries([current, candidate])[0]


class JsonFileStateStore(SessionForkMixin, SessionMetaMixin):
    """JSON file-based state store.

    Stores sessions as ``{short_session_id}/session.json`` folders with a
    sidecar backup and root-level locks for cross-process coordination.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self._dir = resolve_sessions_dir() if directory is None else Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Custom titles set before a session's first save.  A never-saved
        # session must not be materialized on disk just for its title (it
        # would list as a ghost entry and may still be discarded), so the
        # title waits here until the first full save merges it.
        self._pending_custom_titles: dict[str, str] = {}
        # Listing cache: folder name -> catalog entry (``session.json``
        # signature + listing meta without size).  Holds ONLY the small
        # ``SessionMeta`` dataclasses — the multi-MB envelope payloads are
        # parsed transiently and dropped — so memory stays bounded even with
        # thousands of sessions.  Entries also come from, and are merged back
        # into, the persisted catalog (``session_catalog.py``); ``_catalog_pending``
        # names the ones not yet written there, ``_catalog_removals_owed`` the
        # deleted or vanished sessions not yet removed from it.  Legacy flat files are cached
        # separately and never persisted.  Accessed from ``asyncio.to_thread``
        # workers, so all reads/writes/evictions take ``_meta_cache_lock``; a
        # lost check-then-set race costs at most one redundant re-parse.
        self._meta_cache: dict[str, CatalogEntry] = {}
        self._legacy_meta_cache: dict[str, tuple[FileSignature, SessionMeta]] = {}
        self._meta_cache_lock = threading.Lock()
        self._catalog = SessionCatalogFile(self._dir)
        self._catalog_signature: FileSignature | None = None
        self._catalog_pending: set[str] = set()
        self._catalog_removals_owed: set[str] = set()
        self._catalog_committed_at: float | None = None
        # The surface-carry fields of each sidecar this store wrote, keyed by
        # its folder (the short id: callers pass either id form) and matched
        # on the file's signature so checkpoints never re-parse their own
        # sidecar; one another process wrote (or rewrote) is parsed once.
        # Only read and written under the session's write lock.
        self._written_recovery_meta: dict[str, tuple[FileSignature, dict[str, Any]]] = {}
        # Derived per-root MRU index (``session_mru.json``) so ``/resume``
        # can find the newest session without parsing every envelope.
        # Session files stay authoritative; every index update is
        # best-effort via ``_note_mru`` and never fails a session operation.
        # Writers record *before* committing the envelope (inside the
        # session write lock) so a crash can only leave the index ahead of
        # disk — a state the lookup's verification tolerates — never behind.
        # Whatever still bypasses the index (an older chrys, a copied
        # folder, a record that failed) is caught by the lookup's sweep of
        # folders modified after the indexed winner.
        self._mru = SessionMruIndex(self._dir)
        self._sweep_trajectory_tombstones()

    def _sweep_trajectory_tombstones(self) -> None:
        """Finish logical deletes whose writer has since released its lease."""
        from chrys.service.trajectory.tombstone import sweep_tombstones

        try:
            removed = sweep_tombstones(self._dir)
        except Exception:
            logger.debug("Trajectory tombstone sweep failed under %s", self._dir, exc_info=True)
            return
        if removed:
            logger.info("Removed %d tombstoned session director%s", removed, "y" if removed == 1 else "ies")

    def session_dir(self, session_id: str) -> Path:
        """Return the folder for a session.

        Delegates folder-name derivation to :func:`chrys.foundation.util.session_ids.session_short_id`
        so callers that don't hold a store instance (engine fallback,
        agent_builder fallback) produce bit-identical paths.
        """
        return self._dir / _session_short_id(session_id)

    # Keep private alias for internal callers.
    _session_dir = session_dir

    def _session_file(self, session_id: str) -> Path:
        """Return the session.json path inside the session folder."""
        return self._session_dir(session_id) / SESSION_FILE_NAME

    def _backup_file(self, session_id: str) -> Path:
        """Return the session.json recovery backup path."""
        return self._session_dir(session_id) / SESSION_BACKUP_FILE_NAME

    def _recovery_file(self, session_id: str) -> Path:
        """Return the crash-recovery sidecar path."""
        return self._session_dir(session_id) / SESSION_RECOVERY_FILE_NAME

    def _write_lock_path(self, session_id: str) -> Path:
        """Return the write lock path for this store/session."""
        path = session_write_lock_path(self._dir, session_id)
        _ensure_lock_parent(path)
        return path

    def active_lock_path(self, session_id: str) -> Path:
        """Return the long-lived active-session lock path."""
        path = session_active_lock_path(self._dir, session_id)
        _ensure_lock_parent(path)
        return path

    def active_owner_path(self, session_id: str) -> Path:
        """Return the active-session owner metadata path."""
        path = session_active_owner_path(self._dir, session_id)
        _ensure_lock_parent(path)
        return path

    def _legacy_path(self, session_id: str) -> Path:
        """Return the old flat-file path for migration fallback."""
        return self._dir / f"{_session_short_id(session_id)}.json"

    def _legacy_session_files(self) -> list[Path]:
        """Root-level ``*.json`` legacy flat-file sessions (never the MRU index)."""
        return legacy_session_files(self._dir)

    def _note_mru[T](self, update: Callable[[], T]) -> T | None:
        """Apply a best-effort MRU index update; never fails the session operation.

        On an I/O error the index is invalidated so the next ``/resume``
        rebuilds it from a full scan.  A lock timeout means a stuck peer:
        invalidating would only wait on the same lock again inside the
        session write lock, so the update is just dropped — the lookup's
        modified-folder sweep still finds the committed session.  ``None``
        is returned in place of the update's result either way.
        """
        try:
            return update()
        except TimeoutError:
            logger.warning("Session MRU index %s is locked; skipping update", self._mru.path)
            return None
        except Exception:
            logger.warning("Failed to update session MRU index %s", self._mru.path, exc_info=True)
            with contextlib.suppress(Exception):
                self._mru.invalidate()
            return None

    def _record_mru(self, session_id: str, updated_at: datetime | None) -> None:
        """Index *session_id* at *updated_at*; call this before the envelope commit."""
        if updated_at is None:
            return
        self._note_mru(lambda: self._mru.record(session_id, updated_at))

    def _migrate_if_needed_unlocked(self, session_id: str) -> None:
        """Move a legacy flat-file session into folder format on first access."""
        legacy = self._legacy_path(session_id)
        session_file = self._session_file(session_id)
        if legacy.exists() and not session_file.exists():
            session_file.parent.mkdir(parents=True, exist_ok=True)
            # Record before the rename like every other commit: a backfill
            # scan racing this migration can miss the file in both its
            # folder and its legacy enumeration, and only the index entry
            # keeps the session from vanishing behind a complete index.
            self._record_mru(session_id, self._envelope_updated_at(self._read_json_file(legacy)))
            legacy.rename(session_file)

    def _migrate_if_needed(self, session_id: str) -> None:
        """Move a legacy flat-file session into folder format on first access."""
        if not self._legacy_path(session_id).exists():
            return
        with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            self._migrate_if_needed_unlocked(session_id)

    def _resolve_session_file(self, session_id: str) -> Path | None:
        """Return the session.json path, migrating from legacy if needed."""
        self._migrate_if_needed(session_id)
        path = self._session_file(session_id)
        return path if path.exists() else None

    def resolve_session_file(self, session_id: str) -> Path | None:
        """The session's ``session.json`` path; None when it has none.

        A session still in the legacy flat layout is migrated first, so this
        may write to disk and raise the I/O and lock errors migration raises.
        """
        return self._resolve_session_file(session_id)

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any] | None:
        """Read a JSON object from *path*, returning ``None`` on invalid input."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in session file %s", path)
            return None
        except OSError:
            return None
        return raw if isinstance(raw, dict) else None

    def _snapshot_recovery_files(self, session_id: str) -> list[Path]:
        """Return rollback snapshots newest-first for last-ditch session recovery."""
        snap_dir = self._session_dir(session_id) / "snapshots"
        if not snap_dir.is_dir():
            return []

        return sorted(
            (p for p in snap_dir.glob("*.json") if parse_snapshot_turn(p) >= 1),
            key=parse_snapshot_turn,
            reverse=True,
        )

    def _read_session_envelope(
        self,
        session_id: str,
        *,
        heal: bool = True,
        migrate: bool = True,
        include_snapshots: bool = True,
    ) -> dict[str, Any] | None:
        """Read a session envelope, falling back to backup/snapshots if needed."""
        envelope, _source = self._read_session_envelope_with_source(
            session_id, heal=heal, migrate=migrate, include_snapshots=include_snapshots
        )
        return envelope

    def _read_session_envelope_with_source(
        self,
        session_id: str,
        *,
        heal: bool = True,
        migrate: bool = True,
        include_snapshots: bool = True,
    ) -> tuple[dict[str, Any] | None, Literal["primary", "backup", "snapshot"]]:
        """Read a session envelope and name the file it came from."""
        if migrate:
            path = self._resolve_session_file(session_id)
        else:
            session_file = self._session_file(session_id)
            path = session_file if session_file.exists() else None
        backup = self._backup_file(session_id)

        candidates: list[tuple[Path, Literal["primary", "backup", "snapshot"]]] = []
        if path is not None:
            candidates.append((path, "primary"))
        if backup.exists():
            candidates.append((backup, "backup"))
        if include_snapshots:
            candidates.extend((snapshot, "snapshot") for snapshot in self._snapshot_recovery_files(session_id))
        if not candidates:
            return None, "primary"

        for candidate, source in candidates:
            envelope = self._read_json_file(candidate)
            if envelope is None:
                continue
            if heal and candidate != path:
                self._heal_session_file_from_candidate(session_id, self._session_file(session_id), envelope, candidate)
            return envelope, source
        return None, "primary"

    def _heal_session_file_from_candidate(
        self,
        session_id: str,
        path: Path,
        envelope: dict[str, Any],
        candidate: Path,
    ) -> None:
        """Best-effort repair of a corrupt primary session file."""
        try:
            payload = json.dumps(envelope, indent=2, ensure_ascii=False)
            with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
                current = self._read_json_file(path)
                if current is None:
                    _atomic_write_text(path, payload)
                    _atomic_write_text(self._backup_file(session_id), payload)
                    logger.warning("Recovered corrupt session %s from %s", session_id, candidate)
        except Exception:
            logger.warning("Failed to heal session %s from %s", session_id, candidate, exc_info=True)

    @staticmethod
    def _inheritable_meta(
        session_id: str,
        envelope: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Return metadata when the live envelope can be attributed to the session."""
        if envelope is None:
            return None
        meta = envelope.get("meta")
        if not isinstance(meta, dict) or meta.get("session_id") != session_id:
            return None
        return meta

    @classmethod
    def _title_patch_meta(
        cls,
        session_id: str,
        envelope: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Return attributable metadata whose required timestamps are parseable."""
        meta = cls._inheritable_meta(session_id, envelope)
        if meta is None:
            return None
        created_at = _parse_session_timestamp(meta.get("created_at"))
        updated_at = _parse_session_timestamp(meta.get("updated_at"))
        if created_at is None or updated_at is None:
            return None
        return meta

    def _read_inherited_live_meta_unlocked(
        self,
        session_id: str,
    ) -> tuple[dict[str, Any], str | None, bool]:
        """Return live metadata, creation time, and whether primary supplied the meta.

        This is deliberately separate from ``_read_session_envelope``: save
        inheritance must continue past a parseable but malformed primary to a
        healthy backup, while a timestamp defect repairs only that field. The
        immutable creation time may come from the backup when the selected
        primary meta lacks it. Rollback snapshots are never consulted.
        """
        inherited_meta: dict[str, Any] | None = None
        inherited_from_primary = False
        created_at: str | None = None
        primary_file = self._session_file(session_id)
        for path in (primary_file, self._backup_file(session_id)):
            envelope = self._read_json_file(path)
            meta = self._inheritable_meta(session_id, envelope)
            if meta is None:
                continue
            if inherited_meta is None:
                inherited_meta = meta
                inherited_from_primary = path == primary_file
            value = meta.get("created_at")
            if created_at is None and isinstance(value, str) and _parse_session_timestamp(value) is not None:
                created_at = value
            if inherited_meta is meta and created_at is not None:
                break
        return inherited_meta or {}, created_at, inherited_from_primary

    def _read_recovery_meta_unlocked(self, session_id: str) -> dict[str, Any]:
        """Return current crash-recovery sidecar metadata, best-effort."""
        envelope = self._read_json_file(self._recovery_file(session_id))
        return self._inheritable_meta(session_id, envelope) or {}

    def _effective_title_unlocked(
        self,
        session_id: str,
        existing_meta: dict[str, Any],
        recovery_meta: dict[str, Any],
        *,
        title_key: str,
        prefer_recovery: bool,
    ) -> str:
        """Resolve one title without making healthy saves parse the sidecar.

        Pending custom titles are explicit user actions made after the live
        metadata was read, so they always win until a primary write consumes
        them. Recovery titles outrank live titles only when the primary was
        rejected and the live metadata came from backup (or did not exist).
        """
        if title_key == "custom_title" and session_id in self._pending_custom_titles:
            return self._pending_custom_titles[session_id]

        existing_value = existing_meta.get(title_key)
        recovery_value = recovery_meta.get(title_key)
        candidates = (recovery_value, existing_value) if prefer_recovery else (existing_value, recovery_value)
        return next((value for value in candidates if isinstance(value, str)), "")

    def _carried_surface_unlocked(
        self,
        session_id: str,
        existing_meta: dict[str, Any],
        recovery_meta: dict[str, Any],
        *,
        inherited_from_primary: bool,
    ) -> str | None:
        """The recorded surface an unmarked save keeps; ``None`` when none was ever recorded.

        Only a turn names the surface, so every other save carries the stored
        value verbatim (one written by a newer version survives), as
        ``recorded_surface`` picks it; consulting the sidecar parses only one
        this store did not write itself.
        """
        if inherited_from_primary and not recovery_meta:
            recovery_meta = self._recovery_carry_meta_unlocked(session_id)
        return recorded_surface(existing_meta, recovery_meta, sidecar_first=not inherited_from_primary)

    def _recovery_carry_meta_unlocked(self, session_id: str) -> dict[str, Any]:
        """The sidecar's ``updated_at``/``last_surface``; empty when there is no sidecar."""
        signature = file_signature(self._recovery_file(session_id))
        if signature is None:
            return {}
        written = self._written_recovery_meta.get(_session_short_id(session_id))
        if written is not None and written[0] == signature:
            return written[1]
        return self._read_recovery_meta_unlocked(session_id)

    def _remember_written_recovery_meta_unlocked(self, session_id: str, meta: dict[str, Any]) -> None:
        signature = file_signature(self._recovery_file(session_id))
        if signature is None:
            self._written_recovery_meta.pop(_session_short_id(session_id), None)
            return
        carried = {key: meta[key] for key in ("updated_at", "last_surface") if key in meta}
        self._written_recovery_meta[_session_short_id(session_id)] = (signature, carried)

    @staticmethod
    def _resolve_created_at(
        live_created_at: str | None,
        recovery_meta: dict[str, Any],
        state: dict[str, Any],
        *,
        now: str,
    ) -> str:
        """Resolve the immutable session creation timestamp for a full save."""
        if live_created_at is not None:
            return live_created_at

        recovery_created_at = _parse_session_timestamp(recovery_meta.get("created_at"))
        history_created_at = _earliest_history_created_at(state)
        fallback_candidates = [
            timestamp for timestamp in (recovery_created_at, history_created_at) if timestamp is not None
        ]
        if fallback_candidates:
            return min(fallback_candidates).isoformat()
        return now

    def _build_session_envelope(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        agent_profile: str = "",
        agent_display_name: str = "",
        agent_profile_id: str = "",
        agent_profile_fingerprint: str = "",
        primary_cwd: str = "",
        working_dirs: list[str] | None = None,
        title: str = "",
        agent_profile_history: list[str] | None = None,
        model_provider: str = "",
        model_api_style: str | None = None,
        model_id: str = "",
        model_profile_id: str = "",
        model_base_url: str = "",
        model_profile_fingerprint: str | None = None,
        service_session_id: str | None = None,
        parent_session_id: str = "",
        last_surface: SessionSurface | None = None,
        kind: Literal["chat", "workflow"] = "chat",
        force_updated_at: bool = False,
    ) -> dict[str, Any]:
        """Build the persisted session envelope shared by primary and recovery saves."""
        now = datetime.now(UTC).isoformat()
        existing_meta, live_created_at, inherited_from_primary = self._read_inherited_live_meta_unlocked(session_id)

        if existing_meta and resolve_session_kind(existing_meta) != kind:
            raise ValueError("A session cannot change between chat and workflow.")

        # Healthy primary metadata already owns both title fields and the
        # immutable creation time, so the common checkpoint path must not
        # parse a potentially multi-megabyte recovery envelope. Consult it
        # only for fallback data. A pending custom title always wins until a primary save.
        needs_recovery_meta = (
            not inherited_from_primary
            or live_created_at is None
            or "custom_title" not in existing_meta
            or "generated_title" not in existing_meta
        )
        recovery_meta = self._read_recovery_meta_unlocked(session_id) if needs_recovery_meta else {}
        carry_custom_title = self._effective_title_unlocked(
            session_id,
            existing_meta,
            recovery_meta,
            title_key="custom_title",
            prefer_recovery=not inherited_from_primary,
        )
        carry_generated_title = self._effective_title_unlocked(
            session_id,
            existing_meta,
            recovery_meta,
            title_key="generated_title",
            prefer_recovery=not inherited_from_primary,
        )

        messages = state.get("messages", [])
        new_count = sum(1 for m in messages if _is_visible_message(m))
        old_count = existing_meta.get("message_count", 0)
        created_at = self._resolve_created_at(
            live_created_at,
            recovery_meta,
            state,
            now=now,
        )
        # Normal saves preserve updated_at when visible count is unchanged.
        # Recovery saves always advance it so recency can arbitrate sidecars.
        existing_updated_at = existing_meta.get("updated_at")
        parsed_existing_updated_at = _parse_session_timestamp(existing_updated_at)
        preserve_updated_at = (
            not force_updated_at
            and kind == "chat"
            and new_count == old_count
            and isinstance(existing_updated_at, str)
            and parsed_existing_updated_at is not None
        )
        updated_at = existing_updated_at if preserve_updated_at else now
        parsed_created_at = _parse_session_timestamp(created_at)
        parsed_updated_at = _parse_session_timestamp(updated_at)
        updated_at_floor = parsed_created_at
        if force_updated_at and parsed_existing_updated_at is not None:
            # Recovery arbitration requires a strict ``>`` and stale-sidecar
            # GC deletes on ``<=``. The 1µs advance is therefore intentional:
            # do not simplify this to max(now, parsed_existing_updated_at),
            # because equality would discard the new recovery checkpoint.
            recovery_floor = parsed_existing_updated_at + timedelta(microseconds=1)
            updated_at_floor = max(updated_at_floor, recovery_floor) if updated_at_floor is not None else recovery_floor
        if updated_at_floor is not None and parsed_updated_at is not None and parsed_updated_at < updated_at_floor:
            updated_at = updated_at_floor.isoformat()
        # Import lazily to avoid a circular import at module load:
        # ``chrys/__init__.py`` resolves the installed package version,
        # which would otherwise be pulled in at state-store import time.
        from chrys import __version__ as _app_version
        from chrys.foundation.platform import get_platform

        # Platform snapshot is derived here (not taken from the caller)
        # so every save stamps the *actual* runtime the file was written
        # on — the cached ``get_platform()`` is cheap and consistent
        # across saves within a process.
        plat = get_platform()

        meta = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "app_version": _app_version,
            "os_name": plat.os_name,
            "arch": plat.arch,
            "session_id": session_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "kind": kind,
            "primary_cwd": primary_cwd or existing_meta.get("primary_cwd", ""),
            "working_dirs": working_dirs if working_dirs is not None else existing_meta.get("working_dirs", []),
            "title": title or _extract_title(messages) or existing_meta.get("title", ""),
            "custom_title": carry_custom_title,
            "generated_title": carry_generated_title,
        }
        if kind == "chat":
            meta.update(
                {
                    "model_provider": model_provider or existing_meta.get("model_provider", ""),
                    "model_api_style": model_api_style
                    if model_api_style is not None
                    else existing_meta.get("model_api_style", ""),
                    "model_id": model_id or existing_meta.get("model_id", ""),
                    "model_profile_id": model_profile_id,
                    "model_base_url": model_base_url or existing_meta.get("model_base_url", ""),
                    "model_profile_fingerprint": model_profile_fingerprint
                    if model_profile_fingerprint is not None
                    else existing_meta.get("model_profile_fingerprint", ""),
                    "service_session_id": service_session_id
                    if service_session_id is not None
                    else existing_meta.get("service_session_id", ""),
                    "agent_profile": agent_profile or existing_meta.get("agent_profile", ""),
                    "agent_display_name": agent_display_name
                    or existing_meta.get("agent_display_name")
                    or existing_meta.get("display_name", ""),
                    "agent_profile_id": agent_profile_id,
                    "agent_profile_fingerprint": agent_profile_fingerprint
                    or existing_meta.get("agent_profile_fingerprint", ""),
                    "message_count": new_count,
                    "agent_profile_history": agent_profile_history
                    if agent_profile_history is not None
                    else existing_meta.get("agent_profile_history", existing_meta.get("profile_history", [])),
                    "parent_session_id": parent_session_id or existing_meta.get("parent_session_id", ""),
                }
            )
            surface = (
                last_surface.value
                if last_surface is not None
                else self._carried_surface_unlocked(
                    session_id, existing_meta, recovery_meta, inherited_from_primary=inherited_from_primary
                )
            )
            if surface is not None:
                meta["last_surface"] = surface
        return {
            "meta": meta,
            "state": state if kind == "workflow" else serialize_state(state),
            # A derived trajectory summary names the revision via this id and content hash.
            SESSION_CHECKPOINT_ID_KEY: new_analytics_id(),
        }

    def _delete_recovery_session_unlocked(self, session_id: str) -> None:
        """Best-effort deletion of the recovery sidecar."""
        self._written_recovery_meta.pop(_session_short_id(session_id), None)
        with contextlib.suppress(OSError):
            self._recovery_file(session_id).unlink()

    def _delete_recovery_session_if_not_newer(
        self,
        session_id: str,
        *,
        primary_updated_at: datetime | None,
    ) -> None:
        """Best-effort stale sidecar GC, rechecking under the write lock."""
        try:
            with FileLock(self._write_lock_path(session_id), timeout=0.0):
                current = self._read_json_file(self._recovery_file(session_id))
                current_updated_at = self._envelope_updated_at(current)
                if (
                    current is None
                    or current_updated_at is None
                    or (primary_updated_at is not None and current_updated_at <= primary_updated_at)
                ):
                    self._delete_recovery_session_unlocked(session_id)
        except TimeoutError:
            logger.debug("Timed out deleting stale recovery sidecar for %s", session_id, exc_info=True)
        except OSError:
            logger.debug("Failed to delete stale recovery sidecar for %s", session_id, exc_info=True)

    def _write_session_envelope_unlocked(self, session_id: str, data: dict[str, Any]) -> SessionCheckpoint:
        """Write primary + backup session envelopes atomically.

        The primary write is authoritative: failures propagate so callers know
        the save did not complete.  The backup is best-effort and can lag by one
        successful write, but is itself updated atomically when it succeeds.
        Returns the checkpoint identity of the bytes written.
        """
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        written = _atomic_write_text(self._session_file(session_id), payload)
        self._delete_recovery_session_unlocked(session_id)
        try:
            _atomic_write_text(self._backup_file(session_id), payload)
        except OSError:
            logger.warning("Failed to update session backup for %s", session_id, exc_info=True)
        return session_checkpoint_of(data, written)

    def _save_session_sync(
        self,
        session_id: str,
        state: dict[str, Any] | WorkflowSessionState,
        *,
        agent_profile: str = "",
        agent_display_name: str = "",
        agent_profile_id: str = "",
        agent_profile_fingerprint: str = "",
        primary_cwd: str = "",
        working_dirs: list[str] | None = None,
        title: str = "",
        agent_profile_history: list[str] | None = None,
        model_provider: str = "",
        model_api_style: str | None = None,
        model_id: str = "",
        model_profile_id: str = "",
        model_base_url: str = "",
        model_profile_fingerprint: str | None = None,
        service_session_id: str | None = None,
        parent_session_id: str = "",
        last_surface: SessionSurface | None = None,
        kind: Literal["chat", "workflow"] = "chat",
    ) -> SessionCheckpoint:
        """Sync implementation of session save (runs in a thread)."""
        if isinstance(state, WorkflowSessionState):
            state = state.encode()
        elif kind == "workflow":
            raise TypeError("Workflow saves require WorkflowSessionState.")
        path = self._session_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            self._migrate_if_needed_unlocked(session_id)
            data = self._build_session_envelope(
                session_id,
                state,
                agent_profile=agent_profile,
                agent_display_name=agent_display_name,
                agent_profile_id=agent_profile_id,
                agent_profile_fingerprint=agent_profile_fingerprint,
                primary_cwd=primary_cwd,
                working_dirs=working_dirs,
                title=title,
                agent_profile_history=agent_profile_history,
                model_provider=model_provider,
                model_api_style=model_api_style,
                model_id=model_id,
                model_profile_id=model_profile_id,
                model_base_url=model_base_url,
                model_profile_fingerprint=model_profile_fingerprint,
                service_session_id=service_session_id,
                parent_session_id=parent_session_id,
                last_surface=last_surface,
                kind=kind,
                force_updated_at=self._recovery_file(session_id).exists(),
            )

            # Index first, commit second: a save that kept ``updated_at``
            # (unchanged visible count) is a no-op for the index.
            self._record_mru(session_id, self._envelope_updated_at(data))
            checkpoint = self._write_session_envelope_unlocked(session_id, data)
            # The primary envelope now owns the title; a stale pending entry
            # must not resurrect it after a later clear.
            self._pending_custom_titles.pop(session_id, None)
            return checkpoint

    async def save_session(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        agent_profile: str = "",
        agent_display_name: str = "",
        agent_profile_id: str = "",
        agent_profile_fingerprint: str = "",
        primary_cwd: str = "",
        working_dirs: list[str] | None = None,
        title: str = "",
        agent_profile_history: list[str] | None = None,
        model_provider: str = "",
        model_api_style: str | None = None,
        model_id: str = "",
        model_profile_id: str = "",
        model_base_url: str = "",
        model_profile_fingerprint: str | None = None,
        service_session_id: str | None = None,
        parent_session_id: str = "",
        last_surface: SessionSurface | None = None,
    ) -> SessionCheckpoint:
        """Checkpoint Chat history and model metadata into the session folder.

        The session kind is fixed on its first save.
        """
        return await asyncio.to_thread(
            self._save_session_sync,
            session_id,
            state,
            agent_profile=agent_profile,
            agent_display_name=agent_display_name,
            agent_profile_id=agent_profile_id,
            agent_profile_fingerprint=agent_profile_fingerprint,
            primary_cwd=primary_cwd,
            working_dirs=working_dirs,
            title=title,
            agent_profile_history=agent_profile_history,
            model_provider=model_provider,
            model_api_style=model_api_style,
            model_id=model_id,
            model_profile_id=model_profile_id,
            model_base_url=model_base_url,
            model_profile_fingerprint=model_profile_fingerprint,
            service_session_id=service_session_id,
            parent_session_id=parent_session_id,
            last_surface=last_surface,
        )

    async def save_side_question(
        self,
        session_id: str,
        entry: SideQuestionEntry,
        seed: SideQuestionSessionSeed,
        *,
        cancelled: threading.Event | None = None,
    ) -> None:
        """Commit a complete Q&A without replacing any main-turn checkpoint."""
        await asyncio.to_thread(self._save_side_question_sync, session_id, entry, seed, cancelled=cancelled)

    def _save_side_question_sync(
        self,
        session_id: str,
        entry: SideQuestionEntry,
        seed: SideQuestionSessionSeed,
        *,
        cancelled: threading.Event | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("Side questions require a session identity")
        directory = self.session_dir(session_id)
        # Queue behind main checkpoints like other writers. Session operations never await this
        # worker, so waiting for the lock cannot block them; they can invalidate a queued save.
        with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            if cancelled is not None and cancelled.is_set():
                return
            entries = load_side_questions(directory, session_id)
            if not session_dir_has_artifacts(directory):
                # A side-question-only session still needs a restorable envelope. Never overwrite
                # a main turn's primary, recovery, or backup, even if it committed during this call.
                data = self._build_session_envelope(
                    session_id,
                    {},
                    agent_profile=seed.agent_profile,
                    agent_display_name=seed.agent_display_name,
                    agent_profile_id=seed.agent_profile_id,
                    primary_cwd=seed.primary_cwd,
                    working_dirs=list(seed.working_dirs),
                    model_provider=seed.model_provider,
                    model_api_style=seed.model_api_style,
                    model_id=seed.model_id,
                    model_profile_id=seed.model_profile_id,
                    last_surface=seed.last_surface,
                    title=title_from_text(entry.question),
                )
                self._record_mru(session_id, self._envelope_updated_at(data))
                self._write_session_envelope_unlocked(session_id, data)
            write_side_questions(directory, session_id, (*entries, entry))

    async def save_workflow_session(
        self, session_id: str, state: WorkflowSessionState, *, title: str = ""
    ) -> SessionCheckpoint:
        """Checkpoint a Workflow's typed state without Chat metadata or a second workspace input."""
        return await asyncio.to_thread(
            self._save_session_sync,
            session_id,
            state,
            primary_cwd=state.workspace.primary_cwd,
            working_dirs=[directory.path for directory in state.workspace.working_dirs],
            title=title,
            kind="workflow",
        )

    def save_recovery_session(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        agent_profile: str = "",
        agent_display_name: str = "",
        agent_profile_id: str = "",
        agent_profile_fingerprint: str = "",
        primary_cwd: str = "",
        working_dirs: list[str] | None = None,
        title: str = "",
        agent_profile_history: list[str] | None = None,
        model_provider: str = "",
        model_api_style: str | None = None,
        model_id: str = "",
        model_profile_id: str = "",
        model_base_url: str = "",
        model_profile_fingerprint: str | None = None,
        parent_session_id: str = "",
        last_surface: SessionSurface | None = None,
    ) -> None:
        """Synchronously write a crash-recovery sidecar for an in-flight turn."""
        recovery_file = self._recovery_file(session_id)
        recovery_file.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            self._migrate_if_needed_unlocked(session_id)
            data = self._build_session_envelope(
                session_id,
                state,
                agent_profile=agent_profile,
                agent_display_name=agent_display_name,
                agent_profile_id=agent_profile_id,
                agent_profile_fingerprint=agent_profile_fingerprint,
                primary_cwd=primary_cwd,
                working_dirs=working_dirs,
                title=title,
                agent_profile_history=agent_profile_history,
                model_provider=model_provider,
                model_api_style=model_api_style,
                model_id=model_id,
                model_profile_id=model_profile_id,
                model_base_url=model_base_url,
                model_profile_fingerprint=model_profile_fingerprint,
                service_session_id="",
                parent_session_id=parent_session_id,
                last_surface=last_surface,
                force_updated_at=True,
            )
            payload = json.dumps(data, indent=2, ensure_ascii=False)
            self._record_mru(session_id, self._envelope_updated_at(data))
            _common_atomic_write_text(recovery_file, payload)
            self._remember_written_recovery_meta_unlocked(session_id, data["meta"])

    @staticmethod
    def _envelope_updated_at(envelope: dict[str, Any] | None) -> datetime | None:
        """Return the envelope updated_at timestamp when it is parseable."""
        if envelope is None:
            return None
        meta = envelope.get("meta", {})
        if not isinstance(meta, dict):
            return None
        return _parse_session_timestamp(meta.get("updated_at"))

    def _resolve_effective_envelope_with_source(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> tuple[dict[str, Any] | None, Literal["recovery", "primary", "backup", "snapshot"]]:
        """Return the effective envelope and the file it came from, without healing recovery into primary."""
        self._migrate_if_needed(session_id)
        recovery_path = self._recovery_file(session_id)
        if prefer_recovery and recovery_path.exists():
            recovery = self._read_json_file(recovery_path)
            if recovery is not None:
                primary_path = self._session_file(session_id)
                primary = self._read_json_file(primary_path) if primary_path.exists() else None
                recovery_updated_at = self._envelope_updated_at(recovery)
                primary_updated_at = self._envelope_updated_at(primary)
                if primary is None:
                    return recovery, "recovery"
                if recovery_updated_at is not None and (
                    primary_updated_at is None or recovery_updated_at > primary_updated_at
                ):
                    return recovery, "recovery"
                self._delete_recovery_session_if_not_newer(session_id, primary_updated_at=primary_updated_at)
            else:
                self._delete_recovery_session_if_not_newer(session_id, primary_updated_at=None)
        return self._read_session_envelope_with_source(session_id)

    def _resolve_effective_envelope(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> dict[str, Any] | None:
        """Return the effective envelope, optionally preferring a fresh recovery sidecar."""
        envelope, _source = self._resolve_effective_envelope_with_source(
            session_id,
            prefer_recovery=prefer_recovery,
        )
        return envelope

    def _recovery_session_wins_sync(self, session_id: str) -> bool:
        """Return whether the crash-recovery sidecar is the winning effective source."""
        _envelope, source = self._resolve_effective_envelope_with_source(session_id, prefer_recovery=True)
        return source == "recovery"

    async def recovery_session_wins(self, session_id: str) -> bool:
        """Return whether the crash-recovery sidecar would win when recovery is allowed."""
        return await asyncio.to_thread(self._recovery_session_wins_sync, session_id)

    def _active_lock_is_held(self, session_id: str) -> bool:
        """Return True when another live owner, or this process, holds the active-session lock."""
        lock = FileLock(self.active_lock_path(session_id), timeout=0.0)
        try:
            lock.acquire()
        except TimeoutError:
            return True
        else:
            lock.release()
            return False

    def _load_recovery_session_sync(self, session_id: str) -> dict[str, Any] | None:
        """Sync implementation of direct recovery sidecar load."""
        try:
            raw = self._read_json_file(self._recovery_file(session_id))
            if raw is None:
                return None
            if resolve_session_kind(raw["meta"]) != "chat":
                raise ValueError("Chat loading requires a Chat session.")
            return deserialize_state(raw.get("state", {}))
        except KeyError:
            return None

    async def load_recovery_session(self, session_id: str) -> dict[str, Any] | None:
        """Load only the recovery sidecar state, if present."""
        return await asyncio.to_thread(self._load_recovery_session_sync, session_id)

    def _load_recovery_session_meta_sync(self, session_id: str) -> SessionMeta | None:
        """Sync implementation of direct recovery sidecar metadata load."""
        try:
            raw = self._read_json_file(self._recovery_file(session_id))
            if raw is None or "meta" not in raw:
                return None
            directory = self._session_dir(session_id)
            return self._session_meta_from_envelope(
                raw,
                size_bytes=_dir_size(directory),
            )
        except KeyError, OSError:
            return None

    async def load_recovery_session_meta(self, session_id: str) -> SessionMeta | None:
        """Load only the recovery sidecar metadata, if present."""
        return await asyncio.to_thread(self._load_recovery_session_meta_sync, session_id)

    def delete_recovery_session(self, session_id: str) -> None:
        """Delete the recovery sidecar, if present."""
        with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            self._delete_recovery_session_unlocked(session_id)

    def _load_workflow_session_sync(self, session_id: str) -> WorkflowSessionState | None:
        raw = self._resolve_effective_envelope(session_id, prefer_recovery=False)
        if raw is None:
            return None
        if resolve_session_kind(raw["meta"]) != "workflow":
            raise ValueError("Workflow loading requires a Workflow session.")
        return WorkflowSessionState.decode(raw.get("state"))

    async def load_workflow_session(self, session_id: str) -> WorkflowSessionState | None:
        """Validate the envelope once, then return typed workflow state directly."""
        return await asyncio.to_thread(self._load_workflow_session_sync, session_id)

    def _load_session_sync(self, session_id: str, *, prefer_recovery: bool = False) -> dict[str, Any] | None:
        """Sync implementation of session load (runs in a thread)."""
        try:
            raw = self._resolve_effective_envelope(session_id, prefer_recovery=prefer_recovery)
            if raw is None:
                return None
            if resolve_session_kind(raw["meta"]) != "chat":
                raise ValueError("Chat loading requires a Chat session.")
            return deserialize_state(raw.get("state", {}))
        except KeyError:
            return None

    async def load_session(self, session_id: str, *, prefer_recovery: bool = False) -> dict[str, Any] | None:
        """Load session state from the session folder."""
        return await asyncio.to_thread(self._load_session_sync, session_id, prefer_recovery=prefer_recovery)

    def _load_session_transcript_sync(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> SessionTranscript | None:
        """Read the session envelope once into replayable messages and turn identity."""
        try:
            raw = self._resolve_effective_envelope(session_id, prefer_recovery=prefer_recovery)
            if raw is None:
                return None
            state = raw.get("state", {})
            messages = state.get("messages", [])
            # Normalize: ensure each message has role + contents as dicts.
            # Skip every message compaction excluded, deliberately: tool
            # calls it summarized or removed, a dropped current turn's
            # in-between text, and turns folded into a compressed block.
            # Tool summaries stay in this raw list as the model's context;
            # transcript replays drop them too (``is_compaction_tool_summary``),
            # so the reopened session transcript shows neither a summary nor
            # the messages it replaced. Compressed blocks keep their own
            # archived copy and render it separately.
            result = []
            for msg in messages:
                ap = msg.get("additional_properties", {})
                if ap.get("_excluded", False):
                    continue
                contents = []
                for c in msg.get("contents", []):
                    if isinstance(c, str):
                        # Legacy plain-string format
                        if c.startswith("Content(type="):
                            contents.append({"type": "legacy", "raw": c})
                        else:
                            contents.append({"type": "text", "text": c})
                    elif isinstance(c, dict):
                        contents.append(c)
                entry: dict[str, Any] = {"role": msg.get("role", ""), "contents": contents}
                if msg.get("additional_properties"):
                    entry["additional_properties"] = msg["additional_properties"]
                result.append(entry)
            return SessionTranscript(messages=result, turns=turn_records(state))
        except KeyError:
            return None

    async def load_session_transcript(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> SessionTranscript | None:
        """Load a chat session's replayable history and turn registry together."""
        return await asyncio.to_thread(self._load_session_transcript_sync, session_id, prefer_recovery=prefer_recovery)

    async def load_session_raw(
        self,
        session_id: str,
        *,
        prefer_recovery: bool = False,
    ) -> list[dict[str, Any]] | None:
        """Load raw serialized messages for replay.

        Returns:
            A list of message dicts or ``None`` if the session does not
            exist.  Intermediate text (agent text returned alongside tool
            calls) is embedded per-message in
            ``additional_properties["_intermediate_text"]``.
        """
        transcript = await self.load_session_transcript(session_id, prefer_recovery=prefer_recovery)
        return None if transcript is None else transcript.messages

    def _load_session_meta_sync(
        self, session_id: str, *, prefer_recovery: bool = False, strict: bool = False
    ) -> SessionMeta | None:
        """Sync implementation of single-session meta load (runs in a thread)."""
        try:
            raw = self._resolve_effective_envelope(session_id, prefer_recovery=prefer_recovery)
            if raw is None:
                if strict and session_dir_has_artifacts(self._session_dir(session_id)):
                    raise ValueError(f"Session {session_id!r} has no readable checkpoint.")
                return None
            if "meta" not in raw:
                raise ValueError(f"Session {session_id!r} is missing metadata.")
            directory = self._session_dir(session_id)
            return self._session_meta_from_envelope(
                raw,
                size_bytes=_dir_size(directory),
            )
        except KeyError, OSError, ValueError, TypeError:
            if strict:
                raise
            return None

    async def load_session_meta(
        self, session_id: str, *, prefer_recovery: bool = False, strict: bool = False
    ) -> SessionMeta | None:
        """Load metadata for one specific session by id.

        Unlike :py:meth:`list_sessions`, this resolves the session file
        directly via :py:meth:`pathlib.Path.exists`, avoiding the Windows
        ``iterdir`` directory cache that can briefly hide a just-written
        session folder.

        Explicit restore can request strict validation: missing sessions return
        None, while unreadable checkpoints and invalid state retain their error.
        Lists and legacy Chat callers keep their tolerant behavior.
        """
        return await asyncio.to_thread(
            self._load_session_meta_sync, session_id, prefer_recovery=prefer_recovery, strict=strict
        )

    def _patch_recovery_titles_unlocked(
        self,
        session_id: str,
        *,
        custom_title: str | None,
        generated_title: str | None,
    ) -> None:
        """Mirror title patches into a live crash-recovery sidecar, best-effort."""
        recovery_file = self._recovery_file(session_id)
        if not recovery_file.exists():
            return
        try:
            envelope = self._read_json_file(recovery_file)
            if envelope is None:
                return
            meta = self._inheritable_meta(session_id, envelope)
            if meta is None:
                return
            if custom_title is not None:
                meta["custom_title"] = custom_title
            if generated_title is not None:
                meta["generated_title"] = generated_title
            payload = json.dumps(envelope, indent=2, ensure_ascii=False)
            _common_atomic_write_text(recovery_file, payload)
            self._remember_written_recovery_meta_unlocked(session_id, meta)
        except OSError:
            logger.debug("Failed to mirror titles into recovery sidecar for %s", session_id, exc_info=True)

    def _update_session_titles_sync(
        self,
        session_id: str,
        *,
        custom_title: str | None = None,
        generated_title: str | None = None,
    ) -> SessionMeta | None:
        """Sync implementation of the meta-only title patch (runs in a thread)."""
        session_file = self._session_file(session_id)
        with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            self._migrate_if_needed_unlocked(session_id)
            envelope = self._read_session_envelope(
                session_id,
                heal=False,
                migrate=False,
                include_snapshots=False,
            )
            meta = self._title_patch_meta(session_id, envelope)
            if envelope is None or meta is None:
                # Never materialize or rewrite a session file just for a title
                # patch when no semantically valid live envelope is available.
                # Auto-generated titles are simply dropped (the next turn
                # regenerates one); a custom title is held in memory until
                # a later full save can merge it — or discarded with the
                # session if it never saves. A recovery-only session
                # additionally gets the patch mirrored into its sidecar so
                # the edit survives the process (the envelope builder reads
                # it back on the next save).
                if custom_title is not None:
                    has_persisted_target = any(
                        path.exists()
                        for path in (
                            session_file,
                            self._backup_file(session_id),
                            self._recovery_file(session_id),
                        )
                    )
                    if custom_title or has_persisted_target:
                        self._pending_custom_titles[session_id] = custom_title
                    else:
                        self._pending_custom_titles.pop(session_id, None)
                    self._patch_recovery_titles_unlocked(
                        session_id,
                        custom_title=custom_title,
                        generated_title=None,
                    )
                return None
            session_file.parent.mkdir(parents=True, exist_ok=True)
            effective_custom = self._effective_title_unlocked(
                session_id,
                meta,
                {},
                title_key="custom_title",
                prefer_recovery=False,
            )
            effective_generated = self._effective_title_unlocked(
                session_id,
                meta,
                {},
                title_key="generated_title",
                prefer_recovery=False,
            )
            if custom_title is None and generated_title is not None and effective_custom:
                # A custom title permanently disables auto-generated updates.
                return None
            if custom_title is None and generated_title is None:
                return self._meta_from_patched_envelope(session_id, envelope)

            final_custom = effective_custom
            if custom_title is not None and custom_title != effective_custom:
                final_custom = custom_title
            final_generated = effective_generated
            if generated_title is not None and generated_title != effective_generated:
                final_generated = generated_title

            custom_needs_write = meta.get("custom_title") != final_custom
            generated_needs_write = meta.get("generated_title") != final_generated
            if not custom_needs_write and not generated_needs_write:
                # The durable envelope already holds the effective winners;
                # any pending custom candidate is therefore stale or synced.
                self._pending_custom_titles.pop(session_id, None)
                return self._meta_from_patched_envelope(session_id, envelope)
            if custom_needs_write:
                meta["custom_title"] = final_custom
            if generated_needs_write:
                meta["generated_title"] = final_generated
            payload = json.dumps(envelope, indent=2, ensure_ascii=False)
            _atomic_write_text(session_file, payload)
            try:
                _atomic_write_text(self._backup_file(session_id), payload)
            except OSError:
                logger.warning("Failed to update session backup for %s", session_id, exc_info=True)
            self._patch_recovery_titles_unlocked(
                session_id,
                custom_title=final_custom if custom_needs_write else None,
                generated_title=final_generated if generated_needs_write else None,
            )
            # The primary now durably contains the effective custom winner,
            # so no in-memory candidate may override a later full save.
            self._pending_custom_titles.pop(session_id, None)
        return self._meta_from_patched_envelope(session_id, envelope)

    def _meta_from_patched_envelope(self, session_id: str, envelope: dict[str, Any]) -> SessionMeta | None:
        """Best-effort SessionMeta for a title patch's return value.

        The patch itself has already succeeded (or been skipped as a no-op)
        by the time this runs, so malformed meta must degrade to ``None``
        rather than escape as a false failure.  ``TypeError`` covers
        ``datetime.fromisoformat`` on non-string timestamps.
        """
        try:
            directory = self._session_dir(session_id)
            return self._session_meta_from_envelope(
                envelope,
                size_bytes=_dir_size(directory),
            )
        except KeyError, ValueError, TypeError:
            return None

    async def update_session_titles(
        self,
        session_id: str,
        *,
        custom_title: str | None = None,
        generated_title: str | None = None,
    ) -> SessionMeta | None:
        """Patch the session's title overlay fields without touching state.

        ``None`` leaves a field unchanged; empty string clears it.  A
        ``generated_title``-only patch is refused (returns ``None``) when a
        custom title is already set.  This never creates a session file: for
        a session that has not been saved yet a custom title is held in
        memory (and ``None`` is returned) until the first full save merges
        it, while a generated title is dropped.  Returns the updated
        ``SessionMeta`` on success.
        """
        return await asyncio.to_thread(
            self._update_session_titles_sync,
            session_id,
            custom_title=custom_title,
            generated_title=generated_title,
        )

    def _session_dir_candidates(self) -> list[Path]:
        """Session folders that plausibly hold a restorable session."""
        from chrys.service.trajectory.tombstone import pending_delete_intents

        candidates = session_dir_candidates(self._dir)
        # A folder a delete could not finish removing (its trajectory file was
        # still open) is already gone as far as the user is concerned; listing
        # its remains would resurrect a deleted session.
        pending = pending_delete_intents(self._dir)
        if not pending:
            return candidates
        return [candidate for candidate in candidates if candidate.name not in pending]

    @staticmethod
    def _has_session_artifacts(session_dir: Path) -> bool:
        """Whether *session_dir* holds anything a restore could start from."""
        return session_dir_has_artifacts(session_dir)

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        """(mtime_ns, size) change-signature for *path*; ``(-1, -1)`` if absent."""
        try:
            stat = path.stat()
        except OSError:
            return (-1, -1)
        return (stat.st_mtime_ns, stat.st_size)

    def _meta_for_session_dir(self, session_dir: Path) -> SessionMeta | None:
        """One folder-format session's listing meta (``size_bytes`` unset), via the listing caches."""
        try:
            resolved = self._listing_meta(session_dir)
        except KeyError, ValueError, TypeError, OSError:
            return None
        return resolved[0] if resolved is not None else None

    def _listing_meta(self, session_dir: Path) -> tuple[SessionMeta, CatalogEntry | None] | None:
        """Resolve listing meta and, when it is cacheable, its catalog entry.

        Only a stable read of a valid primary with no recovery sidecar is
        cached: the sidecar's eligibility depends on the active lock, and a
        backup or snapshot fallback on files outside the signature. Those
        sessions are resolved live, probing the lock only when a sidecar
        exists.
        """
        short_id = session_dir.name
        primary = session_dir / SESSION_FILE_NAME
        recovery = session_dir / SESSION_RECOVERY_FILE_NAME
        signature = file_signature(primary)
        recovery_present = file_signature(recovery) is not None
        if signature is not None and not recovery_present:
            with self._meta_cache_lock:
                cached = self._meta_cache.get(short_id)
            if cached is not None and cached.signature == signature:
                return copy_session_meta(cached.meta), cached
        lock_held = recovery_present and self._active_lock_is_held(short_id)
        # A sidecar that appears after the probe is a live writer's checkpoint: never read it.
        prefer_recovery = recovery_present and not lock_held
        raw, source = self._resolve_effective_envelope_with_source(short_id, prefer_recovery=prefer_recovery)
        if raw is None:
            return None
        meta = self._session_meta_from_envelope(raw, size_bytes=0)
        stable = (
            source == "primary"
            and signature is not None
            and not recovery_present
            and file_signature(primary) == signature
            and file_signature(recovery) is None
        )
        if not stable or signature is None:
            return meta, None
        entry = CatalogEntry(signature, meta)
        with self._meta_cache_lock:
            self._meta_cache[short_id] = entry
            # A malformed envelope's meta would be dropped on every load: keep it in memory only.
            if is_recordable(entry):
                self._catalog_pending.add(short_id)
        return copy_session_meta(meta), entry

    def _with_workflow_status(self, meta: SessionMeta) -> SessionMeta:
        if meta.kind != "workflow" or not _is_run_name(meta.latest_run_id):
            return meta
        return replace(
            meta,
            latest_run=read_workflow_meta(
                run_dir(self._session_dir(meta.session_id), meta.latest_run_id),
                active=self._active_lock_is_held(meta.session_id),
            ),
        )

    def _workflow_listed_at(
        self, session_dir: Path, meta: WorkflowSessionMeta, entry: CatalogEntry | None
    ) -> datetime | None:
        """The latest run's listing time (``None``: no displayable run), cached on its run files.

        Matches ``_with_workflow_status``: the time does not depend on the
        live status, so it can be kept without probing the active lock.
        """
        run_id = meta.latest_run_id
        if not _is_run_name(run_id):
            return None
        directory = run_dir(session_dir, run_id)

        def run_key() -> RunListingKey:
            return RunListingKey(
                run_id, file_signature(directory / HEADER_FILE), file_signature(directory / EVENTS_FILE)
            )

        key = run_key()
        if entry is not None and entry.run is not None and entry.run.key == key:
            return entry.run.listed_at
        read = read_workflow_run(directory, active=False)
        listed_at = read.meta.updated_at if read.meta is not None else None
        # A read an I/O error shaped would stick until the run files change.
        if read.settled and entry is not None and run_key() == key:
            listed = replace(entry, run=RunListing(key, listed_at))
            recordable = is_recordable(listed)
            with self._meta_cache_lock:
                if self._meta_cache.get(session_dir.name) is entry:
                    self._meta_cache[session_dir.name] = listed
                    # As in ``_listing_meta``: an entry no load would read back stays in memory.
                    if recordable:
                        self._catalog_pending.add(session_dir.name)
        return listed_at

    # ------------------------------------------------------------------ #
    # Persisted listing catalog
    # ------------------------------------------------------------------ #

    def _refresh_catalog(self) -> None:
        """Adopt the persisted catalog's entries when its file changed since the last look."""
        signature = file_signature(self._catalog.path)
        with self._meta_cache_lock:
            if signature == self._catalog_signature:
                return
        loaded = self._catalog.load() if signature is not None else {}
        if loaded is None:
            return  # Unreadable right now: look again next time.
        with self._meta_cache_lock:
            self._catalog_signature = signature
            for short_id, entry in loaded.items():
                current = self._meta_cache.get(short_id)
                # A differing in-memory entry not yet persisted was derived
                # here after the peer's write, or will simply miss and re-parse.
                if current is None or (current.signature != entry.signature and short_id not in self._catalog_pending):
                    self._meta_cache[short_id] = entry

    def _commit_catalog(self) -> None:
        """Settle owed removals, then merge newly derived entries into the persisted catalog; best-effort.

        Removals never wait for the commit interval. A skipped or failed
        merge keeps the entries pending for a later listing.
        """
        self._settle_catalog_removals()
        now = time.monotonic()
        with self._meta_cache_lock:
            self._catalog_pending.intersection_update(self._meta_cache.keys())
            updates = {short_id: self._meta_cache[short_id] for short_id in self._catalog_pending}
            if not updates:
                return
            last = self._catalog_committed_at
            if (
                len(updates) < SESSION_CATALOG_EAGER_COMMIT_ENTRIES
                and last is not None
                and now - last < SESSION_CATALOG_COMMIT_INTERVAL_SECONDS
            ):
                return
            self._catalog_committed_at = now
        outcome = self._catalog.commit(
            updates, still_valid=self._catalog_entry_still_valid, is_live=self._catalog_liveness()
        )
        if not outcome.settled:
            return
        with self._meta_cache_lock:
            for short_id, entry in updates.items():
                if self._meta_cache.get(short_id) is entry:
                    self._catalog_pending.discard(short_id)
            if outcome.signature is not None:
                # Our own write needs no reload; peers' entries it merged are
                # only a missed cache hit away.
                self._catalog_signature = outcome.signature

    def _catalog_entry_still_valid(self, short_id: str, entry: CatalogEntry) -> bool:
        session_dir = self._dir / short_id
        return (
            file_signature(session_dir / SESSION_FILE_NAME) == entry.signature
            and file_signature(session_dir / SESSION_RECOVERY_FILE_NAME) is None
        )

    def _catalog_liveness(self) -> Callable[[str], bool]:
        """Whether the session an entry describes still exists.

        It needs its primary file (a reset keeps the folder) and no recorded
        delete intent (a logical delete may leave the primary pinned). The
        intents are read on first use: a delete records its intent before it
        removes its entry under the catalog lock, so a check made under that
        lock sees every delete whose removal went first.
        """
        from chrys.service.trajectory.tombstone import pending_delete_intents

        deleted: frozenset[str] | None = None

        def is_live(short_id: str) -> bool:
            nonlocal deleted
            if not short_id or short_id.startswith(".") or "/" in short_id or "\\" in short_id:
                return False
            if deleted is None:
                deleted = pending_delete_intents(self._dir)
            return short_id not in deleted and file_signature(self._dir / short_id / SESSION_FILE_NAME) is not None

        return is_live

    def _forget_catalog_entry(self, session_id: str) -> None:
        """Drop a deleted session's listing entry from memory and from the persisted catalog.

        A removal the catalog lock or a write failure stops stays owed and is
        retried before each later commit, until it lands (as is one for a
        session a scan found gone).
        """
        short_id = _session_short_id(session_id)
        with self._meta_cache_lock:
            self._meta_cache.pop(short_id, None)
            self._catalog_pending.discard(short_id)
            self._catalog_removals_owed.add(short_id)
        self._settle_catalog_removals()

    def _settle_catalog_removals(self) -> None:
        with self._meta_cache_lock:
            owed = frozenset(self._catalog_removals_owed)
        if not owed or not self._catalog.remove(owed):
            return
        with self._meta_cache_lock:
            self._catalog_removals_owed.difference_update(owed)

    def _legacy_session_metas(self, seen_session_ids: set[str]) -> list[SessionMeta]:
        """Metas for legacy flat-file sessions not yet migrated to folders."""
        return [meta for _path, meta in self._legacy_session_metas_with_paths(seen_session_ids)]

    def _legacy_session_metas_with_paths(self, seen_session_ids: set[str]) -> list[tuple[Path, SessionMeta]]:
        """Legacy flat-file sessions not yet migrated to folders, with their files.

        Duplicate ``meta.session_id`` values — across flat files, or against
        the folder-format ids in *seen_session_ids* — keep only the first
        occurrence, matching the pre-cache listing behavior.
        """
        sessions: list[tuple[Path, SessionMeta]] = []
        seen = set(seen_session_ids)
        for path in self._legacy_session_files():
            meta = self._legacy_meta_for_file(path)
            if meta is None or meta.session_id in seen:
                continue
            seen.add(meta.session_id)
            sessions.append((path, meta))
        return sessions

    def _legacy_meta_for_file(self, path: Path) -> SessionMeta | None:
        """Load one legacy flat-file session's meta, via its in-process cache."""
        try:
            signature = file_signature(path)
            with self._meta_cache_lock:
                cached = self._legacy_meta_cache.get(path.name)
            if signature is not None and cached is not None and cached[0] == signature:
                return copy_session_meta(cached[1])
            raw = json.loads(path.read_text(encoding="utf-8"))
            meta = self._session_meta_from_envelope(raw, size_bytes=path.stat().st_size)
            if signature is not None and file_signature(path) == signature:
                with self._meta_cache_lock:
                    self._legacy_meta_cache[path.name] = (signature, meta)
                return copy_session_meta(meta)
            return meta
        except json.JSONDecodeError, KeyError, ValueError, TypeError, OSError:
            return None

    def _evict_stale_meta_cache_sync(self) -> None:
        """Drop cache entries whose session file is gone (deleted, or reset in place).

        An evicted folder entry is owed its catalog removal: once out of
        memory, no later scan would find it again.
        """
        with self._meta_cache_lock:
            cached = list(self._meta_cache)
        is_live = self._catalog_liveness()
        gone = [key for key in cached if not is_live(key)]
        try:
            live_legacy: set[str] | None = {p.name for p in self._legacy_session_files()}
        except OSError:
            live_legacy = None
        with self._meta_cache_lock:
            dead = [key for key in gone if self._meta_cache.pop(key, None) is not None]
            self._catalog_pending.difference_update(dead)
            self._catalog_removals_owed.update(dead)
            if live_legacy is not None:
                for key in [key for key in self._legacy_meta_cache if key not in live_legacy]:
                    del self._legacy_meta_cache[key]

    def _settle_listing_caches(self) -> None:
        """After a scan: evict gone sessions, then remove them from, and merge new entries into, the catalog."""
        self._evict_stale_meta_cache_sync()
        self._commit_catalog()

    def _folder_metas(self) -> list[tuple[Path, SessionMeta]]:
        """Every session folder's listing meta, without size or run status."""
        self._refresh_catalog()
        return [
            (session_dir, meta)
            for session_dir in self._session_dir_candidates()
            if (meta := self._meta_for_session_dir(session_dir)) is not None
        ]

    @property
    def sessions_root(self) -> Path:
        """The directory holding the session folders."""
        return self._dir

    def session_folder_cwds(self) -> dict[str, str]:
        """Each listed session folder's name and primary working folder; blocking, so it runs off the event loop."""
        cwds = {session_dir.name: meta.primary_cwd for session_dir, meta in self._folder_metas()}
        self._settle_listing_caches()
        return cwds

    def _scan_session_metas_sync(self) -> list[SessionMeta]:
        """Every listed session's meta without folder size or run status (for ranking by time)."""
        folders = self._folder_metas()
        sessions = [meta for _session_dir, meta in folders]
        sessions.extend(self._legacy_session_metas({meta.session_id for meta in sessions}))
        self._settle_listing_caches()
        return sessions

    def _list_sessions_sync(self, *, kind: Literal["chat", "workflow"] | None = None) -> list[SessionMeta]:
        """Sync implementation of session listing (runs in a thread)."""
        folders = self._folder_metas()
        sessions = [
            replace(meta, size_bytes=_dir_size(session_dir))
            for session_dir, meta in folders
            if kind is None or meta.kind == kind
        ]
        legacy = self._legacy_session_metas({meta.session_id for _session_dir, meta in folders})
        sessions.extend(meta for meta in legacy if kind is None or meta.kind == kind)
        self._settle_listing_caches()
        return [self._with_workflow_status(meta) for meta in sessions]

    async def list_sessions(self, *, kind: Literal["chat", "workflow"] | None = None) -> list[SessionMeta]:
        """List all saved sessions with metadata."""
        return await asyncio.to_thread(self._list_sessions_sync, kind=kind)

    # ------------------------------------------------------------------ #
    # Latest-session lookup via the MRU index
    # ------------------------------------------------------------------ #

    def _rescan_latest_session_id(self) -> str | None:
        """Full listing scan; rebuilds the MRU index and returns the newest id.

        The rebuild keeps index entries newer than the scan (a save whose
        pre-commit record beat our read of its envelope, or a stale entry the
        scan already contradicts).  The merged ranking — with the scan's top
        as the floor — goes through the same verification as an indexed
        lookup: a matching or newer envelope means the save has landed and
        wins; an unavailable (or, racing a delete, vanished) or older one is
        skipped/re-ranked and the next candidate stands.  The modified-folder
        sweep then covers whatever committed during the scan without leaving
        a rankable record behind, including a first session that landed
        after its in-flight record failed verification.
        """
        scan_started = datetime.now(UTC)
        sessions = self._scan_session_metas_sync()
        entries = sort_entries(SessionMruEntry(m.session_id, coerce_utc(m.updated_at)) for m in sessions)
        scanned = entries[0] if entries else None
        merged = self._note_mru(lambda: self._mru.rebuild(entries))
        candidates = list(merged) if merged else entries
        if scanned is not None and all(entry.session_id != scanned.session_id for entry in candidates):
            candidates.append(scanned)  # ranked below every record: keep it as the floor
        winner = self._verify_ranked(sort_entries(candidates))
        since = scan_started if winner is None else min(winner.last_updated_at, scan_started)
        best = self._newest_modified_after(winner, since=since)
        return best.session_id if best is not None else None

    def _verify_ranked(self, entries: list[SessionMruEntry]) -> SessionMruEntry | None:
        """Verify *entries* (sorted newest first) against disk; return the winner.

        The top entry is checked against its envelope: an unavailable session
        is dropped, a differing on-disk stamp re-ranks it in memory (only a
        newer stamp is written back — an older one is either transient (a
        live owner keeping the primary ahead of a sidecar, a save between its
        record and its commit) or a rollback that the session's next save
        overwrites, and persisting it would break the crash-recovery case
        where the sidecar must win) and the loop repeats until the top has
        been verified.  ``None`` when every entry is unavailable.
        """
        verified: set[str] = set()
        while entries:
            top = entries[0]
            if top.session_id in verified:
                return top
            actual = self._mru_verify(top.session_id)
            if actual is None:
                entries.pop(0)
                continue
            verified.add(top.session_id)
            if actual != top.last_updated_at:
                entries[0] = SessionMruEntry(top.session_id, actual)
                self._record_mru(top.session_id, actual)
                entries = sort_entries(entries)
        return None

    def _mru_verify(self, session_id: str) -> datetime | None:
        """Return the on-disk ``updated_at`` for an indexed session, or ``None``.

        Uses ``_meta_for_session_dir`` so active-lock, recovery-sidecar and
        backup/snapshot fallback rules match ``list_sessions()`` exactly.  A
        session with nothing restorable on disk — no folder, or a folder a
        pre-commit crash left without envelope/backup/sidecar/snapshots — is
        dropped from the index (see ``_prune_ghost_entry``); one that has
        artifacts but cannot be read right now is only skipped in memory.
        """
        session_dir = self._session_dir(session_id)
        if not self._has_session_artifacts(session_dir) and not self._legacy_path(session_id).exists():
            legacy = self._legacy_meta_for_id(session_id)
            if legacy is not None:  # a legacy flat file under a non-canonical name
                return coerce_utc(legacy.updated_at)
            self._prune_ghost_entry(session_id)
            return None
        self._migrate_if_needed(session_id)
        meta = self._meta_for_session_dir(session_dir)
        if meta is None:  # unreadable folder: the listing may still attribute a legacy file to the id
            meta = self._legacy_meta_for_id(session_id)
        return None if meta is None else coerce_utc(meta.updated_at)

    def _legacy_meta_for_id(self, session_id: str) -> SessionMeta | None:
        """The legacy flat file the listing would attribute to *session_id*, if any.

        Legacy files are discovered by ``*.json``, not by name, so a session
        may live under a file that is not ``<id>.json``; the listing takes
        the first sorted readable file embedding the id, and so does this.
        """
        for path in self._legacy_session_files():
            meta = self._legacy_meta_for_file(path)
            if meta is not None and meta.session_id == session_id:
                return meta
        return None

    def _prune_ghost_entry(self, session_id: str) -> None:
        """Drop an indexed session with nothing restorable — decided under its write lock.

        Saves and forks record their id before the commit that makes the
        envelope (or the directory) appear, so "nothing on disk" alone is
        ambiguous: a busy write lock means a writer is committing it right
        now, and the absence must be re-checked while holding the lock so a
        commit that lands between the first look and this one is not
        mistaken for a ghost.  What survives is a real ghost: a folder a
        pre-commit crash left empty, or a session deleted behind the index.
        """
        try:
            lock = FileLock(self._write_lock_path(session_id), timeout=0.0)
            lock.acquire()
        except TimeoutError:
            return
        except OSError:  # unwritable/odd lock leaf: pruning is optional, the lookup is not
            logger.debug("Skipping ghost prune of %s: write lock unavailable", session_id, exc_info=True)
            return
        try:
            if (
                not self._has_session_artifacts(self._session_dir(session_id))
                and not self._legacy_path(session_id).exists()
                and self._legacy_meta_for_id(session_id) is None
            ):
                self._note_mru(lambda: self._mru.remove(session_id))
        finally:
            lock.release()

    def _newest_modified_after(self, winner: SessionMruEntry | None, *, since: datetime) -> SessionMruEntry | None:
        """Safety net for writers that bypass the index: re-rank *winner* against
        every session whose folder (or legacy file) was modified after *since*
        (the winner's stamp, or the start of a scan that found no winner).

        A session that outranks the winner was necessarily written after the
        winner's ``updated_at``, and writing it touches its folder's mtime —
        so an older chrys, a copied folder, a save whose record failed, or a
        recovery-only session that was hidden behind its owner's active lock
        during the backfill scan all surface here at the cost of one stat per
        folder plus a parse for the (normally zero) recently touched ones.
        Folders are ranked one by one; a recently touched legacy flat file
        defers to the full listing so duplicate-id precedence matches it.
        """
        try:
            cutoff_ns = int((since - _MRU_SWEEP_SLACK).timestamp() * 1_000_000_000)
        except OverflowError, OSError, ValueError:
            cutoff_ns = -1  # absurd stamp (datetime.min/max): parse everything rather than guess
        winner_dir = self._session_dir(winner.session_id) if winner is not None else None
        best = winner
        try:
            candidates = self._session_dir_candidates()
        except OSError:
            return best
        for session_dir in candidates:
            if session_dir == winner_dir or self._file_signature(session_dir)[0] <= cutoff_ns:
                continue
            meta = self._meta_for_session_dir(session_dir)
            if meta is not None:
                best = _newer_of(best, SessionMruEntry(meta.session_id, coerce_utc(meta.updated_at)))
        if any(self._file_signature(legacy)[0] > cutoff_ns for legacy in self._legacy_session_files()):
            # A freshly touched legacy flat file: which copy of a session id
            # counts (folder over legacy, first sorted legacy otherwise) is the
            # listing's rule, so let the listing rank it — legacy roots are
            # transitional and the meta cache makes the repeat cheap.
            for meta in self._scan_session_metas_sync():
                best = _newer_of(best, SessionMruEntry(meta.session_id, coerce_utc(meta.updated_at)))
        if best is not None and best != winner:
            self._record_mru(best.session_id, best.last_updated_at)
        return best

    def _load_latest_session_id_sync(self) -> str | None:
        """Sync implementation of the MRU-backed latest-session lookup."""
        snapshot = self._mru.load()
        if snapshot is None or not snapshot.complete:
            return self._rescan_latest_session_id()
        winner = self._verify_ranked(list(snapshot.sessions))
        if winner is None:
            # Every indexed entry is gone (or the index is empty): older
            # sessions may still exist below the index horizon, so scan once
            # and rebuild.
            return self._rescan_latest_session_id()
        if snapshot.horizon is not None and winner.last_updated_at < snapshot.horizon:
            # A downgrade dropped the winner below sessions that were trimmed
            # out of the index; only a full scan can rank those.
            return self._rescan_latest_session_id()
        best = self._newest_modified_after(winner, since=winner.last_updated_at)
        return best.session_id if best is not None else None

    async def load_latest_session_id(self, *, chat_only: bool = False) -> str | None:
        """Return the id of the most recently updated restorable session.

        Backed by the ``session_mru.json`` index; a valid index costs one
        small read, verification of the newest session's envelope and a
        stat sweep of the session folders (parsing only those modified after
        the winner), while a missing/corrupt/incomplete index falls back to
        a full listing scan that also rebuilds the index.
        """
        latest_id = await asyncio.to_thread(self._load_latest_session_id_sync)
        if latest_id is None or not chat_only:
            return latest_id
        latest = await asyncio.to_thread(self._meta_for_session_dir, self._session_dir(latest_id))
        if latest is not None and latest.kind == "chat":
            return latest_id
        # Keep the MRU fast path for ordinary chat use; only a workflow-only
        # winner requires finding the newest eligible chat among older sessions.
        latest_chat = max(
            (meta for meta in await self.list_sessions(kind="chat")),
            key=lambda meta: (coerce_utc(meta.updated_at), meta.session_id),
            default=None,
        )
        return latest_chat.session_id if latest_chat is not None else None

    # ------------------------------------------------------------------ #
    # Paged listing
    # ------------------------------------------------------------------ #

    def _listed_at(self, source: Path, meta: SessionMeta, entry: CatalogEntry | None) -> datetime | None:
        """A session's listing time; ``None`` for a workflow session with no displayable run."""
        if isinstance(meta, WorkflowSessionMeta):
            return self._workflow_listed_at(source, meta, entry)
        return meta.updated_at

    def _open_session_listing_sync(self, kind: Literal["chat", "workflow"]) -> SessionListing:
        self._refresh_catalog()
        entries: list[SessionListingEntry] = []
        seen: set[str] = set()
        for session_dir in self._session_dir_candidates():
            try:
                resolved = self._listing_meta(session_dir)
            except KeyError, ValueError, TypeError, OSError:
                continue
            if resolved is None:
                continue
            meta, entry = resolved
            if meta.session_id in seen:
                continue  # a copied folder: the first one lists, as when streaming
            seen.add(meta.session_id)
            if meta.kind == kind and (listed_at := self._listed_at(session_dir, meta, entry)) is not None:
                entries.append(
                    SessionListingEntry(
                        meta.session_id, coerce_utc(listed_at), meta.last_surface or SessionSurface.TUI, session_dir
                    )
                )
        for path, meta in self._legacy_session_metas_with_paths(seen):
            if meta.kind != kind:
                continue
            # A legacy file is migrated to its folder on first load; until then
            # any workflow run lives under the folder its id names.
            listed_at = self._listed_at(self._session_dir(meta.session_id), meta, None)
            if listed_at is not None:
                entries.append(
                    SessionListingEntry(
                        meta.session_id,
                        coerce_utc(listed_at),
                        meta.last_surface or SessionSurface.TUI,
                        path,
                        legacy=True,
                    )
                )
        self._settle_listing_caches()
        entries.sort(key=lambda item: (item.listed_at, item.session_id), reverse=True)
        return SessionListing(kind, tuple(entries))

    async def open_session_listing(self, *, kind: Literal["chat", "workflow"]) -> SessionListing:
        """Snapshot the displayable sessions of *kind*, newest first, for paging.

        Costs a ``stat`` per session folder plus a parse for each session
        changed since it was last listed (by any process, through the
        persisted catalog); folder sizes and run status are left to
        :meth:`load_session_page`.
        """
        return await asyncio.to_thread(self._open_session_listing_sync, kind)

    def _load_session_page_sync(
        self, listing: SessionListing, surfaces: Collection[SessionSurface], page: int, page_size: int
    ) -> SessionPage:
        from chrys.service.trajectory.tombstone import pending_delete_intents

        selected, current, page_count, total = page_slice(listing, surfaces, page, page_size=page_size)
        deleted = pending_delete_intents(self._dir) if selected else frozenset()
        metas: list[SessionMeta] = []
        for item in selected:
            meta = self._legacy_meta_for_file(item.source) if item.legacy else None
            # Loading a legacy session since the snapshot moved it into its folder.
            folder = self._session_dir(item.session_id) if item.legacy else item.source
            # A logical delete may leave the primary pinned.
            if meta is None and folder.name not in deleted:
                meta = self._meta_for_session_dir(folder)
                if meta is not None:
                    meta = replace(meta, size_bytes=_dir_size(folder))
            # A session deleted, or replaced by another, since the snapshot drops out of its page.
            if meta is not None and meta.session_id == item.session_id and meta.kind == listing.kind:
                metas.append(self._with_workflow_status(meta))
        self._commit_catalog()
        return SessionPage(tuple(metas), current, page_count, total)

    async def load_session_page(
        self,
        listing: SessionListing,
        *,
        surfaces: Collection[SessionSurface],
        page: int = 1,
        page_size: int = SESSION_PAGE_SIZE,
    ) -> SessionPage:
        """Load one page of *listing* filtered to *surfaces*: fresh metas with sizes and run status.

        Rows keep the snapshot's order and page; their contents (title,
        surface, run status) are read now, so a session changed since the
        snapshot shows its current state where the snapshot placed it.
        """
        return await asyncio.to_thread(self._load_session_page_sync, listing, surfaces, page, page_size)

    def _delete_session_sync(self, session_id: str, *, allow_active: bool = False) -> None:
        """Sync implementation of session delete (runs in a thread)."""
        session_dir = self._session_dir(session_id)
        legacy = self._legacy_path(session_id)
        if not session_dir.exists() and not legacy.exists():
            return

        active_lock: FileLock | None = None
        if not allow_active:
            active_lock = FileLock(self.active_lock_path(session_id), timeout=SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS)
            active_lock.acquire()
        try:
            with FileLock(self._write_lock_path(session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
                if session_dir.is_dir():
                    # A live trajectory writer (a stuck worker here, a writer
                    # in another process) turns this into a logical delete:
                    # the directory moves to a tombstone now and is removed
                    # once the writer's lease is free.
                    from chrys.service.trajectory.tombstone import DeleteOutcome, delete_session_directory

                    delete_result = delete_session_directory(session_dir, sessions_root=self._dir)
                    if delete_result.outcome == DeleteOutcome.INTENT_FAILED and session_dir.exists():
                        # The rename failed, the durable delete intent failed,
                        # and best-effort removal left something behind. No
                        # sweeper owns that directory, so reporting success
                        # would let it reappear after its MRU entry was removed.
                        raise OSError(f"Session directory could not be deleted or scheduled: {session_dir}")
                # Also clean up legacy flat file if it exists
                if legacy.exists():
                    legacy.unlink()
                with contextlib.suppress(OSError):
                    self.active_owner_path(session_id).unlink()
                # Still under the write lock: a same-id save recreating the
                # session records before it can commit, so it cannot slip in
                # between the deletion and this removal and lose its entry.
                self._note_mru(lambda: self._mru.remove(session_id))
                # The catalog holds prompt excerpts: they go with the session.
                self._forget_catalog_entry(session_id)
                self._pending_custom_titles.pop(session_id, None)
                self._written_recovery_meta.pop(_session_short_id(session_id), None)
        finally:
            if active_lock is not None:
                active_lock.release()

    async def delete_session(self, session_id: str, *, allow_active: bool = False) -> None:
        """Delete a saved session (folder or legacy file)."""
        await asyncio.to_thread(self._delete_session_sync, session_id, allow_active=allow_active)
