# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The registry of a session's background commands and the logs they write."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from chrys.foundation.models.background_tasks import BackgroundTaskView
from chrys.foundation.platform.output_capture import CapturedOutput
from chrys.foundation.trajectory.event_types import EventType
from chrys.service.tools.background import MAIN_SCOPE, BackgroundTask, BackgroundTasks, TaskScope, stop_command
from chrys.service.tools.background import crash as crash_mod
from chrys.service.tools.background import log as log_mod
from chrys.service.tools.background import registry as registry_mod
from chrys.service.tools.background.log import TaskLog
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.background_tasks import HeldProcess
from tests.support.waiting import wait_for


@pytest.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[BackgroundTasks]:
    registry = BackgroundTasks(tmp_path)
    yield registry
    await registry.close()


def _log(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def test_adopted_command_logs_its_output_and_exit_code(tasks: BackgroundTasks) -> None:
    process = HeldProcess()
    task = tasks.adopt(process, command="make serve", trigger="timeout")
    assert task is not None and process.sink is not None
    assert tasks.running == [task]

    process.sink(0, b"line one\n")
    process.sink(0, b"\x1b[31mred\x1b[0m\n")
    process.exit(3)
    await wait_for(task.ended.is_set, description="the task ended")

    assert _log(task.log_path) == "before\nline one\nred\n[icode] process exited with code 3\n"
    assert task.exit_code == 3
    assert task.stopped_by is None
    assert process.closed
    assert tasks.running == []


async def test_stop_file_stops_the_task_and_is_removed(tasks: BackgroundTasks) -> None:
    process = HeldProcess()
    task = tasks.adopt(process, command="npm run dev", trigger="timeout")
    assert task is not None
    assert await tasks.stop_requested() == []

    task.stop_path.touch()
    assert await tasks.stop_requested() == [task]

    assert task.ended.is_set()
    assert process.signals == [False]
    assert _log(task.log_path).endswith("[icode] process stopped by agent\n")
    assert task.exit_code is None
    await wait_for(lambda: not task.stop_path.exists(), description="the stop file was removed")


async def test_stop_kills_a_command_that_ignores_it_then_stops_reading(
    tasks: BackgroundTasks, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_mod, "_TERM_GRACE", 0.01)
    monkeypatch.setattr(registry_mod, "_KILL_GRACE", 0.01)
    process = HeldProcess(ignores=(False, True))
    process.alive = (4243,)  # Another user's, say.
    task = tasks.adopt(process, command="stubborn", trigger="timeout")
    assert task is not None

    await tasks.stop(task, "user")

    assert process.signals == [False, True]
    assert process.closed
    assert _log(task.log_path).endswith("[icode] process stopped by user, but could not stop pid 4243\n")
    assert task.outcome == "stopped by user, but could not stop pid 4243"


async def test_close_stops_every_task_and_takes_no_more(tmp_path: Path) -> None:
    tasks = BackgroundTasks(tmp_path)
    first = tasks.adopt(HeldProcess(), command="one", trigger="timeout")
    second = tasks.adopt(HeldProcess(), command="two", trigger="timeout")
    assert first is not None and second is not None

    await tasks.close()

    for task in (first, second):
        assert task.ended.is_set()
        assert _log(task.log_path).endswith("[icode] process stopped at session end\n")
    assert tasks.adopt(HeldProcess(), command="three", trigger="timeout") is None


async def test_a_full_session_takes_no_more_tasks(tasks: BackgroundTasks) -> None:
    for index in range(registry_mod.MAX_RUNNING_TASKS):
        assert tasks.adopt(HeldProcess(), command=f"task {index}", trigger="timeout") is not None
    assert tasks.full

    process = HeldProcess()
    assert tasks.adopt(process, command="one more", trigger="timeout") is None
    assert process.sink is None


async def test_task_ids_never_repeat_a_used_id_or_an_existing_file(
    tasks: BackgroundTasks, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks.directory.mkdir(parents=True)
    (tasks.directory / "bg-aaaaaa.stop").touch()
    tokens = iter(["aaaaaa", "bbbbbb", "bbbbbb", "cccccc"])
    monkeypatch.setattr(registry_mod, "secrets", SimpleNamespace(token_hex=lambda _n: next(tokens)))

    first = tasks.adopt(HeldProcess(), command="one", trigger="timeout")
    second = tasks.adopt(HeldProcess(), command="two", trigger="timeout")

    assert first is not None and first.id == "bg-bbbbbb"
    assert second is not None and second.id == "bg-cccccc"


async def test_only_the_newest_finished_logs_are_kept(tasks: BackgroundTasks, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry_mod, "_KEPT_FINISHED_LOGS", 2)
    # Exercise a live recovery record on POSIX without consulting the process table.
    monkeypatch.setattr(crash_mod, "_members", lambda session: ((session, 1.0),))
    tasks.directory.mkdir(parents=True)
    old = time.time() - 100
    for index, name in enumerate(("bg-old001", "bg-old002", "bg-old003")):
        for suffix in (".log", ".log.1"):
            path = tasks.directory / f"{name}{suffix}"
            path.touch()
            os.utime(path, (old + index, old + index))
    running = tasks.adopt(HeldProcess(), command="still running", trigger="timeout")
    process = HeldProcess()
    finished = tasks.adopt(process, command="done", trigger="timeout")
    assert running is not None and finished is not None
    await wait_for(running.log_path.exists, description="the running task's log was written")

    process.exit(0)

    kept = {f"{finished.id}.log", f"{running.id}.log", "bg-old003.log", "bg-old003.log.1"}
    await wait_for(
        lambda: {path.name for path in tasks.directory.glob("*.log*")} == kept, description="old logs pruned"
    )


@pytest.mark.parametrize(
    ("shell_name", "path", "expected"),
    [
        ("zsh", PurePosixPath("/tmp/it's/bg-1.stop"), "touch '/tmp/it'\"'\"'s/bg-1.stop'"),
        ("git_bash", PureWindowsPath(r"C:\it's\bg-1.stop"), "touch 'C:/it'\"'\"'s/bg-1.stop'"),
        (
            "pwsh",
            PureWindowsPath(r"C:\it's\bg-1.stop"),
            r"New-Item -ItemType File -Force -Path 'C:\it''s\bg-1.stop' | Out-Null",
        ),
        ("cmd", PureWindowsPath(r"C:\it's\bg-1.stop"), 'type nul > "C:\\it\'s\\bg-1.stop"'),
    ],
)
def test_stop_command_quotes_the_path_for_each_shell(shell_name: str, path: PurePath, expected: str) -> None:
    assert stop_command(shell_name, path) == expected


async def test_only_the_exact_stop_command_of_a_task_counts(tasks: BackgroundTasks) -> None:
    task = tasks.adopt(HeldProcess(), command="serve", trigger="timeout")
    ended_process = HeldProcess()
    ended = tasks.adopt(ended_process, command="build", trigger="timeout")
    assert task is not None and ended is not None
    ended_process.exit(0)
    await wait_for(ended.ended.is_set, description="the build ended")
    command = stop_command("zsh", task.stop_path)

    assert tasks.is_stop_command(f"  {command}\n", "zsh")
    # The model may not have seen yet that the task ended on its own.
    assert tasks.is_stop_command(stop_command("zsh", ended.stop_path), "zsh")
    assert not tasks.is_stop_command(f"{command} && rm -rf build", "zsh")
    assert not tasks.is_stop_command(command, "pwsh")
    assert not tasks.is_stop_command(stop_command("zsh", tasks.directory / "bg-other.stop"), "zsh")


# ───────────────────────── agents, reminder, trajectory ─────────────────────────

_SUB_AGENT = TaskScope("sub_agent", "invocation-1")


async def _ended(task: BackgroundTask) -> None:
    await wait_for(task.ended.is_set, description=f"{task.id} ended")


async def test_each_agent_hears_once_of_its_own_ended_tasks_but_not_of_its_own_stops(tasks: BackgroundTasks) -> None:
    built, child_process = HeldProcess(), HeldProcess()
    build = tasks.adopt(built, command="make", trigger="timeout")
    served = tasks.adopt(HeldProcess(), command="serve", trigger="timeout")
    child = tasks.adopt(child_process, command="pytest", trigger="timeout", scope=_SUB_AGENT)
    assert build is not None and served is not None and child is not None
    built.exit(0)
    child_process.exit(1)
    served.stop_path.touch()
    await tasks.stop_requested()
    await _ended(build)
    await _ended(child)

    assert tasks.unreported(MAIN_SCOPE) == [build]
    assert tasks.unreported(_SUB_AGENT) == [child]
    build.reported = build.outcome
    assert tasks.unreported(MAIN_SCOPE) == []


async def test_reminder_lists_the_main_agents_commands_and_nothing_that_changes_with_output(
    tasks: BackgroundTasks,
) -> None:
    running_process, built = HeldProcess(), HeldProcess()
    running = tasks.adopt(running_process, command="npm  run\ndev", trigger="timeout", reason="Start the dev server")
    build = tasks.adopt(built, command="make", trigger="timeout")
    assert tasks.adopt(HeldProcess(), command="pytest", trigger="timeout", scope=_SUB_AGENT) is not None
    assert running is not None and build is not None and running_process.sink is not None
    built.exit(2)
    await _ended(build)

    text = tasks.reminder("zsh")

    assert text == "\n".join(
        [
            "Background commands of this session:",
            f"- {running.id} (pid 4242), running: npm run dev",
            "  reason: Start the dev server",
            f"  log: {running.log_path}",
            f"  stop: {stop_command('zsh', running.stop_path)}",
            f"- {build.id} (pid 4242), exited with code 2: make",
            f"  log: {build.log_path}",
            (
                "To stop a running command, run its stop command in the shell: "
                "this stops every process the command started."
            ),
        ]
    )
    running_process.sink(0, b"more output\n")
    assert tasks.reminder("zsh") == text

    await tasks.stop(running, "user")
    assert tasks.reminder("zsh") == "\n".join(
        [
            "Background commands of this session:",
            f"- {build.id} (pid 4242), exited with code 2: make",
            f"  log: {build.log_path}",
            f"- {running.id} (pid 4242), stopped by user: npm run dev",
            "  reason: Start the dev server",
            f"  log: {running.log_path}",
        ]
    )


async def test_an_end_the_reminder_lists_is_still_news_for_a_sleep(tasks: BackgroundTasks) -> None:
    built = HeldProcess()
    build = tasks.adopt(built, command="make", trigger="timeout")
    assert build is not None
    built.exit(0)
    await _ended(build)
    assert tasks.unreported(MAIN_SCOPE) == [build]

    assert tasks.reminder("zsh") is not None

    assert tasks.unreported(MAIN_SCOPE) == [build]


async def test_a_user_stop_is_news_only_until_the_reminder_lists_it(tasks: BackgroundTasks) -> None:
    served = tasks.adopt(HeldProcess(), command="serve", trigger="timeout")
    assert served is not None
    assert tasks.reminder("zsh") is not None
    await tasks.stop(served, "user")
    # Stopped after this turn's list was taken: the agent has not heard of it yet.
    assert tasks.unreported(MAIN_SCOPE) == [served]

    assert tasks.reminder("zsh") is not None

    assert tasks.unreported(MAIN_SCOPE) == []

    # A command that exited as the user stopped it ended with that exit, which stays news.
    built = HeldProcess()
    build = tasks.adopt(built, command="make", trigger="timeout")
    assert build is not None
    built.exit(0)
    await _ended(build)
    await tasks.stop(build, "user")
    assert build.outcome == "exited with code 0"
    assert tasks.reminder("zsh") is not None
    assert tasks.unreported(MAIN_SCOPE) == [build]


async def test_a_task_whose_shell_exits_first_is_news_once_then_ends_with_no_exit_code(
    tasks: BackgroundTasks,
) -> None:
    process = HeldProcess()
    task = tasks.adopt(process, command="npm run build && npm start", trigger="timeout")
    assert task is not None

    process.outlive(0, tuple(range(5001, 5007)))
    await wait_for(lambda: task.outcome != "running", description="its shell exited")

    assert task.outcome == "running after its shell exited with code 0"
    assert task.processes == "pids 5001, 5002, 5003, 5004, 5005, …"
    assert tasks.unreported(MAIN_SCOPE) == [task]
    task.reported = task.outcome  # As a sleep that told it does.
    assert tasks.unreported(MAIN_SCOPE) == []

    process.exit(0)
    await _ended(task)

    # What the processes that outlived the shell ended with is not known.
    assert task.exit_code is None
    assert task.outcome == "ended (its shell had exited with code 0)"
    assert tasks.unreported(MAIN_SCOPE) == [task]
    assert _log(task.log_path) == (
        "before\n"
        "[icode] shell exited with code 0; other processes still running\n"
        "[icode] process ended (its shell had exited with code 0)\n"
    )


async def test_a_command_adopted_once_its_shell_exited_is_no_news_until_it_ends(tasks: BackgroundTasks) -> None:
    process = HeldProcess()
    process.outlive(3, (5001,))
    # Its tool call already said that the shell exited.
    task = tasks.adopt(process, command="serve &", trigger="shell_exited", outlived=(5001,))
    assert task is not None

    assert (task.shell_exit_code, task.processes) == (3, "pid 5001")
    assert tasks.unreported(MAIN_SCOPE) == []
    reminder = tasks.reminder("zsh")
    assert reminder is not None
    assert f"- {task.id} (pid 5001), running after its shell exited with code 3: serve &" in reminder

    process.exit(0)
    await _ended(task)
    assert tasks.unreported(MAIN_SCOPE) == [task]


async def test_reminder_is_none_while_the_main_agent_never_had_a_task(tasks: BackgroundTasks) -> None:
    assert tasks.reminder("zsh") is None
    assert tasks.adopt(HeldProcess(), command="pytest", trigger="timeout", scope=_SUB_AGENT) is not None
    assert tasks.reminder("zsh") is None


async def test_a_finished_agent_stops_only_its_own_tasks(tasks: BackgroundTasks) -> None:
    child = tasks.adopt(HeldProcess(), command="pytest", trigger="timeout", scope=_SUB_AGENT)
    main = tasks.adopt(HeldProcess(), command="serve", trigger="timeout")
    assert child is not None and main is not None

    stopped = tasks.stop_scope(_SUB_AGENT)
    await tasks.wait_stopped(stopped)

    assert stopped == [child]
    assert child.outcome == "stopped when its agent finished"
    assert tasks.running == [main]


async def test_rollback_stops_the_tasks_of_the_turns_it_removes_and_forgets_them(tmp_path: Path) -> None:
    turn = 1
    tasks = BackgroundTasks(tmp_path, turn=lambda: turn)
    try:
        kept = tasks.adopt(HeldProcess(), command="serve", trigger="timeout")
        turn = 2
        removed = tasks.adopt(HeldProcess(), command="watch", trigger="timeout")
        built = HeldProcess()
        finished = tasks.adopt(built, command="build", trigger="timeout")
        assert kept is not None and removed is not None and finished is not None
        built.exit(0)
        await finished.ended.wait()

        await tasks.stop_turns_after(1)

        assert removed.ended.is_set()
        assert _log(removed.log_path).endswith("[icode] process stopped by rollback\n")
        assert tasks.running == [kept]
        reminder = tasks.reminder("zsh") or ""
        assert removed.id not in reminder
        assert finished.id not in reminder
        assert tasks.unreported(MAIN_SCOPE) == []
    finally:
        await tasks.close()


async def test_each_change_of_a_task_is_told_with_the_main_agents_tasks_as_they_are_then(tmp_path: Path) -> None:
    turn = 1
    told: list[tuple[tuple[BackgroundTaskView, ...], bool]] = []

    async def on_change(registry: BackgroundTasks) -> None:
        told.append((registry.views(), registry.full))

    tasks = BackgroundTasks(tmp_path, turn=lambda: turn, on_change=on_change)
    try:
        server = HeldProcess()
        main = tasks.adopt(server, command="npm run dev", reason="serve", cwd="/repo", trigger="user")
        assert main is not None
        await wait_for(lambda: len(told) == 1, description="the start was told")
        (view,), full = told[-1]
        assert not full and view.running
        assert (view.id, view.command, view.reason, view.cwd, view.trigger, view.turn) == (
            main.id,
            "npm run dev",
            "serve",
            "/repo",
            "user",
            1,
        )
        assert (view.pids, view.log_path) == ((4242,), str(main.log_path))

        child = tasks.adopt(HeldProcess(), command="pytest", trigger="timeout", scope=_SUB_AGENT)
        assert child is not None
        await wait_for(lambda: len(told) == 2, description="the sub-agent's start was told")
        assert [view.id for view in told[-1][0]] == [main.id]

        server.outlive(0, (7, 8))
        await wait_for(lambda: len(told) == 3, description="the shell's exit was told")
        (view,), _ = told[-1]
        assert view.running and (view.shell_exit_code, view.pids) == (0, (7, 8))

        turn = 2
        built = HeldProcess()
        build = tasks.adopt(built, command="make", trigger="timeout")
        assert build is not None
        built.exit(2)
        await build.ended.wait()
        await wait_for(lambda: told[-1][0][-1].ended_at is not None, description="the end was told")
        assert told[-1][0][-1].exit_code == 2

        count = len(told)
        await tasks.stop_turns_after(1)
        assert len(told) == count + 1
        assert [view.id for view in told[-1][0]] == [main.id]

        # A stop is told as it starts, then once the task ended.
        count = len(told)
        await tasks.stop(main, "user")
        (stopping,), _ = told[count]
        assert stopping.running and stopping.stopped_by == "user"
        assert told[-1][0][0].ended_at is not None
    finally:
        await tasks.close()


async def test_start_and_end_of_a_task_are_recorded_on_the_trajectory(tmp_path: Path) -> None:
    sink = FakeSink()
    context = make_context(sink)
    tasks = BackgroundTasks(tmp_path, turn=lambda: 3, trajectory=lambda: context)
    try:
        process = HeldProcess()
        task = tasks.adopt(process, command="make", trigger="timeout", scope=_SUB_AGENT)
        assert task is not None
        process.exit(0)
        await _ended(task)
    finally:
        await tasks.close()

    started = sink.only(EventType.BACKGROUND_TASK_STARTED).payload
    finished = sink.only(EventType.BACKGROUND_TASK_FINISHED).payload
    assert started == {"task": task.id, "pid": 4242, "trigger": "timeout", "agent": "sub_agent", "turn": 3}
    assert finished == {"task": task.id, "exit_code": 0, "duration_ms": finished["duration_ms"]}
    assert finished["duration_ms"] >= 0


# ───────────────────────── task log ─────────────────────────


async def _written(log: TaskLog, text: str) -> None:
    await wait_for(lambda: log.path.exists() and _log(log.path) == text, description=f"log reads {text!r}")


async def test_log_merges_streams_line_by_line(tmp_path: Path) -> None:
    log = TaskLog(tmp_path / "bg-1.log")
    log.start(())
    log.feed(0, b"out1\nou")
    log.feed(1, b"err1\n")
    log.feed(0, b"t2\n")
    await log.close("[icode] process exited with code 0")

    assert _log(log.path) == "out1\nerr1\nout2\n[icode] process exited with code 0\n"


async def test_log_keeps_a_character_split_between_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_mod, "_FLUSH_DELAY", 0)
    character = "你".encode()
    log = TaskLog(tmp_path / "bg-1.log")
    log.start(())
    log.feed(0, b"a\n" + character[:2])
    await _written(log, "a\n")
    log.feed(0, character[2:] + b"\r\n")
    await log.close("[icode] process exited with code 0")

    assert _log(log.path) == "a\n你\n[icode] process exited with code 0\n"


async def test_log_counts_output_it_could_not_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_mod, "_QUEUE_LIMIT", 10)
    log = TaskLog(tmp_path / "bg-1.log")
    log.start(())
    log.feed(0, b"kept\n")
    log.feed(0, b"x" * 20)
    log.feed(0, b"tail\n")
    await log.close("[icode] process stopped by agent")

    assert _log(log.path) == "kept\ntail\n[... 20 bytes of output dropped ...]\n[icode] process stopped by agent\n"


async def test_log_turns_utf16_output_into_utf8_lines(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_mod, "subprocess_output_utf16_codec", lambda _raw: "utf-16-le")
    data = "结果: 成功\r\n第二行\r\n".encode("utf-16-le")
    log = TaskLog(tmp_path / "bg-1.log")
    log.start(())
    # Split inside a code unit: each half alone is no valid UTF-16.
    log.feed(0, data[:7])
    log.feed(0, data[7:])
    await log.close("[icode] process exited with code 0")

    assert _log(log.path) == "结果: 成功\n第二行\n[icode] process exited with code 0\n"


async def test_log_rotates_past_its_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(log_mod, "LOG_LIMIT_BYTES", 60)
    log = TaskLog(tmp_path / "bg-1.log")
    log.start((CapturedOutput(b"x" * 30 + b"\n", b"", 31),))
    await _written(log, "x" * 30 + "\n")
    log.feed(0, b"after rotation\n")
    await log.close("[icode] process exited with code 0")

    assert _log(tmp_path / "bg-1.log.1") == "x" * 30 + "\n"
    assert _log(log.path) == "after rotation\n[icode] process exited with code 0\n"


async def test_log_splits_captured_output_longer_than_its_limit_at_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(log_mod, "LOG_LIMIT_BYTES", 80)
    line = "x" * 29 + "\n"
    log = TaskLog(tmp_path / "bg-1.log")
    log.start((CapturedOutput((line * 3).encode(), b"", 90),))
    await log.close("[icode] process exited with code 0")

    assert _log(tmp_path / "bg-1.log.1") == line * 2
    assert _log(log.path) == line + "[icode] process exited with code 0\n"


async def test_log_goes_on_with_the_line_and_character_the_move_split(tmp_path: Path) -> None:
    character = "你".encode()
    log = TaskLog(tmp_path / "bg-1.log")
    log.start((CapturedOutput(b"step 1\nstep " + character[:2], b"", 14),))
    log.feed(0, character[2:] + b" done\n")
    await log.close("[icode] process exited with code 0")

    assert _log(log.path) == "step 1\nstep 你 done\n[icode] process exited with code 0\n"
