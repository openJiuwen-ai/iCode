# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A shell command moves to the session's background tasks when it outlives its timeout, goes quiet,
or leaves processes running after its shell exited; it ends with the last of its processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform.output_capture import CapturedOutput
from chrys.foundation.tool_result_metadata import (
    SHELL_BACKGROUND_TASK_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
    tool_result_metadata_failure_state,
)
from chrys.kernel.tools import SyncToolCancelledAfterCompletion
from chrys.service.tools.background import MAIN_SCOPE, BackgroundTasks, stop_command
from chrys.service.tools.background import registry as registry_mod
from chrys.service.tools.builtins import shell as shell_mod
from chrys.service.tools.builtins.shell import ShellTools, shell_result_metadata
from chrys.service.tools.builtins.shell_filter import ShellCommandFilter
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.support.background_tasks import HeldProcess
from tests.support.processes import finished_reader
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

_WAIT_FOR_RELEASE = """\
import pathlib, sys, time
print("started", flush=True)
release = pathlib.Path(sys.argv[1])
while not release.exists():
    time.sleep(0.05)
print("finished", flush=True)
"""

_SILENT_FAMILY = """\
import os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
pathlib.Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
while True:
    time.sleep(0.05)
"""

_WAIT_FOR_FILE = """\
import pathlib, sys, time
while not pathlib.Path(sys.argv[1]).exists():
    time.sleep(0.05)
"""

_LEAVE_BEHIND = """\
import pathlib, subprocess, sys, time
waiter, release, pid_file, code, *start = sys.argv[1:]
print("built", flush=True)
while start and not pathlib.Path(start[0]).exists():
    time.sleep(0.05)
# As `cmd &` with its output redirected, or nohup, leaves it: no longer writing to the command's output.
child = subprocess.Popen(
    [sys.executable, waiter, release],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(pid_file).write_text(str(child.pid), encoding="utf-8")
sys.exit(int(code))
"""

_LATE_WORKER = """\
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3); print('late', flush=True)"])
print("shell done", flush=True)
"""

_TERM_IGNORER = """\
import pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text("ready", encoding="utf-8")
time.sleep(300)
"""

_PARENT_OF_TERM_IGNORER = """\
import os, pathlib, subprocess, sys, time
ignorer, ready, pids = sys.argv[1:]
child = subprocess.Popen(
    [sys.executable, ignorer, ready],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(pids).write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
while True:
    time.sleep(0.05)
"""


@pytest.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[BackgroundTasks]:
    registry = BackgroundTasks(tmp_path / "session")
    yield registry
    await registry.close()


@pytest.fixture
def metadata() -> Iterator[dict[str, Any]]:
    values: dict[str, Any] = {}
    shell_token = shell_result_metadata.set(values)
    tool_token = tool_result_metadata.set(values)
    yield values
    tool_result_metadata.reset(tool_token)
    shell_result_metadata.reset(shell_token)


def _python_command(script: Path, *args: Path | str) -> str:
    # PowerShell runs a quoted program path only through its call operator.
    prefix = "& " if sys.platform == "win32" else ""
    return " ".join([f'{prefix}"{sys.executable}"', f'"{script}"', *(f'"{arg}"' for arg in args)])


def _lines(result: str) -> list[str]:
    return [line for line in result.splitlines() if line]


def _scripts(tmp_path: Path, **scripts: str) -> dict[str, Path]:
    paths = {name: tmp_path / f"{name}.py" for name in scripts}
    for name, text in scripts.items():
        paths[name].write_text(text, encoding="utf-8")
    return paths


async def _pid_of(pid_file: Path) -> psutil.Process:
    await wait_for(
        lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").isdigit(),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the pid written",
    )
    return psutil.Process(int(pid_file.read_text(encoding="utf-8")))


def _left_by(child: psutil.Process) -> set[int]:
    """*child* and what it started: on Windows a venv's python.exe runs the real interpreter as its child."""
    return {child.pid, *(process.pid for process in child.children(recursive=True))}


async def _family(pids: Path, call: asyncio.Task[str] | None = None) -> list[psutil.Process]:
    """The command's processes, once its script wrote their pids; *call* runs the command in the foreground."""
    await wait_for(
        lambda: pids.exists() or (call is not None and call.done()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="pids written",
    )
    if call is not None and call.done():
        pytest.fail(f"the call ended before its command started: {call.result()}")
    await wait_for(lambda: pids.read_text(encoding="utf-8").count(" ") == 1, description="both pids written")
    return [psutil.Process(int(pid)) for pid in pids.read_text(encoding="utf-8").split()]


async def _gone(processes: list[psutil.Process]) -> None:
    await wait_for(
        lambda: not any(process.is_running() for process in processes),
        timeout=ENGINE_TURN_TIMEOUT,
        description="every process of the command exited",
    )


async def _start_waiting_command(
    shell: ShellTools, tasks: BackgroundTasks, tmp_path: Path, *, timeout: int = 1
) -> tuple[str, registry_mod.BackgroundTask, Path]:
    script = tmp_path / "wait_for_release.py"
    script.write_text(_WAIT_FOR_RELEASE, encoding="utf-8")
    release = tmp_path / "release"
    result = await shell.execute(_python_command(script, release), reason="test", timeout=timeout)
    [task] = tasks.running
    return result, task, release


async def test_timed_out_command_keeps_running_and_logs_to_its_end(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)

    result, task, release = await _start_waiting_command(shell, tasks, tmp_path)

    assert "[Still running in the background: timeout of 1s reached]" in result
    assert f"Task {task.id} (pid {task.pid}) keeps running" in result
    assert str(task.log_path) in result
    assert stop_command(shell.shell.name, task.stop_path) in result
    assert "[exit_code:" not in result
    assert metadata[SHELL_BACKGROUND_TASK_METADATA_KEY] == {
        "id": task.id,
        "pid": task.pid,
        "trigger": "timeout",
        "log": str(task.log_path),
    }
    assert metadata[TOOL_FAILED_METADATA_KEY] is False
    assert SHELL_TIMED_OUT_METADATA_KEY not in metadata
    # What the tool call kept is in its result; the command may run on for hours without it.
    assert task.process.captured() == ()

    release.touch()
    await wait_for(task.ended.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the command finished")

    log = task.log_path.read_text(encoding="utf-8")
    assert log.endswith("finished\n[icode] process exited with code 0\n")
    assert log.count("started") == 1
    assert task.exit_code == 0


async def test_stop_command_stops_a_silent_command_with_every_process_it_started(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path
) -> None:
    # A filter that blocks every way of creating the file still lets the stop command through.
    blocking = ShellCommandFilter(blacklist=frozenset({"touch", "New-Item", "type"}), allow_redirections=False)
    shell = ShellTools(SessionEnvironment.capture(), command_filter=blocking, background_tasks=tasks)
    script = tmp_path / "family.py"
    script.write_text(_SILENT_FAMILY, encoding="utf-8")
    pids = tmp_path / "pids"
    await shell.execute(_python_command(script, pids), reason="test", timeout=1)
    [task] = tasks.running
    # Nothing was printed, yet the log and its directory exist for the stop command.
    assert task.log_path.exists()
    processes = await _family(pids)
    other = await shell.execute(stop_command(shell.shell.name, tmp_path / "other.stop"), reason="test")
    assert other.startswith("Error: command blocked")

    result = await shell.execute(stop_command(shell.shell.name, task.stop_path), reason="stop the task")

    assert f"[Stopped background task {task.id}]" in result
    assert result.endswith("[exit_code: 0]")
    assert task.log_path.read_text(encoding="utf-8").endswith("[icode] process stopped by agent\n")
    await _gone(processes)
    assert tasks.running == []


async def test_cancelled_call_still_kills_its_command(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = tmp_path / "family.py"
    script.write_text(_SILENT_FAMILY, encoding="utf-8")
    pids = tmp_path / "pids"
    run = asyncio.create_task(shell.execute(_python_command(script, pids), reason="test", timeout=60))
    processes = await _family(pids, run)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    await _gone(processes)
    assert tasks.running == []


async def test_full_session_kills_the_timed_out_command(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_mod, "MAX_RUNNING_TASKS", 0)
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = tmp_path / "family.py"
    script.write_text(_SILENT_FAMILY, encoding="utf-8")
    pids = tmp_path / "pids"
    run = asyncio.create_task(shell.execute(_python_command(script, pids), reason="test", timeout=3))
    processes = await _family(pids, run)

    result = await run

    assert result.startswith("Error: command timed out after 3 seconds.")
    assert "stop one first" in result
    assert metadata[SHELL_TIMED_OUT_METADATA_KEY] is True
    await _gone(processes)
    assert tasks.running == []


_READY_THEN_WAIT = """\
import pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text("ready", encoding="utf-8")
while not pathlib.Path(sys.argv[2]).exists():
    time.sleep(0.05)
print("finished", flush=True)
"""


def _asked_to_background(command: str, shell: ShellTools, asked: asyncio.Event) -> asyncio.Task[str]:
    token = shell_mod.shell_background_request.set(asked)
    try:
        return asyncio.create_task(shell.execute(command, reason="test", timeout=60))
    finally:
        shell_mod.shell_background_request.reset(token)


async def test_the_user_moves_a_running_command_to_the_background(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = tmp_path / "family.py"
    script.write_text(_SILENT_FAMILY, encoding="utf-8")
    pids = tmp_path / "pids"
    asked = asyncio.Event()
    run = _asked_to_background(_python_command(script, pids), shell, asked)
    processes = await _family(pids, run)

    asked.set()
    result = await run

    [task] = tasks.running
    assert "[Still running in the background: the user moved it there]" in result
    assert metadata[SHELL_BACKGROUND_TASK_METADATA_KEY]["trigger"] == "user"
    assert Path(task.cwd).samefile(Path.cwd())
    await tasks.stop(task, "user")
    await _gone(processes)


async def test_a_full_session_keeps_the_command_the_user_asked_to_move_in_the_foreground(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_mod, "MAX_RUNNING_TASKS", 0)
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = tmp_path / "ready.py"
    script.write_text(_READY_THEN_WAIT, encoding="utf-8")
    ready, release = tmp_path / "ready", tmp_path / "release"
    asked = asyncio.Event()
    run = _asked_to_background(_python_command(script, ready, release), shell, asked)
    await wait_for(lambda: ready.exists() or run.done(), timeout=ENGINE_TURN_TIMEOUT, description="the command started")
    assert not run.done()

    asked.set()
    await wait_for(lambda: not asked.is_set(), description="the request was turned down")
    release.touch()
    result = await run

    assert _lines(result)[-2:] == ["finished", "[exit_code: 0]"]
    assert tasks.running == []


def test_description_offers_the_background_only_with_a_registry(tmp_path: Path) -> None:
    runtime = SessionEnvironment.capture()

    def description(shell: ShellTools) -> str:
        [tool] = [tool for tool in shell.tools() if tool.name == shell.shell.name]
        return tool.description

    without = description(ShellTools(runtime))
    with_tasks = description(ShellTools(runtime, background_tasks=BackgroundTasks(tmp_path)))
    session_wide = description(
        ShellTools(runtime, background_tasks=BackgroundTasks(tmp_path), session_wide_background=True)
    )

    assert "No asynchronous task handle is provided." in without
    assert "moves to the background" not in without
    assert "No asynchronous task handle is provided." not in with_tasks
    assert "moves to the background" in with_tasks
    assert "stopped when you finish" in with_tasks
    assert "stopped when the session ends" not in with_tasks
    assert "stopped when the session ends" in session_wide
    assert "stopped when you finish" not in session_wide


async def test_processes_left_after_the_shell_exited_move_to_the_background_with_its_exit_code(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = _scripts(tmp_path, leave=_LEAVE_BEHIND, waiter=_WAIT_FOR_FILE)
    release, pid_file = tmp_path / "release", tmp_path / "pid"

    command = _python_command(script["leave"], script["waiter"], release, pid_file, "3")
    # PowerShell exits with 1 after a failed native command unless told its code.
    result = await shell.execute(
        f"{command}; exit $LASTEXITCODE" if sys.platform == "win32" else command, reason="test", timeout=60
    )

    [task] = tasks.running
    child = await _pid_of(pid_file)
    assert _lines(result)[:2] == [
        "built",
        "[The shell exited with code 3, but processes it started still run in the background]",
    ]
    assert f"Task {task.id} ({task.processes}) keeps running." in result
    assert str(task.log_path) in result
    assert result.endswith("\n[exit_code: 3]")
    assert (task.trigger, task.shell_exit_code) == ("shell_exited", 3)
    assert child.pid in task.outlived and set(task.outlived) <= _left_by(child)
    assert metadata[SHELL_BACKGROUND_TASK_METADATA_KEY]["trigger"] == "shell_exited"
    # The shell's own result still counts.
    assert tool_result_metadata_failure_state(metadata) is True
    # The tool call already said so.
    assert tasks.unreported(MAIN_SCOPE) == []

    release.touch()
    await wait_for(task.ended.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the left process ended")

    assert task.log_path.read_text(encoding="utf-8").endswith(
        "[icode] shell exited with code 3; other processes still running\n"
        "[icode] process ended (its shell had exited with code 3)\n"
    )


async def test_a_call_cancelled_while_its_output_spills_keeps_the_lines_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _cancelled_while_spilling(*_args: object, **_kwargs: object) -> str:
        raise SyncToolCancelledAfterCompletion("[output, cut]")

    monkeypatch.setattr(shell_mod, "bound_process_output", _cancelled_while_spilling)
    shell = ShellTools(SessionEnvironment.capture())

    with pytest.raises(SyncToolCancelledAfterCompletion) as cancelled:
        await shell._output_then((CapturedOutput(b"built\n", b"", 6),), 100, "[note]", "[exit_code: 0]")

    assert cancelled.value.completed_result == "[output, cut]\n[note]\n[exit_code: 0]"


@pytest.mark.parametrize("loud", [False, True], ids=["short", "spilled"])
async def test_without_a_registry_processes_left_after_the_shell_exited_are_stopped(
    loud: bool, metadata: dict[str, Any], tmp_path: Path
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), session_dir=tmp_path / "session")
    script = _scripts(tmp_path, leave=_LEAVE_BEHIND, waiter=_WAIT_FOR_FILE, loud="print('word ' * 2000)\n")
    pid_file = tmp_path / "pid"
    command = _python_command(script["leave"], script["waiter"], tmp_path / "release", pid_file, "0")
    if loud:
        command = f"{_python_command(script['loud'])}; {command}"
    call = asyncio.create_task(shell.execute(command, reason="test", timeout=60, max_output_tokens=100))
    child = await _pid_of(pid_file)

    result = await call

    # However much of the output the budget keeps, the note on the stopped processes stays.
    assert _lines(result)[-2:] == [
        "[Processes it started still ran after its shell exited, so they were stopped]",
        "[exit_code: 0]",
    ]
    assert (_lines(result)[0] == "built") is not loud
    await _gone([child])


async def test_a_process_ending_just_after_the_shell_ends_the_command_with_all_its_output(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = _scripts(tmp_path, late=_LATE_WORKER)

    result = await shell.execute(_python_command(script["late"]), reason="test", timeout=60)

    assert _lines(result) == ["shell done", "late", "[exit_code: 0]"]
    assert tasks.running == []


async def test_a_command_that_went_quiet_moves_to_the_background_before_its_timeout(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_mod, "IDLE_SECONDS", 1)
    monkeypatch.setattr(shell_mod, "_SLOW_POLL", 0.2)
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)

    started = time.monotonic()
    result, task, release = await _start_waiting_command(shell, tasks, tmp_path, timeout=50)

    assert time.monotonic() - started < 50
    assert "[Still running in the background: no output and next to no CPU use for 1s]" in result
    assert result.startswith("[partial output]\nstarted\n")
    assert task.trigger == "idle"
    release.touch()
    await wait_for(task.ended.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the command finished")
    assert task.exit_code == 0


async def test_a_command_busy_without_output_is_not_quiet(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_mod, "IDLE_SECONDS", 1)
    monkeypatch.setattr(shell_mod, "_SLOW_POLL", 0.2)
    # Even a loaded machine gives a busy process more than this of every second.
    monkeypatch.setattr(shell_mod, "_IDLE_CPU_SECONDS", 0.05)
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = _scripts(
        tmp_path,
        busy="import time\nend = time.monotonic() + 3\nwhile time.monotonic() < end:\n    pass\nprint('done')\n",
    )

    result = await shell.execute(_python_command(script["busy"]), reason="test", timeout=50)

    assert _lines(result) == ["done", "[exit_code: 0]"]
    assert tasks.running == []


class _UnreapedShell:
    """A shell that printed and exited, no process of it left, whose exit is not reaped until ``reap`` is set."""

    pid = 0

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.output = finished_reader(b"done\n")
        self.reap = asyncio.Event()

    async def wait(self) -> int:
        await self.reap.wait()
        self.returncode = 0
        return 0


async def test_a_command_with_no_process_left_is_ending_not_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    # A loaded machine reaps a command's exit, and reads the rest of its output, a while after its last
    # process is gone: a command with no process left using no CPU is ending, not quiet.
    monkeypatch.setattr(shell_mod, "IDLE_SECONDS", 0.05)
    monkeypatch.setattr(shell_mod, "_SLOW_POLL", 0.01)
    shell = _UnreapedShell()
    command = shell_mod._Command(
        shell, (shell.output,), contextlib.AsyncExitStack(), live=dict, stop=lambda _force: None
    )
    try:
        following = asyncio.create_task(command.follow(None, idle=True))
        # Twenty quiet windows: a quiet verdict would have come long before.
        assert not await wait_until(following.done, timeout=1)
        shell.reap.set()
        await wait_for(following.done, description="the command ended")

        assert following.result() == "ended"
        assert command.captured()[0].head == b"done\n"
    finally:
        await command.close()


async def test_a_background_command_whose_shell_exits_first_ends_with_its_last_process(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = _scripts(tmp_path, leave=_LEAVE_BEHIND, waiter=_WAIT_FOR_FILE)
    start, release, pid_file = tmp_path / "start", tmp_path / "release", tmp_path / "pid"
    command = _python_command(script["leave"], script["waiter"], release, pid_file, "0", start)
    await shell.execute(command, reason="test", timeout=1)
    [task] = tasks.running

    start.touch()
    child = await _pid_of(pid_file)
    await wait_for(lambda: task.shell_exit_code is not None, timeout=ENGINE_TURN_TIMEOUT, description="shell exited")

    assert task.outcome == "running after its shell exited with code 0"
    assert child.pid in task.outlived and set(task.outlived) <= _left_by(child)
    assert tasks.unreported(MAIN_SCOPE) == [task]
    release.touch()
    await wait_for(task.ended.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the left process ended")
    assert task.exit_code is None
    assert task.log_path.read_text(encoding="utf-8").endswith(
        "[icode] process ended (its shell had exited with code 0)\n"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows stops a command by killing its Job at once")
async def test_stopping_kills_a_process_that_ignores_the_request_after_its_shell_exited(
    tasks: BackgroundTasks, metadata: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_mod, "_TERM_GRACE", 0.5)
    shell = ShellTools(SessionEnvironment.capture(), background_tasks=tasks)
    script = _scripts(tmp_path, parent=_PARENT_OF_TERM_IGNORER, ignorer=_TERM_IGNORER)
    ready, pids = tmp_path / "ready", tmp_path / "pids"
    await shell.execute(_python_command(script["parent"], script["ignorer"], ready, pids), reason="test", timeout=1)
    [task] = tasks.running
    processes = await _family(pids)
    await wait_for(ready.exists, timeout=ENGINE_TURN_TIMEOUT, description="the child ignores SIGTERM")

    await tasks.stop(task, "user")

    await _gone(processes)
    assert task.log_path.read_text(encoding="utf-8").endswith("[icode] process stopped by user\n")


async def test_the_stop_note_names_what_the_stop_could_not_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model's stop waits past a stop that gives up, or the note would only say it is still stopping.
    assert registry_mod.STOP_WAIT_SECONDS > registry_mod._TERM_GRACE + registry_mod._KILL_GRACE
    monkeypatch.setattr(registry_mod, "_TERM_GRACE", 0.01)
    monkeypatch.setattr(registry_mod, "_KILL_GRACE", 0.01)
    tasks = BackgroundTasks(tmp_path)
    try:
        process = HeldProcess(ignores=(False, True))
        process.alive = (4243,)  # Root's, say, from sudo.
        task = tasks.adopt(process, command="sudo -n ./server", trigger="timeout")
        assert task is not None
        task.stop_path.touch()

        stopped = await tasks.stop_requested()

        assert shell_mod._with_stop_notes("[exit_code: 0]", stopped) == (
            f"[Stopped background task {task.id}, but could not stop pid 4243]\n[exit_code: 0]"
        )
    finally:
        await tasks.close()
