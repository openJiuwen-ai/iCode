# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from chrys.foundation.platform import get_platform
from chrys.foundation.trajectory import lease as lease_module
from chrys.foundation.trajectory.lease import (
    WRITER_LEASE_FILE_NAME,
    WriterLease,
    owner_record_path,
    read_holder,
)


def test_lease_is_exclusive_within_a_process_and_released_explicitly(tmp_path: Path) -> None:
    """Acceptance 19a ①②: a held lease refuses a second holder without blocking."""
    path = tmp_path / WRITER_LEASE_FILE_NAME
    first = WriterLease.try_acquire(path)
    assert first is not None
    assert first.held
    assert first.path == path
    started = time.monotonic()
    assert WriterLease.try_acquire(path) is None  # non-blocking refusal
    assert time.monotonic() - started < 0.5
    assert WriterLease.is_held_elsewhere(path) is True
    first.release()
    assert not first.held
    first.release()  # idempotent
    assert WriterLease.is_held_elsewhere(path) is False
    second = WriterLease.try_acquire(path)
    assert second is not None
    second.release()


def test_lease_file_is_owner_only_and_requires_existing_parent(tmp_path: Path) -> None:
    path = tmp_path / WRITER_LEASE_FILE_NAME
    lease = WriterLease.try_acquire(path)
    assert lease is not None
    try:
        if not get_platform().is_windows:
            assert path.stat().st_mode & 0o777 == 0o600
    finally:
        lease.release()
    with pytest.raises(OSError):
        WriterLease.try_acquire(tmp_path / "missing" / WRITER_LEASE_FILE_NAME)


def test_a_release_racing_a_tombstone_relocation_leaves_no_phantom_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``relocate`` rewrites the very field ``release`` derives its key from.

    A relocation that lands while the registry is unguarded re-keys the entry
    the release is about to remove, so the removal misses and the registry
    answers "held" under the tombstone name for the rest of the process.
    """
    live = tmp_path / "session"
    live.mkdir()
    path = live / WRITER_LEASE_FILE_NAME
    tombstone = tmp_path / "tombstone"
    lease = WriterLease.try_acquire(path)
    assert lease is not None
    moved_path = tombstone / WRITER_LEASE_FILE_NAME
    interleaved: list[bool] = []
    real_key = lease_module._lease_key

    def _key_racing_a_relocation(key_path: Path) -> str:
        if not interleaved:
            # A tombstone sweep only gets to interleave here if the registry
            # is unguarded at the moment the key is derived. A plain lock
            # refuses this even to the thread already holding it.
            if lease_module._HELD_GUARD.acquire(blocking=False):
                lease_module._HELD_GUARD.release()
                interleaved.append(True)  # before the work: relocate derives keys too
                live.rename(tombstone)
                WriterLease.relocate(path, moved_path)
            else:
                interleaved.append(False)
        return real_key(key_path)

    monkeypatch.setattr(lease_module, "_lease_key", _key_racing_a_relocation)
    lease.release()

    assert interleaved == [False]
    assert not lease.held
    assert lease_module._HELD_LEASES == {}
    survivor = WriterLease.try_acquire(path)
    assert survivor is not None
    survivor.release()


def test_is_held_elsewhere_is_false_for_missing_file(tmp_path: Path) -> None:
    assert WriterLease.is_held_elsewhere(tmp_path / "nope.lock") is False


def test_is_held_elsewhere_fails_closed_when_lock_file_is_unusable(tmp_path: Path) -> None:
    path = tmp_path / WRITER_LEASE_FILE_NAME
    path.write_bytes(b"")
    if get_platform().is_windows:
        pytest.skip("POSIX mode check")
    path.chmod(0o644)  # insecure mode: secure open refuses → treated as held
    assert WriterLease.is_held_elsewhere(path) is True


_HOLDER_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from chrys.foundation.trajectory.lease import WriterLease
    lease = WriterLease.try_acquire(Path(sys.argv[1]))
    assert lease is not None, "child could not acquire"
    Path(sys.argv[2]).write_text("held", encoding="utf-8")
    time.sleep(float(sys.argv[3]))
    """
)


_RAW_HOLDER_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path
    from chrys.foundation.platform import get_platform

    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
    if get_platform().is_windows:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    Path(sys.argv[2]).write_text("held", encoding="utf-8")
    time.sleep(float(sys.argv[3]))
    """
)


def _spawn(script: str, lock_path: Path, ready_path: Path, hold_seconds: float) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-c", script, str(lock_path), str(ready_path), str(hold_seconds)])
    deadline = time.monotonic() + 15.0
    while not ready_path.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            raise RuntimeError("holder subprocess never signalled readiness")
        time.sleep(0.02)
    return proc


def _spawn_holder(lock_path: Path, ready_path: Path, hold_seconds: float) -> subprocess.Popen:
    return _spawn(_HOLDER_SCRIPT, lock_path, ready_path, hold_seconds)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


def _record_for(pid: int, *, holding: bool) -> lease_module.LeaseHolder:
    host, boot_time = lease_module._host_facts()
    return lease_module.LeaseHolder(
        pid=pid,
        # Any real process creation time differs from this by far more than the
        # reuse tolerance, so a recycled pid still reads as the dead original.
        start_time=0.0,
        host=host,
        boot_time=boot_time,
        holding=holding,
        at="2026-01-01T00:00:00Z",
    )


def test_lease_blocks_across_processes_without_waiting(tmp_path: Path) -> None:
    """Acceptance 19a ②: another process sees the lease as held and is not blocked."""
    lock_path = tmp_path / WRITER_LEASE_FILE_NAME
    proc = _spawn_holder(lock_path, tmp_path / "ready", hold_seconds=2.0)
    try:
        started = time.monotonic()
        assert WriterLease.try_acquire(lock_path) is None
        assert time.monotonic() - started < 0.5
        assert WriterLease.is_held_elsewhere(lock_path) is True
        proc.wait(timeout=15)
        assert proc.returncode == 0
        lease = WriterLease.try_acquire(lock_path)
        assert lease is not None
        lease.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_a_taken_lease_records_its_holder(tmp_path: Path) -> None:
    """The lock file itself stays empty; who holds it lives in ``writer.owner``."""
    lock_path = tmp_path / WRITER_LEASE_FILE_NAME
    lease = WriterLease.try_acquire(lock_path)
    assert lease is not None
    try:
        holder = read_holder(lock_path)
        assert holder is not None
        assert (holder.pid, holder.holding) == (os.getpid(), True)
        assert holder.host == lease_module._host_facts()[0]
        assert holder.at
        if not get_platform().is_windows:
            assert owner_record_path(lock_path).stat().st_mode & 0o777 == 0o600
    finally:
        lease.release()


def test_a_live_holder_is_reported_and_not_reclaimed(tmp_path: Path) -> None:
    """A refused probe names the holder and leaves its record byte-for-byte in charge."""
    lock_path = tmp_path / WRITER_LEASE_FILE_NAME
    proc = _spawn_holder(lock_path, tmp_path / "ready", hold_seconds=30.0)
    try:
        assert WriterLease.try_acquire(lock_path) is None
        holder = read_holder(lock_path)
        assert holder is not None
        assert (holder.pid, holder.holding) == (proc.pid, True)
        assert WriterLease.is_held_elsewhere(lock_path) is True
    finally:
        _stop(proc)


def test_a_dead_holders_record_is_reclaimed(tmp_path: Path) -> None:
    """A lock still held after its recorded owner died belongs to nobody: take it."""
    lock_path = tmp_path / WRITER_LEASE_FILE_NAME
    proc = _spawn_holder(lock_path, tmp_path / "ready", hold_seconds=30.0)
    try:
        lease_module._write_record(owner_record_path(lock_path), _record_for(_dead_pid(), holding=True))
        lease = WriterLease.try_acquire(lock_path)
        assert lease is not None
        holder = read_holder(lock_path)
        assert holder is not None
        assert (holder.pid, holder.holding) == (os.getpid(), True)
        lease.release()
    finally:
        _stop(proc)


def test_a_lock_without_a_holder_record_is_never_reclaimed(tmp_path: Path) -> None:
    """A lock file whose holder never announced itself is not ours to break."""
    lock_path = tmp_path / WRITER_LEASE_FILE_NAME
    proc = _spawn(_RAW_HOLDER_SCRIPT, lock_path, tmp_path / "ready", hold_seconds=30.0)
    try:
        assert read_holder(lock_path) is None
        assert WriterLease.try_acquire(lock_path) is None
    finally:
        _stop(proc)


def test_a_record_that_never_held_the_lease_is_never_reclaimed(tmp_path: Path) -> None:
    """A probe's own in-flight record (``holding`` false) proves nobody holds it."""
    lock_path = tmp_path / WRITER_LEASE_FILE_NAME
    proc = _spawn(_RAW_HOLDER_SCRIPT, lock_path, tmp_path / "ready", hold_seconds=30.0)
    try:
        lease_module._write_record(owner_record_path(lock_path), _record_for(_dead_pid(), holding=False))
        assert WriterLease.try_acquire(lock_path) is None
    finally:
        _stop(proc)


def test_lease_is_reclaimed_by_os_when_holder_is_killed(tmp_path: Path) -> None:
    """Acceptance 19a ③: SIGKILL / TerminateProcess releases the lease."""
    lock_path = tmp_path / WRITER_LEASE_FILE_NAME
    proc = _spawn_holder(lock_path, tmp_path / "ready", hold_seconds=30.0)
    try:
        assert WriterLease.try_acquire(lock_path) is None
        if get_platform().is_windows:
            proc.kill()
        else:
            os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5.0
        lease = None
        while lease is None and time.monotonic() < deadline:
            lease = WriterLease.try_acquire(lock_path)
            if lease is None:
                time.sleep(0.05)
        assert lease is not None
        lease.release()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
