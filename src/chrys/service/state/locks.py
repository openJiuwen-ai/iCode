# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session lock helpers for cross-process active-session ownership."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.state.store import (
    SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS,
    session_active_lock_path,
    session_active_owner_path,
)

if TYPE_CHECKING:
    from chrys.service.state.store import StateStore


SESSION_RESTORE_ACTIVE_LOCK_TIMEOUT_SECONDS = 0.25
"""Fast contention check for interactive session restore."""


class ActiveSessionGuard:
    """Owns the long-lived lock that marks a Chrys session active."""

    def __init__(self, state_store: StateStore | None) -> None:
        self._state_store = state_store
        self._lock: FileLock | None = None
        self._session_id: str | None = None

    def owns(self, session_id: str) -> bool:
        """Return whether this guard currently owns *session_id*."""
        if self._lock is None or self._session_id is None:
            return False
        if self._session_id == session_id:
            return True
        owned_path = self._lock_path(self._session_id)
        requested_path = self._lock_path(session_id)
        return owned_path is not None and owned_path == requested_path

    def ensure(self, session_id: str) -> bool:
        """Ensure this process owns *session_id* or report lock contention."""
        if self.owns(session_id):
            return True
        try:
            lock = self.acquire_for_restore(session_id)
        except TimeoutError:
            return False
        self.install(session_id, lock)
        return True

    def acquire_for_restore(
        self,
        session_id: str,
        *,
        timeout: float | None = None,
    ) -> FileLock | None:
        """Acquire a lock handle for a session that is about to be restored."""
        if timeout is None:
            timeout = SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS
        path = self._lock_path(session_id)
        if path is None:
            return None
        lock = FileLock(path, timeout=timeout)
        lock.acquire()
        return lock

    async def acquire_for_restore_async(self, session_id: str, *, timeout: float) -> FileLock | None:
        """Acquire off-thread and release the result if the waiting restore is cancelled."""
        pending = asyncio.create_task(asyncio.to_thread(self.acquire_for_restore, session_id, timeout=timeout))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:

            def release_abandoned(task: asyncio.Task[FileLock | None]) -> None:
                if not task.cancelled() and task.exception() is None and (lock := task.result()) is not None:
                    lock.release()

            pending.add_done_callback(release_abandoned)
            raise

    def install(self, session_id: str, lock: FileLock | None) -> None:
        """Make *lock* the active lock held by this guard."""
        if lock is None:
            return
        self.release()
        self._lock = lock
        self._session_id = session_id
        self._write_owner(session_id)

    def release(self) -> None:
        """Release the active-session lock held by this guard."""
        lock = self._lock
        session_id = self._session_id
        if lock is None:
            return
        if session_id is not None:
            owner_path = self._owner_path(session_id)
            if owner_path is not None:
                with contextlib.suppress(OSError):
                    owner_path.unlink()
        self._lock = None
        self._session_id = None
        lock.release()

    def conflict_message(self, session_id: str) -> str:
        """Return a user-facing message for an active-session conflict."""
        detail = self._format_owner(self._read_owner(session_id))
        return (
            f"Session '{session_short_id(session_id)}' is already open in another {APP_DISPLAY_NAME} instance{detail}."
        )

    def _sessions_root_dir(self, session_id: str) -> Path | None:
        if self._state_store is None:
            return None
        return self._state_store.session_dir(session_id).parent

    def _lock_path(self, session_id: str) -> Path | None:
        sessions_dir = self._sessions_root_dir(session_id)
        if sessions_dir is None:
            return None
        path = session_active_lock_path(sessions_dir, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _owner_path(self, session_id: str) -> Path | None:
        sessions_dir = self._sessions_root_dir(session_id)
        if sessions_dir is None:
            return None
        path = session_active_owner_path(sessions_dir, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _write_owner(self, session_id: str) -> None:
        path = self._owner_path(session_id)
        if path is None:
            return
        owner = {
            "session_id": session_id,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        with contextlib.suppress(OSError):
            atomic_write_text(path, json.dumps(owner, indent=2, ensure_ascii=False))

    def _read_owner(self, session_id: str) -> dict[str, object]:
        path = self._owner_path(session_id)
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _format_owner(owner: dict[str, object]) -> str:
        pid = owner.get("pid")
        return f" (pid={pid})" if pid else ""
