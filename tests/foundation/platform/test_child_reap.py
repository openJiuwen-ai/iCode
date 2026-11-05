# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for chrys.foundation.platform.child_reap — the Darwin stopped-child shim."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from unittest.mock import Mock, create_autospec

import pytest

from chrys.foundation.platform import child_reap
from chrys.foundation.platform.child_reap import install_stopped_child_reap_fix
from tests.support.stopped_child import STOPS_ITSELF

# Importing the process module is what installs the shim in the app; the
# behavioural test below depends on that having happened.
from chrys.foundation.platform import process as process_mod  # noqa: F401  isort: skip


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX job control only")
async def test_stopped_child_does_not_wedge_the_event_loop() -> None:
    """A child in stopped state must not block the loop that supervises it.

    CPython 3.14.7 reaps children with a blocking ``waitpid`` on the loop thread,
    and Darwin schedules that reap as soon as the child *stops*.  Without the
    shim the loop never runs again: the timeout below never fires, the heartbeat
    never ticks, and the stopped-process detection that would kill the child
    cannot run either.
    """
    # Its own group, so the cleanup below ends whatever the child started too.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", STOPS_ITSELF, stdin=asyncio.subprocess.DEVNULL, start_new_session=True
    )
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        assert ticks > 0, "event loop was wedged by the stopped child"
    finally:
        beat.cancel()
        with suppress(asyncio.CancelledError):
            await beat
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_install_is_a_noop_off_darwin(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(child_reap.sys, "platform", platform)

    assert install_stopped_child_reap_fix() is False


@pytest.mark.skipif(sys.platform == "win32", reason="asyncio.unix_events is POSIX-only")
def test_install_leaves_an_implementation_that_reaps_off_the_loop_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``_reap_and_notify`` means no loop-thread reap, so nothing to work around."""
    from asyncio import unix_events

    class _UnaffectedWatcher:
        def _do_waitpid(self) -> None: ...
        def _reap(self) -> None: ...

    monkeypatch.setattr(child_reap.sys, "platform", "darwin")
    monkeypatch.setattr(unix_events, "_ThreadedChildWatcher", _UnaffectedWatcher)

    assert install_stopped_child_reap_fix() is False
    assert _UnaffectedWatcher._do_waitpid is not child_reap._do_waitpid_until_exit


@pytest.mark.skipif(sys.platform == "win32", reason="asyncio.unix_events is POSIX-only")
def test_install_applies_idempotently_to_the_affected_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    from asyncio import unix_events

    class _AffectedWatcher:
        def _do_waitpid(self) -> None: ...
        def _reap(self) -> None: ...
        def _reap_and_notify(self) -> None: ...

    monkeypatch.setattr(child_reap.sys, "platform", "darwin")
    monkeypatch.setattr(unix_events, "_ThreadedChildWatcher", _AffectedWatcher)

    assert install_stopped_child_reap_fix() is True
    assert _AffectedWatcher._do_waitpid is child_reap._do_waitpid_until_exit
    assert install_stopped_child_reap_fix() is False
    assert _AffectedWatcher._do_waitpid is child_reap._do_waitpid_until_exit


@dataclass
class _Watcher:
    """Explicit upstream watcher contract for deterministic scheduling tests."""

    _threads: dict[int, object] = field(default_factory=dict)
    reaped: list[int] = field(default_factory=list)

    def _reap(self, loop: asyncio.AbstractEventLoop, pid: int) -> tuple[int, int]:
        self.reaped.append(pid)
        return pid, 7

    def _reap_and_notify(
        self,
        loop: asyncio.AbstractEventLoop,
        pid: int,
        callback: Callable[..., object],
        args: tuple[object, ...],
    ) -> None:
        pid, returncode = self._reap(loop, pid)
        callback(pid, returncode, *args)


@pytest.mark.skipif(sys.platform == "win32", reason="waitid is POSIX-only")
@pytest.mark.parametrize("exit_state", ["CLD_EXITED", "CLD_KILLED", "CLD_DUMPED"])
def test_waiter_ignores_stops_and_leaves_reaping_to_the_loop(monkeypatch: pytest.MonkeyPatch, exit_state: str) -> None:
    pid = 123
    watcher = _Watcher({pid: object()})
    loop = create_autospec(asyncio.AbstractEventLoop, instance=True)
    loop.is_closed.return_value = False
    callback = Mock(spec=lambda pid, returncode, context: None)
    states = iter([os.CLD_STOPPED, os.CLD_STOPPED, os.CLD_CONTINUED, getattr(os, exit_state)])
    pauses: list[float] = []

    def waitid(idtype: int, child_pid: int, options: int) -> os.waitid_result:
        assert (idtype, child_pid, options) == (os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        assert watcher.reaped == []
        loop.call_soon_threadsafe.assert_not_called()
        return os.waitid_result((pid, 0, 0, 0, next(states)))

    monkeypatch.setattr(child_reap.os, "waitid", waitid)
    monkeypatch.setattr(child_reap.time, "sleep", pauses.append)

    child_reap._do_waitpid_until_exit(watcher, loop, pid, callback, ("context",))

    assert pauses == [child_reap._STOPPED_CHILD_POLL_INTERVAL] * 3
    assert watcher.reaped == []
    assert watcher._threads == {}
    callback.assert_not_called()
    loop.call_soon_threadsafe.assert_called_once_with(watcher._reap_and_notify, loop, pid, callback, ("context",))
    scheduled, *args = loop.call_soon_threadsafe.call_args.args
    scheduled(*args)
    assert watcher.reaped == [pid]
    callback.assert_called_once_with(pid, 7, "context")


@pytest.mark.skipif(sys.platform == "win32", reason="waitid is POSIX-only")
@pytest.mark.parametrize("closed_before_scheduling", [True, False])
def test_waiter_reaps_when_the_loop_has_closed(monkeypatch: pytest.MonkeyPatch, closed_before_scheduling: bool) -> None:
    pid = 123
    watcher = _Watcher({pid: object()})
    loop = create_autospec(asyncio.AbstractEventLoop, instance=True)
    loop.is_closed.return_value = closed_before_scheduling
    loop.call_soon_threadsafe.side_effect = RuntimeError("Event loop is closed")
    callback = Mock(spec=lambda pid, returncode: None)

    def waitid(idtype: int, child_pid: int, options: int) -> os.waitid_result:
        return os.waitid_result((child_pid, 0, 0, 0, os.CLD_EXITED))

    monkeypatch.setattr(child_reap.os, "waitid", waitid)

    child_reap._do_waitpid_until_exit(watcher, loop, pid, callback, ())

    assert watcher.reaped == [pid]
    assert watcher._threads == {}
    callback.assert_not_called()


@pytest.mark.skipif(sys.platform != "darwin", reason="shim only applies to Darwin")
async def test_cancellation_cannot_signal_a_pid_reaped_before_loop_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause loop delivery at the real watcher boundary and attempt cancellation."""
    import signal
    from asyncio import unix_events

    if unix_events._ThreadedChildWatcher._do_waitpid is not child_reap._do_waitpid_until_exit:
        pytest.skip("This Python's watcher does not need the shim")
    loop = asyncio.get_running_loop()
    watcher = loop._watcher
    reaped: set[int] = set()
    signalled_after_reaping: list[int] = []
    original_reap = watcher._reap
    original_kill = os.kill

    def track_reap(reap_loop: asyncio.AbstractEventLoop, pid: int) -> tuple[int, int]:
        result = original_reap(reap_loop, pid)
        reaped.add(result[0])
        return result

    def checked_kill(pid: int, sig: int) -> None:
        if pid in reaped:
            # Do not actually send a signal to an unowned/recyclable PID.
            signalled_after_reaping.append(pid)
            return
        original_kill(pid, sig)

    monkeypatch.setattr(watcher, "_reap", track_reap)
    monkeypatch.setattr(os, "kill", checked_kill)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)", stdin=asyncio.subprocess.PIPE
    )
    assert proc.stdin is not None
    thread = watcher._threads[proc.pid]
    try:
        proc.stdin.write(b"x")
        proc.stdin.close()
        # Deliberately block loop notification until the waiter finishes.
        # The child exits on its input byte, so this join needs no loop work.
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert proc.returncode is None
        proc.send_signal(signal.SIGTERM)
        assert signalled_after_reaping == []
        assert proc.pid not in reaped
    finally:
        if proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=5)
        with suppress(BrokenPipeError, ConnectionResetError):
            await proc.stdin.wait_closed()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX job control only")
async def test_reap_notifies_the_loop_with_the_child_returncode() -> None:
    """The replacement still delivers ``(pid, returncode)`` the way upstream does."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "raise SystemExit(7)", stdin=asyncio.subprocess.DEVNULL
    )

    assert await asyncio.wait_for(proc.wait(), timeout=10) == 7
