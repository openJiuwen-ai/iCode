# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session fork operations for the JSON file state store."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.platform.files import (
    atomic_write_owner_only_bytes,
    atomic_write_owner_only_text,
    fsync_directory,
    read_owner_verified_bounded,
)
from chrys.foundation.text.mentions import format_file_mention, iter_mention_tokens
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.mutations.snapshot_files import rollback_snapshot_paths
from chrys.service.session.side_questions import BTW_FILE_NAME, load_side_questions, write_side_questions
from chrys.service.session.sub_agent_logs import (
    MAX_AUDIT_SCAN_TOTAL_BYTES,
    MAX_SCANNED_AUDIT_FILES,
    MAX_SUB_AGENT_AUDIT_BYTES,
    collect_sub_agent_log_file_references,
    is_safe_sub_agent_log_basename,
    scan_bounded_json_files,
    validate_acp_state_file_references,
)
from chrys.service.state._session_files import (
    RAW_HTTP_LOG_FILE_NAME,
    SESSION_BACKUP_FILE_NAME,
    SESSION_CHECKPOINT_ID_KEY,
    SESSION_FILE_NAME,
    SESSION_FORK_MAX_ID_ATTEMPTS,
    SESSION_RECOVERY_FILE_NAME,
    SESSION_WRITE_LOCK_TIMEOUT_SECONDS,
    FileLock,
    SessionForkError,
    SessionNotFoundError,
    _atomic_write_text,
    _is_string_keyed_dict,
    _session_short_id,
    atomic_copy_file,
    make_junction_dropping_ignore,
    session_active_lock_path,
    session_active_owner_path,
    session_write_lock_path,
)
from chrys.service.state._session_meta import resolve_session_kind
from chrys.service.tools.background import BACKGROUND_DIR_NAME
from chrys.service.tools.session_artifacts import reharden_document_image_artifacts
from chrys.service.trajectory.state import fork_turn_registry

logger = logging.getLogger(__name__)


class SessionForkMixin:
    """Session-fork operations for JSON state stores."""

    if TYPE_CHECKING:
        _dir: Path

        def _session_dir(self, session_id: str) -> Path: ...
        def _session_file(self, session_id: str) -> Path: ...
        def _write_lock_path(self, session_id: str) -> Path: ...
        def _legacy_path(self, session_id: str) -> Path: ...

        @staticmethod
        def _read_json_file(path: Path) -> dict[str, Any] | None: ...

        def _record_mru(self, session_id: str, updated_at: datetime | None) -> None: ...
        def _migrate_if_needed_unlocked(self, session_id: str) -> None: ...

    def fork_session(self, parent_session_id: str, *, last_surface: SessionSurface | None = None) -> str:
        """Create an independent copy of *parent_session_id* with a new canonical id.

        *last_surface* names the surface that forked it; ``None`` keeps the parent's.
        """
        try:
            return self._fork_session_sync(parent_session_id, last_surface=last_surface)
        except SessionNotFoundError:
            raise
        except SessionForkError:
            raise
        except Exception as exc:
            raise SessionForkError(f"Failed to fork session {parent_session_id}") from exc

    def _fork_session_sync(self, parent_session_id: str, *, last_surface: SessionSurface | None = None) -> str:
        """Sync implementation of session fork."""
        with FileLock(self._write_lock_path(parent_session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            self._migrate_if_needed_unlocked(parent_session_id)
            parent_dir = self._session_dir(parent_session_id)
            if not parent_dir.is_dir() or not self._session_file(parent_session_id).exists():
                raise SessionNotFoundError(f"Session '{parent_session_id}' not found")

            parent = self._read_json_file(self._session_file(parent_session_id))
            if parent is not None and resolve_session_kind(parent.get("meta", {})) != "chat":
                raise SessionForkError("Only Chat sessions can be forked. Start a new Workflow session instead.")

            last_collision: SessionForkError | None = None
            for _attempt in range(SESSION_FORK_MAX_ID_ATTEMPTS):
                new_session_id = self._new_fork_session_id()
                dest_dir = self._session_dir(new_session_id)
                with FileLock(self._write_lock_path(new_session_id), timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
                    try:
                        self._assert_fork_destination_available(new_session_id, include_write_lock=False)
                    except SessionForkError as exc:
                        last_collision = exc
                        continue
                    tmp_dir = self._new_fork_temp_dir(new_session_id)
                    try:
                        shutil.copytree(
                            parent_dir, tmp_dir, symlinks=True, ignore=self._make_fork_copy_ignore(parent_dir)
                        )
                        reharden_document_image_artifacts(parent_dir, tmp_dir)
                        self._sanitize_fork_sub_agent_layout(tmp_dir)
                        copied_sub_agent_artifacts = self._secure_copy_sub_agent_artifacts(parent_dir, tmp_dir)
                        fork_updated_at = self._prepare_fork_directory(
                            tmp_dir,
                            dest_dir=dest_dir,
                            parent_session_id=parent_session_id,
                            new_session_id=new_session_id,
                            copied_sub_agent_artifacts=copied_sub_agent_artifacts,
                            last_surface=last_surface,
                        )
                        # Index before the rename commits the fork.
                        self._record_mru(new_session_id, fork_updated_at)
                        os.replace(tmp_dir, dest_dir)
                        fsync_directory(self._dir)
                        tmp_dir = None
                        return new_session_id
                    finally:
                        if tmp_dir is not None:
                            self._remove_fork_temp_dir(tmp_dir)
            raise SessionForkError("Could not allocate a collision-free fork session id") from last_collision

    def _new_fork_session_id(self) -> str:
        """Return a collision-free canonical session id for a fork."""
        for _attempt in range(SESSION_FORK_MAX_ID_ATTEMPTS):
            candidate = str(uuid4())
            if self._fork_destination_available(candidate):
                return candidate
        raise SessionForkError("Could not allocate a collision-free fork session id")

    def _fork_destination_available(self, session_id: str) -> bool:
        try:
            self._assert_fork_destination_available(session_id)
        except SessionForkError:
            return False
        return True

    def _assert_fork_destination_available(self, session_id: str, *, include_write_lock: bool = True) -> None:
        paths = [
            self._session_dir(session_id),
            self._legacy_path(session_id),
            session_active_lock_path(self._dir, session_id),
            session_active_owner_path(self._dir, session_id),
        ]
        if include_write_lock:
            paths.append(session_write_lock_path(self._dir, session_id))
        for path in paths:
            if path.exists():
                raise SessionForkError(f"Fork destination collision at {path}")

    def _new_fork_temp_dir(self, session_id: str) -> Path:
        for _attempt in range(SESSION_FORK_MAX_ID_ATTEMPTS):
            path = self._dir / f".{_session_short_id(session_id)}.fork.{uuid4().hex}.tmp"
            if not path.exists():
                return path
        raise SessionForkError("Could not allocate a temporary fork directory")

    @staticmethod
    def _remove_fork_temp_dir(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("Failed to remove temporary fork directory %s", path, exc_info=True)

    def _prepare_fork_directory(
        self,
        tmp_dir: Path,
        *,
        dest_dir: Path,
        parent_session_id: str,
        new_session_id: str,
        copied_sub_agent_artifacts: list[str],
        last_surface: SessionSurface | None = None,
    ) -> datetime:
        """Delete non-copyable files and rewrite fork-local session identity.

        Returns the ``updated_at`` stamped into the fork's envelope so the
        caller can index the new session with the value actually on disk.
        """
        with contextlib.suppress(FileNotFoundError):
            (tmp_dir / SESSION_RECOVERY_FILE_NAME).unlink()
        with contextlib.suppress(FileNotFoundError):
            (tmp_dir / RAW_HTTP_LOG_FILE_NAME).unlink()
        self._prepare_fork_sub_agent_artifacts(tmp_dir)
        try:
            side_questions = load_side_questions(self._session_dir(parent_session_id), parent_session_id)
        except OSError, ValueError:
            logger.warning("Omitting unreadable side questions from fork of session %s", parent_session_id)
            (tmp_dir / BTW_FILE_NAME).unlink(missing_ok=True)
        else:
            if (tmp_dir / BTW_FILE_NAME).exists():
                write_side_questions(tmp_dir, new_session_id, side_questions)

        fork_updated_at = datetime.now(UTC)
        updated_at = fork_updated_at.isoformat()
        parent_clipboard_root = (self._session_dir(parent_session_id) / "attachments" / "clipboard").resolve(
            strict=False
        )
        fork_clipboard_root = (dest_dir / "attachments" / "clipboard").resolve(strict=False)
        primary = tmp_dir / SESSION_FILE_NAME
        self._rewrite_fork_envelope_file(
            primary,
            parent_session_id=parent_session_id,
            new_session_id=new_session_id,
            updated_at=updated_at,
            parent_clipboard_root=parent_clipboard_root,
            fork_clipboard_root=fork_clipboard_root,
            last_surface=last_surface,
        )
        backup = tmp_dir / SESSION_BACKUP_FILE_NAME
        if backup.exists():
            rewritten = self._try_rewrite_auxiliary_fork_envelope_file(
                backup,
                parent_session_id=parent_session_id,
                new_session_id=new_session_id,
                updated_at=updated_at,
                parent_clipboard_root=parent_clipboard_root,
                fork_clipboard_root=fork_clipboard_root,
                last_surface=last_surface,
            )
            if not rewritten:
                atomic_copy_file(primary, backup)

        for snapshot in rollback_snapshot_paths(tmp_dir):
            rewritten = self._try_rewrite_auxiliary_fork_envelope_file(
                snapshot,
                parent_session_id=parent_session_id,
                new_session_id=new_session_id,
                updated_at=updated_at,
                parent_clipboard_root=parent_clipboard_root,
                fork_clipboard_root=fork_clipboard_root,
                last_surface=last_surface,
            )
            if not rewritten:
                snapshot.unlink()

        self._rewrite_fork_sub_agent_logs(
            tmp_dir,
            new_session_id=new_session_id,
            parent_clipboard_root=parent_clipboard_root,
            fork_clipboard_root=fork_clipboard_root,
            artifact_names=copied_sub_agent_artifacts,
        )
        return fork_updated_at

    @staticmethod
    def _make_fork_copy_ignore(parent_root: Path) -> Callable[[str, list[str]], set[str]]:
        # Exclude the SESSION-ROOT sub_agents subtree from the generic tree
        # copy. copytree(symlinks=True) copies symlinks verbatim but still
        # FOLLOWS NT directory junctions (os.path.islink() is False for them),
        # so a same-uid untrusted ACP process could plant a junction here that
        # gets materialized as a real directory and evades every post-copy link
        # check — a path back to the parent recurses to disk exhaustion. The
        # only artifacts that carry forward (sessions/*.json + *.stderr.log)
        # are re-created afterward by _secure_copy_sub_agent_artifacts, whose
        # owner-verified writes validate a link-free parent chain.
        parent_key = os.path.normcase(os.path.abspath(parent_root))
        drop_junctions = make_junction_dropping_ignore()

        def _ignore(path: str, names: list[str]) -> set[str]:
            # Drop NT directory junctions at EVERY level, not just the root —
            # a junction planted anywhere the copy reaches, including a nested
            # ``compactions/sub_agents/<tool>/<invocation>/…`` spill dir the
            # same-uid untrusted ACP process can write, would be materialized
            # through plain CopyFile2 (bypassing owner-only recreation).
            excluded = drop_junctions(path, names)
            # Match the session ROOT only for the by-name sub_agents exclusion:
            # nested directories that merely share the name — kernel compaction
            # spill lives under ``compactions/sub_agents/<tool>/<invocation>/…``
            # — must copy through untouched, or the fork loses dropped-turn
            # records while keeping audit/catalog references to them.
            if os.path.normcase(os.path.abspath(path)) == parent_key:
                # Case-insensitive on the name: NTFS is case-insensitive, so a
                # planted ``SUB_AGENTS`` dir would slip past an exact-case check
                # yet copytree would still copy it. Exclude every case variant
                # at the root; sessions/ is rebuilt afterward regardless.
                excluded.update(name for name in names if name.casefold() == "sub_agents")
                # The trajectory log is per-session execution history: the
                # fork opens its own (see service.trajectory.fork), and the
                # parent's writer may hold this one open right now.
                excluded.update(name for name in names if name.casefold() == "trajectory")
                # Remembered session permissions belong only to the original session.
                excluded.update(
                    name for name in names if name.casefold() in {"approval-grants.json", "approval-grants.json.lock"}
                )
                # Background command logs and stop files belong to commands the original session runs.
                excluded.update(name for name in names if name.casefold() == BACKGROUND_DIR_NAME)
            return excluded

        return _ignore

    @staticmethod
    def _sanitize_fork_sub_agent_layout(tmp_dir: Path) -> None:
        """Drop symlinked sub_agents levels from the fork copy before touching them.

        The parent session's sub_agents tree is writable by the same-uid
        UNTRUSTED ACP process, and ``copytree(symlinks=True)`` preserves a
        planted directory symlink verbatim (its children never reach the
        ignore callback). Every later cleanup/rewrite in the fork copy would
        then resolve THROUGH the link and rmtree/unlink data outside the
        session. Links are removed, never followed.
        """
        sub_agents = tmp_dir / "sub_agents"
        for candidate in (sub_agents, sub_agents / "sessions", sub_agents / "pending"):
            try:
                if candidate.is_symlink() or candidate.is_junction():
                    candidate.unlink()
                    logger.warning("Removed linked sub-agent layout entry from fork copy: %s", candidate.name)
            except OSError:
                logger.warning("Failed to sanitize fork sub-agent layout entry: %s", candidate.name, exc_info=True)

    @classmethod
    def _secure_copy_sub_agent_artifacts(cls, parent_dir: Path, tmp_dir: Path) -> list[str]:
        """Rebuild the fork's sub_agents/sessions dir; return the copied names.

        The returned basenames are the EXACT set the rewrite pass must process
        (see _rewrite_fork_sub_agent_logs): the reference-driven pass below can
        copy audits past the anti-flood scan cap, so the rewrite must iterate
        this concrete set rather than re-scanning under the same cap (which would
        leave a referenced-but-uncapped audit copied yet never id-rewritten).
        """
        source_root = parent_dir / "sub_agents"
        source_dir = source_root / "sessions"
        # Same trust boundary as the fork-copy sanitizer: never traverse a
        # linked layout the ACP process may have planted in the live session.
        if source_root.is_symlink() or source_root.is_junction() or source_dir.is_symlink() or source_dir.is_junction():
            logger.warning("Skipping fork copy of linked sub-agent layout: %s", source_root.name)
            return []
        if not source_dir.is_dir():
            return []
        destination_dir = tmp_dir / "sub_agents" / "sessions"
        copied: set[str] = set()

        def _copy_artifact(name: str) -> None:
            try:
                # Owner-verified, not owner-only: parent sessions written by
                # releases predating the owner-only writers must still fork.
                # Bounded: a same-uid untrusted ACP process could plant an
                # over-sized/sparse artifact; over-cap files are skipped.
                payload = read_owner_verified_bounded(source_dir / name, max_bytes=MAX_SUB_AGENT_AUDIT_BYTES)
                atomic_write_owner_only_bytes(destination_dir / name, payload)
            except OSError, ValueError:
                logger.warning("Omitting insecure fork sub-agent artifact: %s", name, exc_info=True)
            copied.add(name)

        # The parent audit dir is same-uid ACP-writable: bound BOTH enumeration
        # (Path.iterdir is eager on 3.14 → a planted flood OOMs the fork during
        # listing) and cumulative read (4096 x 128 MiB would be a huge copy) with
        # the shared scandir scanner. This best-effort pass carries UNREFERENCED
        # artifacts (e.g. reconcile orphans) forward up to the caps; the
        # referenced pass below guarantees the ones the forked history links.
        sources, truncated = scan_bounded_json_files(
            source_dir,
            max_files=MAX_SCANNED_AUDIT_FILES,
            max_total_bytes=MAX_AUDIT_SCAN_TOTAL_BYTES,
            suffixes=(".json", ".stderr.log"),
        )
        if truncated:
            logger.warning("Fork sub-agent artifact scan truncated; unreferenced artifacts beyond the cap are omitted")
        for source in sources:
            _copy_artifact(source.name)

        # Guarantee every audit the forked history references is carried forward
        # even when the bounded scan truncated before reaching it. The reference
        # set comes from the already-copied forked history (session.json), so it
        # is bounded by history size — NOT by the attacker-writable file count —
        # and a legitimately large session can never fork with a dangling
        # sub_agent_log_file link. The generic copytree already placed
        # session.json in tmp_dir; the later fork envelope rewrite leaves these
        # references untouched. A corrupt/over-huge session.json simply yields no
        # referenced set (the bounded scan above still ran).
        try:
            envelope = cls._read_json_file(tmp_dir / SESSION_FILE_NAME)
        except ValueError, RecursionError:
            envelope = None
        if not isinstance(envelope, dict):
            return sorted(copied)
        for name in sorted(collect_sub_agent_log_file_references(envelope)):
            if not is_safe_sub_agent_log_basename(name):
                continue
            if name not in copied:
                _copy_artifact(name)
            # Carry the audit's stderr sibling too (deterministic name); a
            # missing one is normal (the sub-agent may not have written stderr),
            # so only attempt a copy when it is present to avoid log noise.
            stderr_name = f"{name}.stderr.log"
            if stderr_name not in copied and (source_dir / stderr_name).is_file():
                _copy_artifact(stderr_name)
        return sorted(copied)

    @staticmethod
    def _prepare_fork_sub_agent_artifacts(tmp_dir: Path) -> None:
        sub_agents = tmp_dir / "sub_agents"
        # Defense in depth behind _sanitize_fork_sub_agent_layout: if the
        # link removal failed, refusing to traverse beats deleting through it.
        if sub_agents.is_symlink() or sub_agents.is_junction() or not sub_agents.is_dir():
            return
        shutil.rmtree(sub_agents / "pending", ignore_errors=True)
        for legacy_pending in sub_agents.glob("*.json"):
            with contextlib.suppress(OSError):
                legacy_pending.unlink()
        for temp_file in sub_agents.rglob("*.tmp"):
            with contextlib.suppress(OSError):
                temp_file.unlink()

    @classmethod
    def _rewrite_fork_sub_agent_logs(
        cls,
        tmp_dir: Path,
        *,
        new_session_id: str,
        parent_clipboard_root: Path,
        fork_clipboard_root: Path,
        artifact_names: list[str] | None = None,
    ) -> None:
        sessions_dir = tmp_dir / "sub_agents" / "sessions"
        # Defense in depth behind _sanitize_fork_sub_agent_layout: rewritten
        # envelopes must never be written through a linked layout.
        if (
            (tmp_dir / "sub_agents").is_symlink()
            or (tmp_dir / "sub_agents").is_junction()
            or sessions_dir.is_symlink()
            or sessions_dir.is_junction()
            or not sessions_dir.is_dir()
        ):
            return
        if artifact_names is not None:
            # Rewrite EXACTLY the set _secure_copy_sub_agent_artifacts wrote. Its
            # reference-driven pass can copy audits past the anti-flood scan cap,
            # so re-scanning here under the same cap would leave those referenced
            # audits copied yet never id-rewritten (stale parent_session_id). The
            # rebuilt dir is chrys-owned inside the unguessable fork tmp, so this
            # concrete list — bounded by the copier — is the authoritative set.
            rewrite_paths = sorted(sessions_dir / name for name in artifact_names if name.endswith(".json"))
        else:
            # Direct callers (no prior copy) fall back to the bounded scanner so
            # no fork audit scan relies on an eager glob over an ACP-influenced
            # tree.
            rewrite_paths, _truncated = scan_bounded_json_files(
                sessions_dir,
                max_files=MAX_SCANNED_AUDIT_FILES,
                max_total_bytes=MAX_AUDIT_SCAN_TOTAL_BYTES,
            )
        for path in rewrite_paths:
            try:
                envelope = json.loads(
                    read_owner_verified_bounded(path, max_bytes=MAX_SUB_AGENT_AUDIT_BYTES).decode("utf-8")
                )
            except OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError:
                # RecursionError (not a ValueError): json.loads recurses per
                # nesting level, so a deeply nested but byte-bounded artifact
                # blows the stack. Skip that one file rather than abort the fork.
                logger.warning("Skipping malformed fork sub-agent audit log: %s", path, exc_info=True)
                continue
            if not isinstance(envelope, dict):
                logger.warning("Leaving opaque fork sub-agent artifact untouched: %s", path)
                continue
            meta = envelope.get("meta")
            if not isinstance(meta, dict):
                logger.warning("Leaving metadata-free fork sub-agent artifact untouched: %s", path)
                continue
            runner = meta.get("runner", "kernel")
            if runner not in {"kernel", "acp"}:
                continue
            if runner == "acp":
                acp_state = envelope.get("acp_state")
                try:
                    if not isinstance(acp_state, dict):
                        raise ValueError("ACP sub-agent log is missing ACP state")
                    validate_acp_state_file_references(acp_state, parent_session_dir=tmp_dir)
                except ValueError:
                    logger.warning("Omitting unsafe ACP fork sub-agent audit log: %s", path, exc_info=True)
                    path.unlink()
                    continue
            meta["parent_session_id"] = new_session_id
            state = envelope.get("state")
            if runner == "kernel" and isinstance(state, dict):
                cls._rewrite_clipboard_mentions_in_state(
                    state,
                    parent_clipboard_root=parent_clipboard_root,
                    fork_clipboard_root=fork_clipboard_root,
                )
            try:
                serialized = json.dumps(envelope, indent=2, ensure_ascii=False, allow_nan=False)
            except ValueError:
                # A legacy artifact written with the default allow_nan=True can
                # carry NaN/Infinity that json.loads accepts but the strict
                # re-encode rejects. Skip this one file (leave the original,
                # parent-scoped copy) rather than aborting the entire fork.
                logger.warning("Leaving non-encodable fork sub-agent artifact untouched: %s", path, exc_info=True)
                continue
            # A lone surrogate a same-uid child may have planted in the audit is
            # neutralized at the sink (atomic_write_owner_only_text encodes with
            # backslashreplace), so this write cannot abort the fork.
            atomic_write_owner_only_text(path, serialized)

    def _rewrite_fork_envelope_file(
        self,
        path: Path,
        *,
        parent_session_id: str,
        new_session_id: str,
        updated_at: str,
        parent_clipboard_root: Path,
        fork_clipboard_root: Path,
        last_surface: SessionSurface | None = None,
    ) -> None:
        envelope = self._read_json_file(path)
        if envelope is None:
            raise SessionForkError(f"Cannot rewrite invalid session envelope: {path}")
        self._rewrite_fork_envelope(
            envelope,
            parent_session_id=parent_session_id,
            new_session_id=new_session_id,
            updated_at=updated_at,
            parent_clipboard_root=parent_clipboard_root,
            fork_clipboard_root=fork_clipboard_root,
            last_surface=last_surface,
        )
        _atomic_write_text(path, json.dumps(envelope, indent=2, ensure_ascii=False))

    def _try_rewrite_auxiliary_fork_envelope_file(
        self,
        path: Path,
        *,
        parent_session_id: str,
        new_session_id: str,
        updated_at: str,
        parent_clipboard_root: Path,
        fork_clipboard_root: Path,
        last_surface: SessionSurface | None = None,
    ) -> bool:
        envelope = self._read_json_file(path)
        if envelope is None:
            logger.warning("Skipping invalid fork auxiliary session envelope: %s", path)
            return False
        try:
            self._rewrite_fork_envelope(
                envelope,
                parent_session_id=parent_session_id,
                new_session_id=new_session_id,
                updated_at=updated_at,
                parent_clipboard_root=parent_clipboard_root,
                fork_clipboard_root=fork_clipboard_root,
                last_surface=last_surface,
            )
        except SessionForkError:
            logger.warning("Skipping malformed fork auxiliary session envelope: %s", path, exc_info=True)
            return False
        _atomic_write_text(path, json.dumps(envelope, indent=2, ensure_ascii=False))
        return True

    @classmethod
    def _rewrite_fork_envelope(
        cls,
        envelope: dict[str, Any],
        *,
        parent_session_id: str,
        new_session_id: str,
        updated_at: str,
        parent_clipboard_root: Path,
        fork_clipboard_root: Path,
        last_surface: SessionSurface | None = None,
    ) -> None:
        meta = envelope.get("meta")
        if not isinstance(meta, dict):
            raise SessionForkError("Session envelope is missing metadata")
        meta["session_id"] = new_session_id
        meta["parent_session_id"] = parent_session_id
        meta["updated_at"] = updated_at
        meta["service_session_id"] = ""
        if last_surface is not None:
            meta["last_surface"] = last_surface.value
        # The fork is a new on-disk revision: derived summaries must not be
        # able to claim they were computed from it via the parent's id.
        envelope[SESSION_CHECKPOINT_ID_KEY] = new_analytics_id()
        # A user-pinned custom_title carries over verbatim (losing the rename
        # reads as data loss).  The carried pin keeps auto-generated refreshes
        # disabled on the fork, exactly as on the parent, until renamed.

        state = envelope.get("state")
        if isinstance(state, dict):
            # The fork records into a log of its own, numbered from one: the
            # registry's sequences point into the parent's log and would make a
            # rollback here name a range that never existed, so they are
            # cleared. The turns themselves carry over — the fork inherits the
            # conversation — restated with the session that first ran them.
            fork_turn_registry(state, origin_session_id=parent_session_id)
            cls._rewrite_clipboard_mentions_in_state(
                state,
                parent_clipboard_root=parent_clipboard_root,
                fork_clipboard_root=fork_clipboard_root,
            )

    @classmethod
    def _rewrite_clipboard_mentions_in_state(
        cls,
        state: dict[str, Any],
        *,
        parent_clipboard_root: Path,
        fork_clipboard_root: Path,
    ) -> None:
        messages = state.get("messages")
        if isinstance(messages, list):
            cls._rewrite_clipboard_mentions_in_messages(
                messages,
                parent_clipboard_root=parent_clipboard_root,
                fork_clipboard_root=fork_clipboard_root,
            )
        compressed_blocks = state.get("compressed_msgs")
        if not isinstance(compressed_blocks, list):
            return
        for block in compressed_blocks:
            if not isinstance(block, dict):
                continue
            block_messages = block.get("messages")
            if isinstance(block_messages, list):
                cls._rewrite_clipboard_mentions_in_messages(
                    block_messages,
                    parent_clipboard_root=parent_clipboard_root,
                    fork_clipboard_root=fork_clipboard_root,
                )

    @classmethod
    def _rewrite_clipboard_mentions_in_messages(
        cls,
        messages: list[Any],
        *,
        parent_clipboard_root: Path,
        fork_clipboard_root: Path,
    ) -> None:
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            contents = message.get("contents")
            if not isinstance(contents, list):
                continue
            for index, content in enumerate(contents):
                if isinstance(content, str):
                    contents[index] = cls._rewrite_clipboard_mentions_in_text(
                        content,
                        parent_clipboard_root=parent_clipboard_root,
                        fork_clipboard_root=fork_clipboard_root,
                    )
                    continue
                if not _is_string_keyed_dict(content) or content.get("type") != "text":
                    continue
                text = content.get("text")
                if isinstance(text, str):
                    content["text"] = cls._rewrite_clipboard_mentions_in_text(
                        text,
                        parent_clipboard_root=parent_clipboard_root,
                        fork_clipboard_root=fork_clipboard_root,
                    )

    @staticmethod
    def _rewrite_clipboard_mentions_in_text(
        text: str,
        *,
        parent_clipboard_root: Path,
        fork_clipboard_root: Path,
    ) -> str:
        tokens = iter_mention_tokens(text)
        if not tokens:
            return text

        pieces: list[str] = []
        last = 0
        changed = False
        for token in tokens:
            # Clipboard mentions written by the TUI are absolute paths. Relative
            # hand-authored mentions resolve against the process cwd here and
            # normally will not match the session-local clipboard directory.
            resolved = SessionForkMixin._resolve_mention_path(token.value)
            if resolved is None:
                continue
            try:
                relative = resolved.relative_to(parent_clipboard_root)
            except ValueError:
                continue
            pieces.append(text[last : token.start])
            pieces.append(format_file_mention(fork_clipboard_root / relative))
            last = token.end
            changed = True
        if not changed:
            return text
        pieces.append(text[last:])
        return "".join(pieces)

    @staticmethod
    def _resolve_mention_path(value: str) -> Path | None:
        try:
            return Path(value).expanduser().resolve(strict=False)
        except OSError:
            return None
        except RuntimeError:
            return None
        except ValueError:
            return None
