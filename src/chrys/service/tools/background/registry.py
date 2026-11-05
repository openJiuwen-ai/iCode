# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The shell commands of one session that kept running after their tool call returned.

The registry owns each command from the moment it moves to the background: it
keeps reading the output into the task's log, writes the line that ends the
log, and stops the command when asked — by the stop file the model creates,
when the agent that started it finishes, or when the session ends. A turn that
is cancelled leaves these commands alone.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import secrets
import shlex
import time
from collections import deque
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from chrys.foundation.models.background_tasks import BackgroundTaskView
from chrys.foundation.trajectory.event_types import EventType
from chrys.service.tools.background.crash import RunningRecord
from chrys.service.tools.background.log import TaskLog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable
    from pathlib import Path, PurePath

    from chrys.foundation.models.invocations import InvocationOrigin
    from chrys.foundation.platform.output_capture import CapturedOutput
    from chrys.foundation.trajectory.context import TrajectoryContext

logger = logging.getLogger(__name__)

BACKGROUND_DIR_NAME = "background"
"""The session subdirectory holding each task's log and stop file."""

MAX_RUNNING_TASKS = 32
"""Background commands a session runs at once; past it, a timed-out command is killed."""

_KEPT_FINISHED_LOGS = 20
"""Finished logs kept in the directory, and ended tasks kept in memory for the agents."""

_LISTED_ENDED = 5
"""Ended tasks the main agent's reminder still lists."""

_COMMAND_CHARS = 200

_TERM_GRACE = 3.0
"""Seconds between asking a command to stop and killing it."""

_KILL_GRACE = 2.0
"""Seconds a killed command gets to close its output before it is no longer read."""

STOP_WAIT_SECONDS = _TERM_GRACE + _KILL_GRACE + 1.0
"""How long a caller that stopped tasks waits for them to end before it goes on: past a stop that gives up."""

_STOP_FILE_POLL = 2.0

_LISTED_PIDS = 5

StopCause = Literal["agent", "user", "agent_finished", "rollback", "session_end"]

_STOPPED: dict[StopCause, str] = {
    "agent": "stopped by agent",
    "user": "stopped by user",
    "agent_finished": "stopped when its agent finished",
    "rollback": "stopped by rollback",
    "session_end": "stopped at session end",
}


@dataclass(frozen=True, slots=True)
class TaskScope:
    """The agent a task belongs to: the main agent across its turns, or one sub-agent call or workflow node."""

    kind: Literal["main", "sub_agent", "workflow_node"] = "main"
    invocation_id: str = ""
    """The sub-agent call, or the workflow node activation whose attempts all share the scope."""

    @classmethod
    def of(cls, origin: InvocationOrigin) -> TaskScope:
        if origin.kind == "side_question":
            raise ValueError("Side questions cannot own background tools")
        return cls() if origin.kind == "turn" else cls(origin.kind, origin.invocation_id)


MAIN_SCOPE = TaskScope()

current_scope: ContextVar[TaskScope] = ContextVar("background_task_scope", default=MAIN_SCOPE)
"""The agent whose tool calls run now, bound by the invocation that runs them."""


class BackgroundProcess(Protocol):
    """A running command the registry can take over."""

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int:
        """The shell's exit code, once it exited."""
        ...

    def hand_off(self, sink: Callable[[int, bytes], None]) -> tuple[CapturedOutput, ...]:
        """Send all further output to *sink*, returning what was kept until now."""
        ...

    async def shell_exited(self) -> tuple[int, ...]:
        """Wait for the shell to exit; return the processes it started that outlived it, if any."""
        ...

    async def wait(self) -> int:
        """Wait for every process of the command to end and return its shell's exit code."""
        ...

    async def terminate(self, *, force: bool) -> None:
        """Ask every process of the command to stop, or kill them all when *force* is set."""
        ...

    async def close(self) -> None:
        """Stop reading the output and release the process, killing what still runs."""
        ...

    async def still_running(self) -> tuple[int, ...]:
        """The processes of the command that still run."""
        ...


def stop_command(shell_name: str, path: PurePath) -> str:
    """A command for the shell named *shell_name* that creates the stop file at *path*."""
    if shell_name in ("pwsh", "powershell"):
        literal = str(path).replace("'", "''")
        return f"New-Item -ItemType File -Force -Path '{literal}' | Out-Null"
    if shell_name == "cmd":
        return f'type nul > "{path}"'
    if shell_name == "git_bash":
        return f"touch {shlex.quote(path.as_posix())}"
    return f"touch {shlex.quote(str(path))}"


class BackgroundTask:
    """One command running in the background, from its hand-off to its end."""

    def __init__(
        self,
        task_id: str,
        command: str,
        process: BackgroundProcess,
        log: TaskLog,
        stop_path: Path,
        *,
        reason: str,
        cwd: str,
        trigger: str,
        scope: TaskScope,
        turn: int,
    ) -> None:
        self.id = task_id
        self.command = command
        self.reason = reason
        self.cwd = cwd
        self.trigger = trigger
        self.scope = scope
        self.turn = turn
        """The main agent's turn the command started in; rollback stops those of the turns it removes."""
        self.pid = process.pid
        self.log_path = log.path
        self.stop_path = stop_path
        self.started = time.monotonic()
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        """The shell's exit code, once the command ended with it, without being stopped."""
        self.shell_exit_code: int | None = None
        """The shell's exit code, once it exited while processes it started ran on."""
        self.outlived: tuple[int, ...] = ()
        """Those processes, as the shell exited."""
        self.stopped_by: StopCause | None = None
        self.unstopped: tuple[int, ...] = ()
        """The processes iCode could not stop, still running as it gave the command up."""
        self.ended = asyncio.Event()
        """Set once the command ended and its log was closed."""
        self.reported: str | None = None
        """The outcome a sleep of its agent or its tool result last told, or a reminder told of a user stop."""
        self.process = process
        self.log = log
        self.stopping: asyncio.Task[None] | None = None

    @property
    def label(self) -> str:
        """The task's id and command, on one line."""
        return f"{self.id} ({_one_line(self.command)})"

    @property
    def pids(self) -> tuple[int, ...]:
        """The pids to tell whether it runs: the shell's, or the processes that outlived it."""
        return self.outlived or (self.pid,)

    @property
    def processes(self) -> str:
        return _pids_text(self.pids)

    @property
    def stop_note(self) -> str:
        """What a stop could not do, to follow "stopped": the processes it could not stop, if any."""
        return f", but could not stop {_pids_text(self.unstopped)}" if self.unstopped else ""

    def view(self) -> BackgroundTaskView:
        return BackgroundTaskView(
            id=self.id,
            command=self.command,
            reason=self.reason,
            cwd=self.cwd,
            trigger=self.trigger,
            turn=self.turn,
            pids=self.pids,
            log_path=str(self.log_path),
            started_at=self.started_at,
            ended_at=self.ended_at,
            exit_code=self.exit_code,
            shell_exit_code=self.shell_exit_code,
            stopped_by=self.stopped_by,
        )

    @property
    def outcome(self) -> str:
        """How the command ended, as the last line of its log says it; until then, how it runs."""
        if self.ended.is_set():
            return _ending(self)
        if self.shell_exit_code is not None:
            return f"running after its shell exited with code {self.shell_exit_code}"
        return "running"


class BackgroundTasks:
    """The background commands of one session."""

    def __init__(
        self,
        session_dir: Path,
        *,
        turn: Callable[[], int] | None = None,
        trajectory: Callable[[], TrajectoryContext | None] | None = None,
        on_change: Callable[[BackgroundTasks], Awaitable[None]] | None = None,
        surface: str = "",
    ) -> None:
        """*turn* reads the main agent's current turn; *trajectory* the session's recording scope, if any;
        *on_change* is awaited each time a task starts, outlives its shell, ends or is forgotten.
        *surface* is the frontend the session runs on, which the record a crash leaves names.
        """
        self.directory = session_dir / BACKGROUND_DIR_NAME
        self._crash_record = RunningRecord(self.directory, surface=surface)
        self._turn = turn
        self._trajectory = trajectory
        self._on_change = on_change
        self._running: dict[str, BackgroundTask] = {}
        self._ended: deque[BackgroundTask] = deque(maxlen=_KEPT_FINISHED_LOGS)
        """The newest tasks that ended; a rollback's own stops are left out, as the turns they belong to."""
        self._used_ids: set[str] = set()
        self._workers: set[asyncio.Task[None]] = set()
        """Each task's watcher and stopper; closing waits for them all."""
        self._sampler: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> list[BackgroundTask]:
        return list(self._running.values())

    @property
    def full(self) -> bool:
        return len(self._running) >= MAX_RUNNING_TASKS

    def adopt(
        self,
        process: BackgroundProcess,
        *,
        command: str,
        trigger: str,
        reason: str = "",
        cwd: str = "",
        scope: TaskScope = MAIN_SCOPE,
        outlived: tuple[int, ...] = (),
    ) -> BackgroundTask | None:
        """Take *process* over, or return None when the session takes no more or has no log for it.

        *outlived* are the processes still running after its shell exited, which
        the caller already told. Synchronous, so the output *process* kept just
        before this call is exactly what the log starts with.
        """
        if self._closed or self.full:
            return None
        task_id = self._new_id()
        log = TaskLog(self.directory / f"{task_id}.log")
        try:
            log.open()
        except OSError:
            logger.warning("Could not create the log of background task %s", task_id, exc_info=True)
            return None
        log.start(process.hand_off(log.feed))
        turn = self._turn() if self._turn is not None else 0
        task = BackgroundTask(
            task_id,
            command,
            process,
            log,
            self._stop_path(task_id),
            reason=reason,
            cwd=cwd,
            trigger=trigger,
            scope=scope,
            turn=turn,
        )
        if outlived:
            task.shell_exit_code, task.outlived = process.returncode, outlived
            task.reported = task.outcome
        self._running[task_id] = task
        self._record(
            EventType.BACKGROUND_TASK_STARTED,
            {"task": task_id, "pid": task.pid, "trigger": trigger, "agent": scope.kind, "turn": turn},
        )
        self._spawn(self._watch(task), self._workers)
        if self._sampler is None:
            self._sampler = self._spawn(self._sample())
        return task

    def is_stop_command(self, command: str, shell_name: str) -> bool:
        """Whether *command* is exactly the command that stops one of this session's tasks, ended ones too."""
        command = command.strip()
        return any(command == stop_command(shell_name, self._stop_path(task_id)) for task_id in self._used_ids)

    def unreported(self, scope: TaskScope) -> list[BackgroundTask]:
        """The tasks of *scope* that ended, or outlived their shell, since that was last told.

        A stop by the agent's own stop file is no news.
        """
        return [
            task
            for task in [*self._running.values(), *self._ended]
            if task.scope == scope
            and task.outcome != "running"
            and task.reported != task.outcome
            and task.stopped_by != "agent"
        ]

    def views(self) -> tuple[BackgroundTaskView, ...]:
        """The main agent's running commands and the few that ended last, as they are now."""
        return tuple(task.view() for task in self._listed())

    def reminder(self, shell_name: str) -> str | None:
        """The main agent's running commands and the few that ended last, or None when it never had any.

        Only what stays the same until a task changes state: no output, no times.
        Taking it tells the user's stops it lists, so they wake no sleep: the user made them, and a turn
        that never sends this list makes no sleep and lists them again. A sleep still tells any other
        end it lists, with the end of its log.
        """
        tasks = self._listed()
        if not tasks:
            return None
        lines = ["Background commands of this session:"]
        for task in tasks:
            if task.stopped_by == "user" and task.exit_code is None and task.ended.is_set():
                # Not one that exited as the user stopped it: its outcome is that exit.
                task.reported = task.outcome
            lines.append(f"- {task.id} ({task.processes}), {task.outcome}: {_one_line(task.command)}")
            if task.reason:
                lines.append(f"  reason: {_one_line(task.reason)}")
            lines.append(f"  log: {task.log_path}")
            if not task.ended.is_set():
                lines.append(f"  stop: {stop_command(shell_name, task.stop_path)}")
        if any(not task.ended.is_set() for task in tasks):
            lines.append(
                "To stop a running command, run its stop command in the shell: "
                "this stops every process the command started."
            )
        return "\n".join(lines)

    async def stop_requested(self) -> list[BackgroundTask]:
        """Stop the tasks whose stop file exists and wait a little for them to end."""
        tasks = self._apply_stop_files()
        await self.wait_stopped(tasks)
        return tasks

    def stop_scope(self, scope: TaskScope) -> list[BackgroundTask]:
        """Start stopping the running tasks of an agent that finished; return them."""
        tasks = [task for task in self._running.values() if task.scope == scope]
        for task in tasks:
            self.stop(task, "agent_finished")
        return tasks

    async def stop_turns_after(self, turn: int) -> None:
        """Stop and forget the main agent's tasks that the turns after *turn* started, as a rollback removes those turns."""
        tasks = [task for task in self._running.values() if task.scope == MAIN_SCOPE and task.turn > turn]
        for task in tasks:
            self.stop(task, "rollback")
        await self.wait_stopped(tasks)
        removed = [task for task in self._ended if task.scope == MAIN_SCOPE and task.turn > turn]
        for task in removed:
            self._ended.remove(task)
        if removed:
            await self._changed()

    async def wait_stopped(self, tasks: Iterable[BackgroundTask]) -> None:
        """Wait a little for *tasks*, being stopped, to end."""
        stopping = [task.stopping for task in tasks if task.stopping is not None]
        if stopping:
            await asyncio.wait(stopping, timeout=STOP_WAIT_SECONDS)

    def stop(self, task: BackgroundTask, by: StopCause) -> asyncio.Task[None]:
        """Start stopping *task*, unless that already began; the returned task ends once *task* did."""
        if task.stopping is None:
            task.stopped_by = by
            task.stopping = self._spawn(self._stop(task), self._workers)
        return task.stopping

    async def stop_all(self) -> None:
        """Stop every task, as the session ends, and wait for each to end."""
        for task in self.running:
            self.stop(task, "session_end")
        if self._workers:
            await asyncio.wait(set(self._workers))

    async def close(self) -> None:
        """Stop every task, as the session ends, and take no more."""
        self._closed = True
        if self._sampler is not None:
            self._sampler.cancel()
        await self.stop_all()

    def _listed(self) -> list[BackgroundTask]:
        ended = [task for task in self._ended if task.scope == MAIN_SCOPE][-_LISTED_ENDED:]
        return [task for task in self._running.values() if task.scope == MAIN_SCOPE] + ended

    async def _changed(self) -> None:
        if self._on_change is not None:
            await self._on_change(self)
        await self._crash_record.update(self._running.values())

    def _stop_path(self, task_id: str) -> Path:
        return self.directory / f"{task_id}.stop"

    def _new_id(self) -> str:
        # Unique among the files already here too: an old result naming bg-x
        # must never reach a later task.
        while True:
            task_id = f"bg-{secrets.token_hex(3)}"
            if task_id in self._used_ids:
                continue
            names = (f"{task_id}.log", f"{task_id}.log.1", f"{task_id}.stop")
            if not any((self.directory / name).exists() for name in names):
                self._used_ids.add(task_id)
                return task_id

    def _apply_stop_files(self) -> list[BackgroundTask]:
        tasks = [task for task in self._running.values() if task.stop_path.exists()]
        for task in tasks:
            self.stop(task, "agent")
        return tasks

    async def _sample(self) -> None:
        try:
            while self._running:
                await asyncio.sleep(_STOP_FILE_POLL)
                self._apply_stop_files()
        finally:
            self._sampler = None

    async def _stop(self, task: BackgroundTask) -> None:
        await self._changed()  # The list shows it stopping.
        for force, grace in ((False, _TERM_GRACE), (True, _KILL_GRACE)):
            try:
                await task.process.terminate(force=force)
            except Exception:
                logger.debug("Could not signal background task %s", task.id, exc_info=True)
            with suppress(TimeoutError):
                await asyncio.wait_for(task.ended.wait(), grace)
                return
        # What still runs is out of reach (another user's, say), or a process that left the command
        # holds the output open: tell the first, and stop reading.
        task.unstopped = await task.process.still_running()
        await task.process.close()
        await task.ended.wait()

    async def _watch(self, task: BackgroundTask) -> None:
        try:
            await self._changed()
            if (outlived := await task.process.shell_exited()) and task.stopped_by is None:
                task.shell_exit_code, task.outlived = task.process.returncode, outlived
                task.log.note(f"[icode] shell exited with code {task.shell_exit_code}; other processes still running")
                await self._changed()
            code = await task.process.wait()
            if task.stopped_by is None and task.shell_exit_code is None:
                task.exit_code = code
            await task.log.close(f"[icode] process {_ending(task)}")
        finally:
            await task.process.close()
            self._running.pop(task.id, None)
            if task.stopped_by != "rollback":
                self._ended.append(task)
            task.ended_at = time.time()
            task.ended.set()
            self._record(
                EventType.BACKGROUND_TASK_FINISHED,
                {
                    "task": task.id,
                    "exit_code": task.exit_code,
                    "shell_exit_code": task.shell_exit_code,
                    "stopped_by": task.stopped_by,
                    "duration_ms": int((time.monotonic() - task.started) * 1000),
                },
            )
        await self._changed()
        await asyncio.to_thread(_tidy, self.directory, task.stop_path, set(self._running))

    def _record(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record *event_type* on the session's trajectory as it is now, never one bound when the task started."""
        context = self._trajectory() if self._trajectory is not None else None
        if context is not None:
            context.sink.emit_soon(context.draft(event_type, payload=payload))

    def _spawn[T](self, coro: Coroutine[Any, Any, T], owner: set[asyncio.Task[T]] | None = None) -> asyncio.Task[T]:
        # A fresh context: the task belongs to the session, not to the tool call that started it.
        task = asyncio.get_running_loop().create_task(coro, context=contextvars.Context())
        if owner is not None:
            owner.add(task)
            task.add_done_callback(owner.discard)
        return task


def _ending(task: BackgroundTask) -> str:
    if task.exit_code is not None:
        return f"exited with code {task.exit_code}"
    if task.stopped_by is not None:
        return f"{_STOPPED[task.stopped_by]}{task.stop_note}"
    if task.shell_exit_code is not None:
        # How the processes that outlived the shell ended is not known.
        return f"ended (its shell had exited with code {task.shell_exit_code})"
    return "ended"


def _pids_text(pids: tuple[int, ...]) -> str:
    listed = ", ".join(str(pid) for pid in pids[:_LISTED_PIDS])
    return f"{'pid' if len(pids) == 1 else 'pids'} {listed}{', …' if len(pids) > _LISTED_PIDS else ''}"


def _one_line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _COMMAND_CHARS else f"{text[: _COMMAND_CHARS - 1]}…"


def _tidy(directory: Path, stop_path: Path, running: set[str]) -> None:
    """Remove an ended task's stop file and all but the newest finished logs."""
    with suppress(OSError):
        stop_path.unlink(missing_ok=True)
    try:
        logs = [log for log in directory.glob("*.log") if log.stem not in running]
    except OSError:
        return
    logs.sort(key=_modified, reverse=True)
    for log in logs[_KEPT_FINISHED_LOGS:]:
        for path in (log, log.with_name(f"{log.name}.1"), log.with_suffix(".stop")):
            with suppress(OSError):
                path.unlink(missing_ok=True)


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
