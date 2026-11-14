# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session writer lease: one trajectory writer per session, across processes.

The lease is an OS-level file lock on ``<session>/trajectory/writer.lock``. It
is bound to the *writer worker*, not to the "currently active session": the UI
may switch sessions while a stuck worker still holds the descriptor, and the
lease keeps following that worker until it exits (or the OS reclaims it when
the process dies — at which point the stuck thread is gone too).

Acquisition never blocks: a held lease means "this session already has a
writer somewhere", and the caller opens the session trajectory-disabled. That
refusal is what the *holder record* answers: ``writer.owner``, an owner-only
JSON file next to the lock, written by a process only once it holds the lock
and never by a refused attempt. It names the holder's pid, process start time,
host, system boot time and acquisition time, so a caller can say which process
holds this session and whether that process is still on this machine instead
of reporting an anonymous "somebody has it".

The record is diagnostics only. No lock is ever broken on the strength of it:
a refused attempt cannot see the holder's own acquisition, so "the record says
a dead process" cannot prove that no live process holds the lock right now —
between a crashed holder and the next one's record there is a window in which
the record still names the dead one. Breaking the lock there would put two
writers on one log. What actually ends a dead holder's lease is the OS: it
drops the lock when the holder goes away, which is why a crashed iCode never
blocks its session for good.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from chrys.foundation.platform.files import (
    SecureFileError,
    atomic_write_owner_only_bytes,
    read_owner_verified_bounded,
    secure_open_owner_only_append,
)

logger = logging.getLogger(__name__)

WRITER_LEASE_FILE_NAME = "writer.lock"
WRITER_OWNER_FILE_NAME = "writer.owner"
HOLDER_RECORD_SCHEMA = 1

_RECORD_MAX_BYTES = 4096
# A process created within this many seconds of the recorded start time is the
# same process; anything further apart means the pid now belongs to a newcomer.
_PID_REUSE_TOLERANCE_SECONDS = 1.0
# Two boots of the same host are told apart by their boot time, which no live
# system can share with the boot that wrote the record.
_SAME_BOOT_TOLERANCE_SECONDS = 5.0

_HELD_LEASES: dict[str, WriterLease] = {}
_HELD_GUARD = threading.Lock()

_HOST_FACTS: tuple[str, float] | None = None
_PROCESS_START_TIME: float | None = None


def _lease_key(path: Path) -> str:
    return os.path.normcase(str(path.absolute()))


def owner_record_path(lease_path: Path) -> Path:
    """The holder record that sits next to the lease at *lease_path*."""
    return lease_path.with_name(WRITER_OWNER_FILE_NAME)


@dataclass(frozen=True, slots=True)
class LeaseHolder:
    """Who took a writer lease, as recorded next to it."""

    pid: int
    start_time: float
    host: str
    boot_time: float
    at: str

    def describe(self) -> str:
        since = f" (since {self.at})" if self.at else ""
        return f"pid {self.pid} started {self.start_time:.0f} on {self.host}{since}"


def read_holder(lease_path: Path) -> LeaseHolder | None:
    """The holder the last writer recorded next to *lease_path*; ``None`` when unknown.

    Anything unreadable, torn, written under another schema or holding a
    number this platform cannot convert is unknown. A
    record left by a process that has since released the lease stays readable:
    it is only ever consulted while somebody holds the lock.
    """
    return _read_record(owner_record_path(lease_path))


def describe_holder(lease_path: Path) -> str:
    """One line naming the recorded holder and whether this machine still sees it.

    Empty when nothing readable is recorded. Never raises: it decorates a
    refused activation's report, and a diagnostic must not become the failure,
    so a failure to read the record itself is answered like an absent one.
    """
    try:
        holder = read_holder(lease_path)
    except Exception:
        logger.debug("Unable to read the recorded writer lease holder", exc_info=True)
        return ""
    if holder is None:
        return ""
    try:
        return f"{holder.describe()} ({_holder_liveness(holder)})"
    except Exception:
        logger.debug("Unable to judge whether a writer lease holder is alive", exc_info=True)
        return holder.describe()


def _host_facts() -> tuple[str, float]:
    global _HOST_FACTS
    if _HOST_FACTS is None:
        import psutil

        _HOST_FACTS = (socket.gethostname(), float(psutil.boot_time()))
    return _HOST_FACTS


def _process_start_time() -> float:
    global _PROCESS_START_TIME
    if _PROCESS_START_TIME is None:
        import psutil

        _PROCESS_START_TIME = float(psutil.Process().create_time())
    return _PROCESS_START_TIME


def _new_record() -> LeaseHolder:
    from chrys.foundation.trajectory.envelope import utc_now_rfc3339

    host, boot_time = _host_facts()
    return LeaseHolder(
        pid=os.getpid(),
        start_time=_process_start_time(),
        host=host,
        boot_time=boot_time,
        at=utc_now_rfc3339(),
    )


def _holder_liveness(holder: LeaseHolder) -> str:
    """Whether the recorded holder is still the process that wrote the record.

    A record from another host, or from another boot of this host, is not
    judged at all: this machine cannot see that pid table, so it says so
    rather than guessing.
    """
    import psutil

    host, boot_time = _host_facts()
    if holder.host != host:
        return "recorded on another host"
    if abs(boot_time - holder.boot_time) > _SAME_BOOT_TOLERANCE_SECONDS:
        return "recorded before this boot"
    try:
        process = psutil.Process(holder.pid)
    except psutil.NoSuchProcess:
        return "no longer running"
    except psutil.Error:
        return "liveness unknown"
    try:
        if abs(process.create_time() - holder.start_time) > _PID_REUSE_TOLERANCE_SECONDS:
            return "pid since reused"
    except psutil.Error:
        return "liveness unknown"
    return "running"


def _read_record(path: Path) -> LeaseHolder | None:
    try:
        payload = read_owner_verified_bounded(path, max_bytes=_RECORD_MAX_BYTES)
    except SecureFileError, OSError, ValueError:
        return None
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("schemaVersion") != HOLDER_RECORD_SCHEMA:
        return None
    try:
        return LeaseHolder(
            pid=int(data["pid"]),
            start_time=float(data["startTime"]),
            host=str(data["host"]),
            boot_time=float(data["bootTime"]),
            at=str(data.get("at", "")),
        )
    except KeyError, TypeError, ValueError, OverflowError:
        # A number JSON accepts but Python cannot convert — ``Infinity`` for the
        # pid, an integer literal wider than any float for a time — makes the
        # record as unreadable as a torn one.
        return None


def _write_record(path: Path, holder: LeaseHolder) -> None:
    payload = (
        json.dumps(
            {
                "schemaVersion": HOLDER_RECORD_SCHEMA,
                "pid": holder.pid,
                "startTime": holder.start_time,
                "host": holder.host,
                "bootTime": holder.boot_time,
                "at": holder.at,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    )
    # ``create_parents=False``: a record written while a directory is being
    # removed must never resurrect it.
    atomic_write_owner_only_bytes(path, payload.encode("utf-8"), create_parents=False)


def _try_lock_fd(fd: int) -> bool:
    from chrys.foundation.platform import get_platform

    if get_platform().is_windows:
        import msvcrt

        locking = cast(Any, msvcrt)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            locking.locking(fd, locking.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    # Same shape as the Windows branch above: the members exist only on the
    # platform the branch runs on, so the type checker is told the module is
    # opaque rather than asked to resolve them for the other one.
    flock = cast(Any, fcntl)
    try:
        flock.flock(fd, flock.LOCK_EX | flock.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError:
        return False
    return True


def _unlock_fd(fd: int) -> None:
    from chrys.foundation.platform import get_platform

    with contextlib.suppress(OSError):
        if get_platform().is_windows:
            import msvcrt

            locking = cast(Any, msvcrt)
            os.lseek(fd, 0, os.SEEK_SET)
            locking.locking(fd, locking.LK_UNLCK, 1)
        else:
            import fcntl

            flock = cast(Any, fcntl)
            flock.flock(fd, flock.LOCK_UN)


class WriterLease:
    """A held writer lease; release it only when the worker has exited."""

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd
        self._released = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def held(self) -> bool:
        return not self._released

    @classmethod
    def try_acquire(cls, path: Path, *, record_holder: bool = True) -> WriterLease | None:
        """Acquire the lease at *path* without waiting; ``None`` when held elsewhere.

        The in-process registry is the fast path; the OS lock is the truth
        across processes. The lock file is owner-only and opened with
        delete-sharing on Windows so the session directory stays renamable.
        The parent directory must already exist; open failures other than
        contention propagate as ``SecureFileError``/``OSError``.

        *record_holder* writes the holder record once the lock is in hand. A
        refused attempt writes nothing: the record always belongs to whoever
        holds the lock, which is what a caller reports as ``lease_held``'s
        holder. Probes that only ask whether the lease is free pass
        ``record_holder=False`` so they never appear as a holder themselves.
        """
        key = _lease_key(path)
        with _HELD_GUARD:
            if key in _HELD_LEASES:
                return None
            handle = secure_open_owner_only_append(path)
            if not _try_lock_fd(handle.fd):
                os.close(handle.fd)
                return None
            if record_holder:
                try:
                    _write_record(owner_record_path(path), _new_record())
                except BaseException:
                    # A holder nobody can name is a holder the next caller
                    # would misreport; refuse the lease rather than hold it
                    # silently.
                    _unlock_fd(handle.fd)
                    os.close(handle.fd)
                    raise
            lease = cls(path, handle.fd)
            _HELD_LEASES[key] = lease
            return lease

    @classmethod
    def is_held_elsewhere(cls, path: Path) -> bool:
        """Return whether another holder owns the lease at *path* right now.

        Used by the tombstone sweep: a tombstone whose lease is still held may
        still be written by a stuck worker in another process and must be left
        alone. A missing lock file means no holder. The probe records nothing,
        so it never displaces the current holder's record.
        """
        if not path.is_file():
            return False
        try:
            probe = cls.try_acquire(path, record_holder=False)
        except SecureFileError, OSError:
            # Cannot even open the lock file: fail closed, treat it as held.
            return True
        if probe is None:
            return True
        probe.release()
        return False

    @classmethod
    def relocate(cls, old_path: Path, new_path: Path) -> None:
        """Re-key a held lease whose lock file moved with its directory.

        The lock lives on the inode, so the holder does not change — only the
        name the in-process registry answers under. Without this a session
        recreated at the old path is refused a writer that nothing holds.
        """
        old_key = _lease_key(old_path)
        with _HELD_GUARD:
            lease = _HELD_LEASES.pop(old_key, None)
            if lease is None or lease._released:
                # A released lease holds nothing; re-keying one would leave the
                # registry answering "held" under the new name forever.
                return
            lease._path = new_path
            _HELD_LEASES[_lease_key(new_path)] = lease

    def release(self) -> None:
        """Release the lease (idempotent).

        Nothing is written here: the record stays as the last holder left it,
        and the directory it lives in may already be gone.
        """
        # Whole body under the registry guard: the key to remove is derived
        # from ``_path``, and ``relocate`` rewrites that field. Reading it
        # outside lets a relocation land in between, so the removal looks for
        # the old name while the registry now holds this lease under the new
        # one — leaving a phantom holder that refuses every later writer.
        with _HELD_GUARD:
            if self._released:
                return
            self._released = True
            key = _lease_key(self._path)
            if _HELD_LEASES.get(key) is self:
                del _HELD_LEASES[key]
            _unlock_fd(self._fd)
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = -1
