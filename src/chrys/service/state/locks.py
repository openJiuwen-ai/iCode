# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session lock helpers for cross-process active-session ownership."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import atomic_write_text
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.once_close import finish_close
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.state.store import (
    SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS,
    session_active_lock_path,
    session_active_owner_path,
)
from chrys.service.state.takeover import TakeoverOwner, credential_path, read_owner_record

if TYPE_CHECKING:
    from chrys.service.state.store import StateStore


SESSION_RESTORE_ACTIVE_LOCK_TIMEOUT_SECONDS = 0.25
"""Fast contention check for interactive session restore."""


class ActiveSessionGuard:
    """Owns the long-lived lock that marks a Chrys session active."""

    def __init__(self, state_store: StateStore | None) -> None:
        self._takeover_file: Path | None = None
        self.takeover_port: int | None = None
        self.takeover_owner: TakeoverOwner | None = None
        self._state_store = state_store
        self._lock: FileLock | None = None

    def owns(self, session_id: str) -> bool:
        """Return whether this guard currently owns *session_id*."""
        if self._lock is None:
            return False
        requested = self._lock_path(session_id)
        if requested is None:
            return False
        if requested == self._lock.path:
            return True
        try:
            return requested.samefile(self._lock.path)
        except FileNotFoundError:
            return False

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
        """Transfer the acquired lock to the waiter, releasing it if that waiter is cancelled."""
        # An executor future survives asyncio.Runner cancelling every task on exit.
        worker = asyncio.get_running_loop().run_in_executor(
            None, partial(self.acquire_for_restore, session_id, timeout=timeout)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # The thread may acquire the lock after cancellation. Drain first,
            # then reclaim its result even if the waiter was cancelled again.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await finish_close(worker)
            if not worker.cancelled() and worker.exception() is None and (lock := worker.result()) is not None:
                with contextlib.suppress(OSError):
                    lock.release()
            raise

    def install(self, session_id: str, lock: FileLock | None) -> None:
        """Make *lock* the active lock held by this guard."""
        if lock is None:
            return
        self.release()
        self._lock = lock
        self._write_owner(session_id, lock.path.with_suffix(".json"))

    def release(self) -> None:
        """Release the active-session lock held by this guard."""
        lock = self._lock
        if lock is None:
            return
        self.takeover_owner = None
        if self._takeover_file is not None:
            with contextlib.suppress(OSError):
                self._takeover_file.unlink(missing_ok=True)
            self._takeover_file = None
        with contextlib.suppress(OSError):
            lock.path.with_suffix(".json").unlink()
        self._lock = None
        lock.release()

    def conflict_message(self, session_id: str) -> str:
        """Return a user-facing message for an active-session conflict."""
        return self.conflict_details(session_id)[0]

    def conflict_details(self, session_id: str) -> tuple[str, str | None]:
        owner = self._read_owner(session_id)
        generation = owner.get("takeover_generation") if owner.get("session_id") == session_id else None
        detail = self._format_owner(owner)
        return (
            f"Session '{session_short_id(session_id)}' is already open in another {APP_DISPLAY_NAME} instance{detail}.",
            generation if isinstance(generation, str) and len(generation) == 64 else None,
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

    def _write_owner(self, session_id: str, path: Path) -> None:
        owner: dict[str, object] = {
            "session_id": session_id,
            "pid": os.getpid(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        import psutil

        with contextlib.suppress(psutil.Error, OSError):
            if (parent := psutil.Process().parent()) is not None:
                owner["parent_name"] = sanitize_legacy_scalar(parent.name())[:128]
        if self.takeover_port is not None:
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                generation = secrets.token_hex(32)
                self._takeover_file = credential_path(path, session_id, generation)
                self.takeover_owner = TakeoverOwner.advertise(self.takeover_port, path, session_id, generation)
                owner["takeover_generation"] = generation
        with contextlib.suppress(OSError):
            atomic_write_text(path, json.dumps(owner))

    def _read_owner(self, session_id: str) -> dict[str, object]:
        path = self._owner_path(session_id)
        return read_owner_record(path) if path is not None else {}

    @staticmethod
    def _format_owner(owner: dict[str, object]) -> str:
        pid = owner.get("pid")
        if type(pid) is not int or pid <= 0:
            return ""
        parent = owner.get("parent_name")
        if isinstance(parent, str) and parent:
            return f" (pid={pid}, parent={sanitize_legacy_scalar(parent)[:128]})"
        return f" (pid={pid})"
