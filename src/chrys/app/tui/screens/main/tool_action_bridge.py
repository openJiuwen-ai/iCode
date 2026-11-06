# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bridge inline tool widget actions to backend events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from chrys.app.tui.screens.main.ports import EventPublisher
from chrys.foundation.events.types import (
    ApprovalResponse,
    AskUserResponse,
    BackgroundTaskStop,
    HookConfirmationResponse,
    InvocationAbortRequested,
    InvocationRetryRequested,
    RetryWaitSkip,
    ShellToBackground,
    SleepSkip,
)
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.foundation.models.ask_user import AskUserAnswer
from chrys.foundation.models.hook_confirmations import HookConfirmationAnswer


class ToolActionBridge:
    """Publish backend events for inline tool action controls."""

    def __init__(self, *, publisher: EventPublisher, debug: Callable[[str, str], None]) -> None:
        self._publisher = publisher
        self._debug = debug

    async def request_sub_agent_retry(self, invocation_id: str) -> None:
        """Publish a sub-agent retry request."""
        await self._publisher.publish(InvocationRetryRequested(invocation_id=invocation_id))
        self._log(f"InvocationRetryRequested inv={invocation_id}")

    async def request_sub_agent_abort(self, invocation_id: str) -> None:
        """Publish a sub-agent abort request."""
        await self._publisher.publish(InvocationAbortRequested(invocation_id=invocation_id))
        self._log(f"InvocationAbortRequested inv={invocation_id}")

    async def skip_sleep(self, call_id: str) -> None:
        """Publish a sleep-skip request."""
        await self._publisher.publish(SleepSkip(call_id=call_id))
        self._log(f"SleepSkip call={call_id}")

    async def skip_retry_wait(self, wait_id: str) -> None:
        """Publish a request to retry now instead of waiting out the backoff."""
        await self._publisher.publish(RetryWaitSkip(wait_id=wait_id))
        self._log(f"RetryWaitSkip wait={wait_id}")

    async def move_shells_to_background(self) -> None:
        """Publish a request to move the main agent's running shell commands to the background."""
        await self._publisher.publish(ShellToBackground())
        self._log("ShellToBackground")

    async def stop_background_task(self, task_id: str) -> None:
        """Publish a request to stop one background command."""
        await self._publisher.publish(BackgroundTaskStop(task_id=task_id))
        self._log(f"BackgroundTaskStop task={task_id}")

    async def submit_ask_user_inline(self, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None:
        """Publish an inline ask-user response."""
        await self._publisher.publish(AskUserResponse(request_id=request_id, answers=answers))
        self._log("AskUserResponse", f"{len(answers)} answers")

    async def publish_hook_confirmation_answer(
        self, session_id: str | None, request_id: str, answer: HookConfirmationAnswer
    ) -> None:
        """Answer the hook confirmation this screen claimed and showed."""
        await self._publisher.publish(
            HookConfirmationResponse(session_id=session_id, request_id=request_id, answer=answer)
        )
        self._log(f"HookConfirmationResponse req={request_id} answer={answer}")

    async def publish_approval_response(
        self,
        request_id: str,
        approved: bool,
        reason: str = "",
        modified_args: dict[str, Any] | None = None,
        remember_choice: ReuseChoice = "",
    ) -> None:
        """Publish an approval response."""
        await self._publisher.publish(
            ApprovalResponse(
                request_id=request_id,
                approved=approved,
                reason=reason,
                modified_args=modified_args,
                remember_choice=remember_choice,
            )
        )
        self._log("ApprovalResponse", "approved" if approved else "declined")

    async def publish_ask_user_response(self, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None:
        """Publish a modal ask-user response."""
        await self._publisher.publish(AskUserResponse(request_id=request_id, answers=answers))
        self._log("AskUserResponse", f"{len(answers)} answers")

    def _log(self, event_type: str, detail: str = "") -> None:
        self._debug(event_type, detail)
