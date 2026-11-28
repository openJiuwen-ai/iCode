# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A launcher started by iCode exits with it instead of outliving it as an orphan."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@pytest.fixture
def death_signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    from jiuwenbox.server import launcher

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(launcher, "_set_parent_death_signal", lambda signum, pid: calls.append((signum, pid)))
    return calls


def test_a_host_pid_arms_sigterm_on_host_death(
    monkeypatch: pytest.MonkeyPatch, death_signals: list[tuple[int, int]]
) -> None:
    from jiuwenbox.server import launcher

    monkeypatch.setenv(launcher.ENV_PARENT_PID, "4242")
    launcher._exit_with_host()
    assert death_signals == [(signal.SIGTERM, 4242)]
    # The server and its sandboxes must not inherit it.
    assert launcher.ENV_PARENT_PID not in os.environ


@pytest.mark.parametrize("value", ["", "not-a-pid"])
def test_no_usable_host_pid_arms_nothing(
    monkeypatch: pytest.MonkeyPatch, death_signals: list[tuple[int, int]], value: str
) -> None:
    from jiuwenbox.server import launcher

    monkeypatch.setenv(launcher.ENV_PARENT_PID, value)
    launcher._exit_with_host()
    assert death_signals == []
    assert launcher.ENV_PARENT_PID not in os.environ


def test_the_supervisor_names_itself_as_the_launcher_host() -> None:
    from chrys.service.sandbox import supervisor

    env = supervisor._child_env("policy.yaml", "http://127.0.0.1:9", "token")
    assert env["JIUWENBOX_PARENT_PID"] == str(os.getpid())


_LAUNCHED = """
import pathlib, signal, sys
marker = pathlib.Path(sys.argv[1])
def stop(_signum, _frame):
    marker.write_text("sigterm", encoding="utf-8")
    sys.exit(0)
from jiuwenbox.server import launcher
launcher._exit_with_host()
signal.signal(signal.SIGTERM, stop)
marker.write_text("ready", encoding="utf-8")
signal.pause()
"""

_HOST = """
import os, subprocess, sys
child = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2]],
    stdin=subprocess.DEVNULL,
    env={**os.environ, "JIUWENBOX_PARENT_PID": str(os.getpid())},
)
print(child.pid, flush=True)
child.wait()
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
async def test_a_sigkilled_host_sends_its_launcher_sigterm(tmp_path: Path) -> None:
    import psutil

    marker = tmp_path / "launched.txt"
    host = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _HOST,
        _LAUNCHED,
        str(marker),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    launched: psutil.Process | None = None
    try:
        assert host.stdout is not None
        line = await asyncio.wait_for(host.stdout.readline(), ENGINE_TURN_TIMEOUT)
        launched = psutil.Process(int(line))
        await wait_for(
            lambda: marker.is_file() and marker.read_text(encoding="utf-8") == "ready",
            description="launched child armed its death signal",
            timeout=ENGINE_TURN_TIMEOUT,
        )
        host.kill()
        await wait_for(
            lambda: marker.read_text(encoding="utf-8") == "sigterm",
            description="host death reached the launched child",
            timeout=ENGINE_TURN_TIMEOUT,
        )
    finally:
        if host.returncode is None:
            host.kill()
        await host.wait()
        if launched is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                launched.kill()
