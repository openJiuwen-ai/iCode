# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session writer lease: one trajectory writer per session, across processes.

The lease is an OS-level file lock on ``<session>/trajectory/writer.lock``. It
is bound to the *writer worker*, not to the "currently active session": the UI
may switch sessions while a stuck worker still holds the descriptor, and the
lease keeps following that worker until it exits (or the OS reclaims it when
the process dies — at which point the stuck thread is gone too).

Acquisition never blocks: a held lease means "this session already has a
writer somewhere", and the caller opens the session trajectory-disabled.

Next to the lock sits the *holder record* (``writer.owner``), an owner-only
JSON file naming who took the lease: its pid, process start time, host name,
system boot time and acquisition time. The record is written before the lock
is attempted and flipped to ``holding`` once the lock is in hand, so the last
record of a live holder always names a holder that announced itself first.
A caller refused the lock reads that record and may *reclaim* the lease — but
only when the record says it was held, the host and boot time are this
machine's, and the recording process is gone or its pid has been reused. The
OS itself frees the lock when a process dies; reclaiming exists for the cases
where it does not (a lock server that outlived its client, a handle inherited
by a child that the recording process no longer speaks for). Anything the
record cannot prove — no record, a torn one, another host, a probe's own
in-flight record — is never reclaimed.

Reclaiming renames the stale lock aside and locks a freshly created file, so
a lock is never truncated or rewritten under its holder; the stale inode keeps
whatever holds it and is unlinked. A forked child never keeps the lease fd:
the lock file is opened ``O_CLOEXEC`` on POSIX and not inheritable on Windows,
and every iCode subprocess is exec'd, so no unrecorded process can be writing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
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
    """Who took a writer lease, as recorded next to it.

    ``holding`` is false only for the moment between a caller writing its own
    record and taking the lock: while it stays false the record proves nothing
    and no reclaim is ever based on it.
    """

    pid: int
    start_time: float
    host: str
    boot_time: float
    holding: bool
    at: str

    def describe(self) -> str:
        since = f" (since {self.at})" if self.at else ""
        return f"pid {self.pid} started {self.start_time:.0f} on {self.host}{since}"


def read_holder(lease_path: Path) -> LeaseHolder | None:
    """The holder the last writer recorded next to *lease_path*; ``None`` when unknown.

    Only diagnostics and the reclaim decision read this: anything unreadable,
    torn or written under another schema is unknown, never "nobody".
    """
    return _read_record(owner_record_path(lease_path))


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


def _new_record(*, holding: bool) -> LeaseHolder:
    host, boot_time = _host_facts()
    from chrys.foundation.trajectory.envelope import utc_now_rfc3339

    return LeaseHolder(
        pid=os.getpid(),
        start_time=_process_start_time(),
        host=host,
        boot_time=boot_time,
        holding=holding,
        at=utc_now_rfc3339(),
    )


def _holder_is_dead(holder: LeaseHolder) -> bool:
    """Whether *holder* provably no longer exists on this machine.

    A record from another host or another boot of this host is never judged:
    this machine cannot see that pid table, and a stale-looking pid there may
    be the live writer. Within this boot, a missing pid or one whose creation
    time does not match the record (the pid was reused) is dead.
    """
    import psutil

    try:
        host, boot_time = _host_facts()
    except Exception:
        logger.debug("Unable to read host facts for a writer lease record", exc_info=True)
        return False
    if holder.host != host or abs(boot_time - holder.boot_time) > _SAME_BOOT_TOLERANCE_SECONDS:
        return False
    try:
        process = psutil.Process(holder.pid)
    except psutil.NoSuchProcess:
        return True
    except psutil.Error:
        return False
    try:
        return abs(process.create_time() - holder.start_time) > _PID_REUSE_TOLERANCE_SECONDS
    except psutil.Error:
        return False


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
            holding=bool(data["holding"]),
            at=str(data.get("at", "")),
        )
    except KeyError, TypeError, ValueError:
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
                "holding": holder.holding,
                "at": holder.at,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    )
    # ``create_parents=False``: a record rewritten while handing the lease back
    # must never resurrect a directory the tombstone sweep has just removed.
    atomic_write_owner_only_bytes(path, payload.encode("utf-8"), create_parents=False)


def _restore_record(path: Path, previous: LeaseHolder | None) -> None:
    """Put the holder's own record back after a probe overwrote it."""
    if previous is None:
        return
    try:
        _write_record(path, previous)
    except SecureFileError, OSError:
        logger.debug("Unable to restore the writer lease holder record at %s", path, exc_info=True)


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
    def try_acquire(cls, path: Path) -> WriterLease | None:
        """Acquire the lease at *path* without waiting; ``None`` when held elsewhere.

        The in-process registry is the fast path; the OS lock is the truth
        across processes. The lock file is owner-only and opened with
        delete-sharing on Windows so the session directory stays renamable.
        The parent directory must already exist; open failures other than
        contention propagate as ``SecureFileError``/``OSError``.

        A lease whose record proves its holder dead is reclaimed here, so the
        caller sees a free lease rather than one nobody holds any more. The
        record of a live holder is left in place (a failed probe restores it),
        because that record is what the caller reports as ``lease_held``'s
        holder.
        """
        key = _lease_key(path)
        with _HELD_GUARD:
            if key in _HELD_LEASES:
                return None
            record_path = owner_record_path(path)
            previous = _read_record(record_path)
            _write_record(record_path, _new_record(holding=False))
            try:
                handle = secure_open_owner_only_append(path)
            except BaseException:
                _restore_record(record_path, previous)
                raise
            if _try_lock_fd(handle.fd):
                try:
                    _write_record(record_path, _new_record(holding=True))
                except BaseException:
                    _unlock_fd(handle.fd)
                    os.close(handle.fd)
                    raise
                lease = cls(path, handle.fd)
                _HELD_LEASES[key] = lease
                return lease
            os.close(handle.fd)
            _restore_record(record_path, previous)
            if previous is None or not previous.holding or not _holder_is_dead(previous):
                return None
            return cls._reclaim(path, key)

    @classmethod
    def _reclaim(cls, path: Path, key: str) -> WriterLease | None:
        """Take the lease a dead holder left behind; ``None`` when that cannot be done safely.

        Renaming the stale lock aside is the race breaker: exactly one caller
        can move that name, so two reclaimers never end up holding two fresh
        lock files. The stale inode keeps whatever still holds it and is
        unlinked; nothing is written into it, so its holder's byte-range lock
        is never disturbed.
        """
        stale = path.with_name(f"{path.name}.stale-{os.getpid()}-{time.monotonic_ns()}")
        try:
            os.rename(path, stale)
        except FileNotFoundError:
            # Somebody reclaimed between our failed lock and this rename.
            return None
        except OSError:
            logger.debug("Unable to set a stale writer lease aside at %s", path, exc_info=True)
            return None
        with contextlib.suppress(OSError):
            os.unlink(stale)
        try:
            handle = secure_open_owner_only_append(path)
        except SecureFileError, OSError:
            logger.debug("Unable to create a reclaimed writer lease at %s", path, exc_info=True)
            return None
        if not _try_lock_fd(handle.fd):
            os.close(handle.fd)
            return None
        try:
            _write_record(owner_record_path(path), _new_record(holding=True))
        except BaseException:
            _unlock_fd(handle.fd)
            os.close(handle.fd)
            raise
        lease = cls(path, handle.fd)
        _HELD_LEASES[key] = lease
        logger.info("Reclaimed a stale trajectory writer lease at %s", path)
        return lease

    @classmethod
    def is_held_elsewhere(cls, path: Path) -> bool:
        """Return whether another holder owns the lease at *path* right now.

        Used by the tombstone sweep: a tombstone whose lease is still held may
        still be written by a stuck worker in another process and must be left
        alone. A missing lock file means no holder. A lease whose record proves
        its holder dead is reclaimed on the way, so this answers "someone is
        writing *now*", not "someone once wrote".
        """
        if not path.is_file():
            return False
        try:
            probe = cls.try_acquire(path)
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

        Nothing is written here: the holder record stays as the last holder
        left it, and the directory it lives in may already be gone.
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
