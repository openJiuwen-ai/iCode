# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A prompt shortcut's ``!`command``` placeholders, run as one step of a message's pre-turn preparation.

A message carries its shortcut (``UserMessage.prompt_shortcut``) only when the
frontend expanded it from one and its text still holds placeholders. The step
runs after the ``user_prompt_submit`` hooks passed and the message's images
were checked, before its turn starts:

* the engine must still load that shortcut, from the same file, for the
  session's working folder now — a draft moved to another folder runs nothing;
* the commands run in the session's working folder, and whatever ends the
  admission (the user withdrawing it, an interrupt, a session transition,
  shutdown) ends them, their process trees included;
* a command that cannot start, times out or is stopped refuses the message.

Meanwhile the execution counts as busy (``ExecutionLease.begin_prompt_commands``),
frontends are told (:class:`PromptCommandsRunning` / :class:`PromptCommandsEnded`),
and the run is a ``prompt_commands`` wait under the admission's preparation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from chrys.foundation.events.types import Error, PromptCommandsEnded, PromptCommandsRunning
from chrys.foundation.i18n import msg
from chrys.foundation.trajectory.event_types import WaitCategory
from chrys.foundation.util.once_close import finish_close
from chrys.service.prompt_shortcuts import load_prompt_shortcuts, prompt_shortcut_folders
from chrys.service.prompt_shortcuts.shell import (
    SHELL_TIMEOUT_SECONDS,
    ShellInterpolationError,
    interpolate_shell,
    shell_commands,
)
from chrys.service.trajectory.preparation import PreparationOutcome
from chrys.service.trajectory.waits import WaitOutcome, WaitTrace

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import PromptShortcutRef, UserMessage
    from chrys.orchestration.engine.execution import ExecutionLease, PromptAdmissionScope
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.service.hooks.operation import HookOperation
    from chrys.service.trajectory.preparation import PreparationTrace

logger = logging.getLogger(__name__)

_SHORTCUT_UNAVAILABLE = msg(
    "coordinator.prompt_shortcut_unavailable",
    fallback="/{name} is not a shortcut in this working folder any more, so its commands did not run. Nothing was sent.",
)
_COMMAND_TIMED_OUT = msg(
    "coordinator.prompt_command_timed_out",
    fallback="The command {command} did not finish within {seconds} seconds. Nothing was sent.",
)
_COMMAND_NOT_STARTED = msg(
    "coordinator.prompt_command_not_started",
    fallback="The command {command} could not start ({detail}). Nothing was sent.",
)
_COMMAND_STOPPED = msg(
    "coordinator.prompt_command_stopped",
    fallback="The command {command} was stopped. Nothing was sent.",
)

type CommandsOutcome = Literal["completed", "command_failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class ExpandedMessage:
    """The text to send, or why the message was not sent (a ``PreparationOutcome``)."""

    text: str | None
    outcome: str = PreparationOutcome.COMPLETED


class ShortcutCommands:
    """Runs the step for one engine: the session and the execution lease."""

    def __init__(
        self,
        *,
        bus: EventBus,
        session: ActiveSession,
        lease: Callable[[], ExecutionLease],
    ) -> None:
        self._bus = bus
        self._session = session
        self._lease = lease

    async def expand(
        self,
        event: UserMessage,
        operation: HookOperation,
        *,
        admission: PromptAdmissionScope,
        preparation: PreparationTrace | None,
        should_publish: Callable[[], bool] | None,
    ) -> ExpandedMessage:
        """Return *event*'s text with its shortcut's commands replaced by their output, or the refusal.

        *should_publish* is the admission's publication guard (None publishes
        always). The admission ends the busy state this starts
        (``ExecutionLease.end_prompt_commands``), once its turn started or it
        was refused.
        """

        def may_publish() -> bool:
            return should_publish is None or should_publish()

        ref = event.prompt_shortcut
        commands = shell_commands(event.text)
        if ref is None or not commands:
            return ExpandedMessage(event.text)
        session_id = self._session.session_id
        workspace = self._session.workspace
        cwd = workspace.primary_cwd if workspace is not None else ""
        available = bool(cwd) and await self._still_loaded(ref, cwd)
        if not operation.pending:
            return ExpandedMessage(None, PreparationOutcome.CANCELLED)
        if not available:
            if may_publish():
                await self._bus.publish(
                    Error(
                        code="prompt_shortcut_unavailable",
                        message=f"Prompt shortcut /{ref.name} is not loaded for this working folder",
                        display_message=_SHORTCUT_UNAVAILABLE.bind(name=ref.name),
                        session_id=session_id,
                    )
                )
            return ExpandedMessage(None, PreparationOutcome.REJECTED)
        wait = (
            WaitTrace.open(
                WaitCategory.PROMPT_COMMANDS,
                target_operation_id=preparation.committed_operation_id,
                context=preparation.context,
            )
            if preparation is not None and preparation.committed_operation_id is not None
            else None
        )
        if wait is not None:
            # Started before the lease holds it: a clear of the admission then always
            # closes a wait that has started, never one whose start is still on its way.
            try:
                await wait.started()
            except BaseException:
                wait.finished_soon(outcome=WaitOutcome.CANCELLED)
                raise
        if not self._lease().begin_prompt_commands(admission, wait):
            if wait is not None:
                # The admission was cleared while the wait started, finishing its preparation first: this
                # child closes after its parent, the price of never leaving a started wait unclosed.
                wait.finished_soon(outcome=WaitOutcome.CANCELLED)
            return ExpandedMessage(None, PreparationOutcome.CANCELLED)
        outcome: CommandsOutcome = "cancelled"
        wait_outcome = WaitOutcome.CANCELLED
        announced = False
        failure: ShellInterpolationError | None = None
        try:
            if may_publish():
                # Set first: a cancel that lands while the announcement goes out is raised
                # only once every frontend has it, and the end still follows.
                announced = True
                await self._deliver(
                    PromptCommandsRunning(count=len(commands), injection_id=event.injection_id, session_id=session_id)
                )
            if not may_publish():
                # The admission's session or build moved on while the commands were announced:
                # its message is refused anyway, so none of them runs for nothing.
                return ExpandedMessage(None, PreparationOutcome.CANCELLED)
            expanded, failure = await self._run(event.text, cwd, operation)
            if failure is not None:
                outcome = "command_failed"
                wait_outcome = WaitOutcome.TIMED_OUT if failure.reason == "timeout" else WaitOutcome.FAILED
                return ExpandedMessage(None, PreparationOutcome.COMMAND_FAILED)
            if expanded is None or not operation.pending:
                return ExpandedMessage(None, PreparationOutcome.CANCELLED)
            outcome = "completed"
            wait_outcome = WaitOutcome.COMPLETED
            return ExpandedMessage(expanded)
        finally:
            held = self._lease().take_prompt_commands_wait(admission)
            if held is not None:
                held.finished_soon(outcome=wait_outcome)
            if announced:
                # Not through the admission's publication guard: a session transition is
                # one of the ways the commands end, and the guard is closed by then.
                await self._deliver(
                    PromptCommandsEnded(outcome=outcome, injection_id=event.injection_id, session_id=session_id)
                )
            if failure is not None and may_publish():
                # After the end: what a frontend shows for the running commands ends first,
                # so it never clears the failure shown after it.
                await self._bus.publish(_command_failed(failure, session_id))

    async def _deliver(self, event: PromptCommandsRunning | PromptCommandsEnded) -> None:
        """Publish *event* to every frontend even when the admission is cancelled meanwhile; then re-raise that cancel.

        So every frontend that was told the commands started is told they ended.
        """
        await finish_close(asyncio.create_task(self._bus.publish(event)))

    async def _still_loaded(self, ref: PromptShortcutRef, cwd: str) -> bool:
        """Whether the shortcut list for *cwd* has *ref*, from the same file."""
        folders = prompt_shortcut_folders(cwd)
        try:
            shortcuts = await asyncio.to_thread(load_prompt_shortcuts, **folders)
        except Exception:
            logger.warning("Prompt shortcuts: reading them for a message failed", exc_info=True)
            return False
        return any(shortcut.name == ref.name and shortcut.path == ref.path for shortcut in shortcuts)

    @staticmethod
    async def _run(text: str, cwd: str, operation: HookOperation) -> tuple[str | None, ShellInterpolationError | None]:
        """Run the commands until they finish or the operation ends; ``(None, None)`` when it ended first."""
        if not operation.pending:
            # Withdrawn while the step announced itself: no command starts.
            return None, None
        task = asyncio.create_task(interpolate_shell(text, cwd=cwd), name="chrys.prompt_commands")
        invalidated = asyncio.create_task(operation.wait_invalidated(), name="chrys.prompt_commands.invalidated")
        try:
            await asyncio.wait({task, invalidated}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            invalidated.cancel()
            if not operation.pending or not task.done():
                # Withdrawn, interrupted or the session moved on (a tie goes to the end), or the
                # admission itself was cancelled: the commands end, their process trees with them.
                task.cancel()
            await _drain(task, invalidated)
        if not operation.pending or task.cancelled():
            return None, None
        error = task.exception()
        if isinstance(error, ShellInterpolationError):
            return None, error
        if error is not None:
            raise error
        return task.result(), None


async def _drain(*tasks: asyncio.Task[Any]) -> None:
    """Wait for every task to end, however often the waiter is cancelled meanwhile; then re-raise that cancel.

    Not ``finish_close``: that re-raises the task's own cancellation, which here
    is the expected end of commands a withdrawn message no longer needs.
    """
    interrupted: asyncio.CancelledError | None = None
    while pending := {task for task in tasks if not task.done()}:
        try:
            await asyncio.wait(pending)
        except asyncio.CancelledError as exc:
            interrupted = exc
            for task in pending:
                task.cancel()
    for task in tasks:
        if not task.cancelled():
            # Retrieved here so an outcome nobody needs is never logged as unretrieved.
            task.exception()
    if interrupted is not None:
        raise interrupted


def _command_failed(failure: ShellInterpolationError, session_id: str | None) -> Error:
    command = failure.command
    if failure.reason == "timeout":
        display = _COMMAND_TIMED_OUT.bind(command=command, seconds=int(SHELL_TIMEOUT_SECONDS))
    elif failure.reason == "start_failed":
        display = _COMMAND_NOT_STARTED.bind(command=command, detail=failure.detail or failure.reason)
    else:
        display = _COMMAND_STOPPED.bind(command=command)
    return Error(
        code="prompt_shortcut_command_failed",
        message=f"Prompt shortcut command failed: {failure}",
        display_message=display,
        session_id=session_id,
    )


__all__ = ["ExpandedMessage", "ShortcutCommands"]
