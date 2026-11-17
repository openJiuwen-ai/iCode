# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real child-process hangs must retire before the next clipboard write."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from chrys.app.tui.support import clipboard_process, clipboard_writer
from chrys.app.tui.support.clipboard_writer import ClipboardWriter
from chrys.foundation.platform.process import (
    ManagedStdioProcess,
    spawn_managed_stdio_process,
    windows_hidden_subprocess_kwargs,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_WORKER = """
import json, sys, time
from pathlib import Path
started, output = map(Path, sys.argv[1:])
sys.stdout.buffer.write(b'R')
sys.stdout.buffer.flush()
text = json.loads(sys.stdin.buffer.read().decode('utf-8'))
if text == 'hang':
    started.write_text(str(__import__('os').getpid()), encoding='utf-8')
    while True:
        time.sleep(1)
output.write_text(text, encoding='utf-8')
"""

# Exercise the real argv, helper import, ready handshake and process cleanup.
# An empty wire payload makes the helper reject the request before any Win32
# call, so this test never changes the machine's clipboard on Windows.
_EMPTY_REQUEST_PARENT = """
import asyncio
from types import SimpleNamespace
from chrys.app.tui.support import clipboard_process
clipboard_process.json = SimpleNamespace(dumps=lambda text, ensure_ascii: '')
async def check():
    try:
        await clipboard_process._copy('must never reach the clipboard')
    except OSError as error:
        assert str(error) == 'Clipboard worker exited with status 1', str(error)
    else:
        raise AssertionError('Empty request must be rejected')
asyncio.run(check())
print('worker ready; empty request rejected')
"""


@pytest.fixture(autouse=True)
def _reset_failure_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(clipboard_process, "_unconfirmed_exit", False)
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(tmp_path))


@pytest.mark.parametrize("parent_flag", ["-I", "-E"])
@pytest.mark.parametrize("override", ["PYTHONHOME", "PYTHONPATH"])
def test_worker_preserves_parent_environment_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_flag: str, override: str
) -> None:
    foreign_python = tmp_path / "foreign-python"
    foreign_python.mkdir()
    (foreign_python / "chrys.py").write_text("raise RuntimeError('foreign chrys imported')\n", encoding="utf-8")
    monkeypatch.setenv(override, str(foreign_python))
    result = subprocess.run(
        [sys.executable, parent_flag, "-c", _EMPTY_REQUEST_PARENT],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=ENGINE_TURN_TIMEOUT,
        **windows_hidden_subprocess_kwargs(),
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.strip() == b"worker ready; empty request rejected"
    assert result.stderr == b""


@pytest.mark.parametrize("parent_flag", ["-P", "-s"])
def test_worker_preserves_user_site_policy_and_explicit_module_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_flag: str
) -> None:
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "user-base"))
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    site_probe = subprocess.run(
        [sys._base_executable, "-P", "-c", "import site; print(site.getusersitepackages())"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=ENGINE_TURN_TIMEOUT,
        **windows_hidden_subprocess_kwargs(),
    )
    user_site = Path(os.fsdecode(site_probe.stdout).strip())
    user_site.mkdir(parents=True, exist_ok=True)
    # The base interpreter needs both the editable package and its dependencies.
    source_path = str(Path(clipboard_process.__file__).parents[4])
    dependency_path = str(Path(psutil.__file__).parents[1])
    if parent_flag == "-s":
        # An explicitly excluded user site must stay excluded in the helper.
        (user_site / "usercustomize.py").write_text(
            "raise SystemExit('user site unexpectedly enabled')\n", encoding="utf-8"
        )
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join((source_path, dependency_path)))
    else:
        # Ordinary user-site installs must remain usable by both processes.
        (user_site / "icode.pth").write_text(source_path + "\n" + dependency_path + "\n", encoding="utf-8")
    result = subprocess.run(
        [sys._base_executable, parent_flag, "-c", _EMPTY_REQUEST_PARENT],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=ENGINE_TURN_TIMEOUT,
        **windows_hidden_subprocess_kwargs(),
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.strip() == b"worker ready; empty request rejected"
    assert result.stderr == b""


@pytest.mark.parametrize("parent_flag", ["-P", "-s"])
@pytest.mark.parametrize("path_entry", ["src", ".", ""])
@pytest.mark.parametrize("change_directory", [False, True])
def test_worker_preserves_relative_module_paths_after_workdir_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_flag: str,
    path_entry: str,
    change_directory: bool,
) -> None:
    source_path = Path(clipboard_process.__file__).parents[4]
    dependency_path = str(Path(psutil.__file__).parents[1])
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setenv("PYTHONUSERBASE", str(tmp_path / "user-base"))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((path_entry, dependency_path)))
    # Import the real adapter before changing the workspace, just as the TUI
    # does for -C. The helper must retain the interpreter's resolved paths.
    setup = "import os\nfrom chrys.app.tui.support import clipboard_process\n"
    if change_directory:
        (tmp_path / "json.py").write_text("raise RuntimeError('workspace module imported')\n", encoding="utf-8")
        setup += f"os.chdir({str(tmp_path)!r})\n"
    result = subprocess.run(
        [sys._base_executable, parent_flag, "-c", setup + _EMPTY_REQUEST_PARENT],
        cwd=source_path.parent if path_entry == "src" else source_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=ENGINE_TURN_TIMEOUT,
        **windows_hidden_subprocess_kwargs(),
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.strip() == b"worker ready; empty request rejected"
    assert result.stderr == b""


def _install_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str = _WORKER):
    processes: list[ManagedStdioProcess] = []
    children: list[psutil.Process] = []
    started, output = tmp_path / "started", tmp_path / "output"

    async def spawn(command: str, *args: str, cwd: str, env: dict[str, str], limit: int) -> ManagedStdioProcess:
        assert command == sys.executable
        assert args[-2:] == ("-m", "chrys.foundation.platform.clipboard_worker")
        if processes:
            # Observe the safety boundary before another child is even spawned.
            assert processes[-1].returncode is not None
            assert not children[-1].is_running()
        process = await spawn_managed_stdio_process(
            command, *args[:-2], "-c", script, str(started), str(output), cwd=cwd, env=env, limit=limit
        )
        children.append(psutil.Process(process.pid))
        processes.append(process)
        return process

    monkeypatch.setattr(clipboard_process, "spawn_managed_stdio_process", spawn)
    return processes, started, output


@pytest.mark.parametrize("restart", [False, True])
async def test_hung_child_is_reaped_before_latest_copy_or_restarted_app_can_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart: bool
) -> None:
    processes, started, output = _install_worker(monkeypatch, tmp_path)
    failures: list[int] = []
    old = ClipboardWriter(clipboard_process.copy_windows_text, on_failure=failures.append)
    newest = old
    try:
        assert old.submit("hang")
        await wait_for(lambda: started.exists() or not old.busy, timeout=ENGINE_TURN_TIMEOUT)
        assert started.exists()
        if restart:
            monkeypatch.setattr(clipboard_writer, "_CLOSE_TIMEOUT_SECONDS", 0)
            await old.close()
            assert old.busy
            newest = ClipboardWriter(clipboard_process.copy_windows_text)
        assert newest.submit("latest 中文😀")
        await asyncio.wait_for(newest.wait(), timeout=ENGINE_TURN_TIMEOUT)
        assert len(processes) == 2
        assert processes[0].returncode not in (None, 0)
        assert processes[1].returncode == 0
        assert output.read_text(encoding="utf-8") == "latest 中文😀"
        assert failures == ([1] if restart else [])
    finally:
        await old.close()
        if newest is not old:
            await newest.close()


async def test_stalled_stdin_reader_is_bounded_and_does_not_poison_later_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes, _, _ = _install_worker(
        monkeypatch,
        tmp_path,
        "import sys,time; sys.stdout.buffer.write(b'R'); sys.stdout.buffer.flush(); time.sleep(60)",
    )
    with pytest.raises(TimeoutError):
        await clipboard_process._copy("large" * 1024 * 1024)
    assert processes[0].returncode not in (None, 0)
    assert not clipboard_process._unconfirmed_exit
    _, _, output = _install_worker(monkeypatch, tmp_path)
    await clipboard_process._copy("recovered")
    assert output.read_text(encoding="utf-8") == "recovered"


async def test_cancellation_reaps_a_started_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processes, started, _ = _install_worker(monkeypatch, tmp_path)
    copying = asyncio.create_task(clipboard_process._copy("hang"))
    try:
        await wait_for(lambda: started.exists() or copying.done(), timeout=ENGINE_TURN_TIMEOUT)
        if copying.done():
            await copying
        assert started.exists()
        copying.cancel()
        with pytest.raises(asyncio.CancelledError):
            await copying
        assert processes[0].returncode not in (None, 0)
        assert not clipboard_process._unconfirmed_exit
    finally:
        copying.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await copying


@pytest.mark.parametrize("outcome", ["cancel", "startup_timeout", "write_timeout"])
async def test_startup_is_bounded_without_spending_the_native_write_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    processes, started, permit = _install_worker(
        monkeypatch,
        tmp_path,
        """
import sys, time
from pathlib import Path
started, permit = map(Path, sys.argv[1:])
started.write_text('starting')
while not permit.exists():
    time.sleep(0.01)
sys.stdout.buffer.write(b'R')
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
""",
    )
    # If the write deadline covers startup, the child cannot reach its marker.
    monkeypatch.setattr(clipboard_process, "_WRITE_TIMEOUT_SECONDS", 0)
    copying = asyncio.create_task(clipboard_process._copy("must not be delivered"))
    try:
        await wait_for(lambda: started.exists() or copying.done(), timeout=ENGINE_TURN_TIMEOUT)
        if copying.done():
            await copying
        assert started.exists()
        child = psutil.Process(processes[0].pid)
        assert not copying.done()
        if outcome == "cancel":
            copying.cancel()
            with pytest.raises(asyncio.CancelledError):
                await copying
        else:
            if outcome == "write_timeout":
                permit.touch()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(copying, timeout=ENGINE_TURN_TIMEOUT)
        assert processes[0].returncode is not None
        assert not child.is_running()
        assert not clipboard_process._unconfirmed_exit
    finally:
        copying.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await copying


async def test_unconfirmed_kill_fails_closed_without_leaving_later_requests_waiting(monkeypatch) -> None:
    class _Unreapable:
        def kill_tree(self) -> None:
            return

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

    monkeypatch.setattr(clipboard_process, "_STOP_TIMEOUT_SECONDS", 0)
    with pytest.raises(TimeoutError):
        await clipboard_process._stop(_Unreapable())
    assert clipboard_process._unconfirmed_exit
    # No child may be launched while the previous worker's death is uncertain.
    with pytest.raises(OSError, match="restart"):
        clipboard_process.copy_windows_text("newer")
