# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Logical session deletion while a trajectory writer may still hold the file.

``/clear`` and delete remove the whole session directory. If the session's
trajectory writer is still alive — a worker stuck in ``write()`` in this
process, or a live writer in another process — the directory cannot simply be
removed: on Windows the open descriptor blocks the unlink, and on POSIX the
stuck worker would keep writing into an unlinked inode while the caller
believes the data is gone. So deletion becomes *logical* first:

1. rename the session directory into ``<sessions>/.tombstones/`` (dot-prefixed
   entries are never listed as sessions) and ``fsync`` the parent, so the
   intent is durable before the index / MRU entries are removed;
2. if the rename itself fails (Windows refuses to rename a directory with an
   open child), write a durable *intent* file naming the directory instead and
   remove whatever can be removed now;
3. the physical cleanup happens in :func:`sweep_tombstones` — at the next
   startup, or whenever a caller asks — and only for tombstones whose writer
   lease this process can take *and hold* for the removal, so a writer in
   another process is never pulled from under its feet, nor allowed to start
   while the directory is going away.

Callers never block on the stuck worker.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from chrys.foundation.platform.files import SecureFileError, secure_open_owner_only, unlink_with_retry
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.keys import ensure_owner_only_directory
from chrys.foundation.trajectory.lease import WRITER_LEASE_FILE_NAME, WriterLease

logger = logging.getLogger(__name__)

TOMBSTONES_DIR_NAME: Final = ".tombstones"
INTENT_SUFFIX: Final = ".intent"
_TRAJECTORY_DIR_NAME: Final = "trajectory"
_ERROR_SHARING_VIOLATION: Final = 32


class DeleteOutcome:
    """How :func:`delete_session_directory` disposed of the directory."""

    REMOVED = "removed"
    TOMBSTONED = "tombstoned"
    INTENT_RECORDED = "intent_recorded"
    INTENT_FAILED = "intent_failed"
    """Neither renamed nor recorded: whatever survived is nobody's to sweep."""
    PATH_REUSED = "path_reused"
    """Not fully removed, deliberately: the caller writes a session here again."""
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class DeleteResult:
    outcome: str
    tombstone_path: Path | None = None


def tombstones_dir(sessions_root: Path) -> Path:
    return sessions_root / TOMBSTONES_DIR_NAME


def session_lease_path(session_dir: Path) -> Path:
    return session_dir / _TRAJECTORY_DIR_NAME / WRITER_LEASE_FILE_NAME


def writer_lease_held(session_dir: Path) -> bool:
    """Whether a trajectory writer (any process) currently holds *session_dir*'s lease."""
    return WriterLease.is_held_elsewhere(session_lease_path(session_dir))


@contextlib.contextmanager
def _writer_lease_taken(session_dir: Path) -> Generator[bool]:
    """Hold *session_dir*'s writer lease for the body; ``False`` when somebody else has it.

    Asking whether the lease is held answers where it *was*: the probe is
    released before the caller acts, and a writer that takes it in that gap
    opens its log inside a directory about to be removed. Holding the lease
    across the removal is the same answer with the guarantee that it stays
    true — activation elsewhere is refused a writer for as long as this runs.

    A session with no lock file yet is not a session nobody can write: the
    lease is taken there too, creating the file, because the alternative is
    to look, see nothing, and be overtaken by the activation that creates it.
    Anything that stops the lease from being taken — including not being able
    to make one — reads as held: this decides whether a directory is removed,
    and "I could not find out" is not "nobody is writing".
    """
    lease_path = session_lease_path(session_dir)
    try:
        ensure_owner_only_directory(lease_path.parent)
        lease = WriterLease.try_acquire(lease_path)
    except SecureFileError, OSError:
        yield False
        return
    if lease is None:
        yield False
        return
    try:
        yield True
    finally:
        lease.release()


def _remove_holding_lease(directory: Path) -> bool:
    """Remove *directory* while holding its writer lease; ``False`` when the lease cannot be taken.

    The lease's own file is open while it goes, and Windows without POSIX
    delete semantics keeps a file deleted under an open handle in its
    directory until that handle closes: the two directories around it, once
    they hold nothing else, are removed after the release, when that file is
    gone. A file pending deletion cannot be opened, so no writer takes the
    lease while the rest goes; one that makes a new lock file after the
    release keeps those directories non-empty, and their removal fails
    instead of deleting what it writes.
    """
    lease_path = session_lease_path(directory)
    # What each directory around the lease file holds when only that file is left.
    pinned = {lease_path.parent: [lease_path.name], directory: [lease_path.parent.name]}
    deferred: list[Path] = []

    def handle_remove_error(function: Callable[..., object], path: str, error: BaseException) -> None:
        if function is os.unlink and getattr(error, "winerror", None) == _ERROR_SHARING_VIOLATION:
            # Windows readers, including the sidebar's snapshot counter, can
            # briefly hold a file without delete sharing. Retry with the lease held.
            with contextlib.suppress(FileNotFoundError):
                unlink_with_retry(path)
            return
        not_empty = isinstance(error, OSError) and error.errno == errno.ENOTEMPTY
        # Other failures still stop removal at the affected path.
        if function is not os.rmdir or not not_empty or pinned.get(Path(path)) != os.listdir(path):
            raise error
        deferred.append(Path(path))

    with _writer_lease_taken(directory) as taken:
        if not taken:
            return False
        shutil.rmtree(directory, onexc=handle_remove_error)
    # Bottom-up, as the removal reported them.
    for path in deferred:
        os.rmdir(path)
    return True


def delete_session_directory(session_dir: Path, *, sessions_root: Path, path_reused: bool = False) -> DeleteResult:
    """Delete *session_dir* physically, or logically when its writer is still alive.

    The caller holds whatever session-level locks deletion requires; this
    helper only decides between ``rmtree`` and the tombstone path.

    ``path_reused`` marks a caller that writes a session at this very path
    again (a reset restarts on the same session id). No delete intent is
    recorded for it: an intent names a directory *by name*, and the sweep that
    finishes it later would remove the session written here since.
    """
    if not session_dir.is_dir():
        return DeleteResult(DeleteOutcome.MISSING)
    if _remove_holding_lease(session_dir):
        return DeleteResult(DeleteOutcome.REMOVED)
    graveyard = tombstones_dir(sessions_root)
    ensure_owner_only_directory(graveyard)
    target = graveyard / f"{session_dir.name}-{new_analytics_id()}"
    try:
        os.rename(session_dir, target)
    except OSError:
        if path_reused:
            logger.debug("Tombstone rename failed for %s; the path is reused", session_dir, exc_info=True)
            # Best effort: remove everything the open descriptor does not pin.
            # What survives is written around, not swept.
            shutil.rmtree(session_dir, ignore_errors=True)
            return DeleteResult(DeleteOutcome.PATH_REUSED)
        logger.debug("Tombstone rename failed for %s; recording delete intent", session_dir, exc_info=True)
        recorded = _record_intent(graveyard, session_dir)
        _fsync_directory(graveyard)
        # Best effort: remove everything the open descriptor does not pin.
        shutil.rmtree(session_dir, ignore_errors=True)
        # An intent that could not be written is the one case with no owner:
        # saying "recorded" would promise a sweep that will never come.
        return DeleteResult(DeleteOutcome.INTENT_RECORDED if recorded else DeleteOutcome.INTENT_FAILED)
    _fsync_directory(graveyard)
    _fsync_directory(sessions_root)
    # The lock file moved with the directory: a writer here still holds it,
    # but under its new name — or a session recreated at the old path (a reset
    # restarts on the same id) would be refused a writer nothing holds.
    WriterLease.relocate(session_lease_path(session_dir), session_lease_path(target))
    return DeleteResult(DeleteOutcome.TOMBSTONED, tombstone_path=target)


def sweep_tombstones(sessions_root: Path) -> int:
    """Physically remove tombstoned sessions whose writer lease is free; return the count.

    Safe to call at any time: a tombstone whose lease is still held (a writer
    in another process, or a stuck worker here) is skipped and retried on the
    next sweep. Errors never propagate — the sweep is housekeeping.
    """
    graveyard = tombstones_dir(sessions_root)
    if not graveyard.is_dir():
        return 0
    removed = 0
    try:
        entries = sorted(graveyard.iterdir())
    except OSError:
        logger.debug("Unable to list trajectory tombstones under %s", graveyard, exc_info=True)
        return 0
    for entry in entries:
        try:
            if entry.name.endswith(INTENT_SUFFIX) and entry.is_file():
                removed += _sweep_intent(entry, sessions_root)
            elif entry.is_dir() and not entry.is_symlink():
                removed += _sweep_tombstone(entry)
        except OSError:
            logger.debug("Trajectory tombstone sweep skipped %s", entry, exc_info=True)
    return removed


def pending_delete_intents(sessions_root: Path) -> frozenset[str]:
    """Session directory names with a recorded but unfinished delete intent."""
    graveyard = tombstones_dir(sessions_root)
    if not graveyard.is_dir():
        return frozenset()
    names: set[str] = set()
    with contextlib.suppress(OSError):
        for entry in graveyard.iterdir():
            if entry.name.endswith(INTENT_SUFFIX) and entry.is_file():
                names.add(entry.name[: -len(INTENT_SUFFIX)])
    return frozenset(names)


# ----------------------------------------------------------------- internals


def _sweep_tombstone(tombstone: Path) -> int:
    return 1 if _remove_holding_lease(tombstone) else 0


def _sweep_intent(intent: Path, sessions_root: Path) -> int:
    name = intent.name[: -len(INTENT_SUFFIX)]
    target = sessions_root / name
    if target.is_dir() and not _remove_holding_lease(target):
        return 0
    intent.unlink()
    return 1


def _record_intent(graveyard: Path, session_dir: Path) -> bool:
    """Write the durable delete intent for *session_dir*; ``False`` if it could not be."""
    intent = graveyard / f"{session_dir.name}{INTENT_SUFFIX}"
    try:
        fd = secure_open_owner_only(intent, write=True, create=True)
    except FileExistsError:
        # An earlier attempt already recorded it; the sweep owns it either way.
        return True
    except SecureFileError, OSError:
        logger.warning("Unable to record trajectory delete intent for %s", session_dir, exc_info=True)
        return False
    try:
        os.write(fd, session_dir.name.encode("utf-8", "surrogateescape"))
        with contextlib.suppress(OSError):
            os.fsync(fd)
    except OSError:
        # The intent *is* the file: both the sweep and the pending listing
        # read the directory to remove off its name, never its body, and the
        # caller fsyncs the graveyard directory right after. A failed write
        # costs the breadcrumb inside, not the owner — reporting failure here
        # would make the caller refuse a deletion the sweep goes on to finish
        # and leave the session listed as one that survived.
        logger.warning("Trajectory delete intent for %s was recorded without its body", session_dir, exc_info=True)
    finally:
        os.close(fd)
    return True


def _fsync_directory(path: Path) -> None:
    from chrys.foundation.platform import get_platform

    if get_platform().is_windows:
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
