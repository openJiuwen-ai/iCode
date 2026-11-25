# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-platform advisory file lock for serialising shared-file writes.

When multiple chrys processes touch the same file under ``~/.chrys/`` (config,
session metadata, mutation snapshots, etc.), wrapping the read-modify-write in
a :class:`FileLock` against a sibling ``.lock`` file prevents lost updates and
torn writes. The OS releases the lock automatically when the file descriptor
closes (process exit, crash, or normal release), so stale lock files are not a
concern.

POSIX uses ``fcntl.flock`` (LOCK_EX, advisory). Windows uses ``msvcrt.locking``
(LK_LOCK, mandatory on the locked byte range). Both are blocking by default;
release happens on context exit or fd close.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path
from typing import IO

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import msvcrt
else:
    import fcntl


# In-process serializer keyed by lock-file path. Same-process threads queue
# here before competing for the OS lock — without it, Windows' polled
# ``msvcrt.locking(LK_NBLCK)`` can starve under thread contention because it
# has no kernel-side fairness guarantee. ``threading.Lock`` is not strictly
# FIFO but in practice serializes well enough to eliminate the starvation.
# Across processes the OS lock still does the work; this layer only collapses
# within-process noise.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _get_path_lock(path: Path) -> threading.Lock:
    # Normalize to an absolute lexical path so equivalent spellings
    # (``foo.lock`` vs ``./foo.lock`` vs absolute) hash to the same key.
    # ``absolute()`` is purely lexical (no stat/symlink resolution) so it
    # stays cheap on the hot path. ``normcase`` collapses Windows
    # case-insensitive variants (``C:\Foo`` vs ``c:\foo``); it's a no-op
    # on POSIX.
    key = os.path.normcase(str(path.absolute()))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


class FileLock:
    """Context-managed exclusive lock on a sidecar ``.lock`` file.

    The lock file is created if missing. Locking blocks until acquired (with an
    optional timeout). Release happens on ``__exit__`` or fd close — the OS
    cleans up if the process dies mid-critical-section.

    Usage::

        with FileLock(path):
            # critical section — exclusive across processes
            ...
    """

    # Time slice for non-blocking lock-retry polling (seconds).
    _RETRY_INTERVAL = 0.05

    def __init__(self, path: Path | str, timeout: float | None = None) -> None:
        """Create a lock bound to ``path``.

        Args:
            path: Sidecar lock file path. Parent dir must exist (caller's job).
            timeout: Max seconds to wait; ``None`` blocks indefinitely. Raises
                ``TimeoutError`` if exceeded.
        """
        self._path = Path(path)
        self._timeout = timeout
        self._fp: IO[bytes] | None = None
        self._proc_lock: threading.Lock | None = None

    @property
    def path(self) -> Path:
        """The lock file selected when this handle was created."""
        return self._path

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def acquire(self) -> None:
        """Acquire the lock, blocking until success or timeout."""
        proc_lock = _get_path_lock(self._path)
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        # Block in-process threads here first so only one thread per process
        # races for the OS lock. The OS-lock step gets whatever timeout is
        # left after this wait.
        if self._timeout is None:
            proc_lock.acquire()
        elif not proc_lock.acquire(timeout=self._timeout):
            msg = f"Timed out acquiring lock {self._path}"
            raise TimeoutError(msg)
        try:
            # Open in append+binary so the file is created if missing and content
            # is never truncated. We never read or write the file — only lock it.
            fp = self._open_lock_file()
            try:
                if deadline is None:
                    self._lock_fd(fp)
                else:
                    # ``_lock_fd`` already tries once non-blocking before
                    # checking the deadline, so a free OS lock succeeds
                    # even when file-open ate the timeout budget. Clamp
                    # to 0 so the inner loop raises on the first miss.
                    remaining = deadline - time.monotonic()
                    prior_timeout = self._timeout
                    self._timeout = max(0.0, remaining)
                    try:
                        self._lock_fd(fp)
                    finally:
                        self._timeout = prior_timeout
            except BaseException:
                fp.close()
                raise
            self._fp = fp
            self._proc_lock = proc_lock
        except BaseException:
            proc_lock.release()
            raise

    def _open_lock_file(self) -> IO[bytes]:
        """Open the sidecar lock file, refusing anything but a regular file.

        A same-uid but untrusted process (e.g. an ACP sub-agent) can pre-plant
        a symlink or FIFO at the predictable ``.lock`` path. A plain
        ``Path.open("ab")`` would follow the symlink, or — for a FIFO — *block
        indefinitely inside open()*, before the acquire timeout (which only
        guards the flock step) ever begins. When a lock is taken synchronously
        on the event-loop thread that freezes the whole process. So on POSIX we
        open with ``O_NOFOLLOW | O_NONBLOCK`` (reject a symlinked leaf, never
        block on a FIFO) and then require ``S_ISREG`` via ``fstat`` (reject
        FIFO/socket/device). We only advisory-lock the fd and never read or
        write it, so the residual ``O_NONBLOCK`` is harmless.
        """
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if _IS_WINDOWS:
            flags |= getattr(os, "O_NOINHERIT", 0)
        else:
            flags |= os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(self._path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                msg = f"Refusing to lock non-regular file {self._path}"
                raise OSError(msg)
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, "ab")

    def release(self) -> None:
        """Release the lock. Safe to call multiple times."""
        fp = self._fp
        proc_lock = self._proc_lock
        if fp is None:
            return
        self._fp = None
        self._proc_lock = None
        try:
            try:
                self._unlock_fd(fp)
            finally:
                fp.close()
        finally:
            if proc_lock is not None:
                proc_lock.release()

    def _lock_fd(self, fp: IO[bytes]) -> None:
        if _IS_WINDOWS:
            self._lock_windows(fp)
        else:
            self._lock_posix(fp)

    def _unlock_fd(self, fp: IO[bytes]) -> None:
        if _IS_WINDOWS:
            try:
                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                # If the lock was never granted or already gone, ignore.
                pass
        else:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)

    def _lock_posix(self, fp: IO[bytes]) -> None:
        if self._timeout is None:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            return

        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    msg = f"Timed out acquiring lock {self._path}"
                    raise TimeoutError(msg) from None
                time.sleep(min(self._RETRY_INTERVAL, remaining))

    def _lock_windows(self, fp: IO[bytes]) -> None:
        # msvcrt locks a byte range; we lock byte 0. The file is opened in
        # append mode so the position is at EOF — seek to 0 first.
        fp.seek(0)
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        while True:
            try:
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if deadline is None:
                    time.sleep(self._RETRY_INTERVAL)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    msg = f"Timed out acquiring lock {self._path}"
                    raise TimeoutError(msg) from None
                time.sleep(min(self._RETRY_INTERVAL, remaining))
