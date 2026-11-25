# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared approval input, results, prompt formatting, and per-request metering."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from chrys.foundation.text.model_json import model_json, model_path
from chrys.kernel import ChatResponse, Message


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalJudgeInput:
    """Judge-only fields separate from presentation metadata."""

    tool_name: str
    tool_kind: str
    args: dict[str, Any]
    user_message: str
    user_messages: list[str]
    workspace_roots: list[str]


@dataclass
class JudgeVerdict:
    """Result of an LLM approval judge evaluation."""

    approved: bool
    reason: str
    audit: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)
    triggered: tuple[str, ...] = ()
    failure_reason: str | None = None


class FormalEvaluationCancelled(asyncio.CancelledError):
    """Cancellation carrying audit evidence, never an approvable result."""

    def __init__(self, verdict: JudgeVerdict) -> None:
        super().__init__("Formal evaluation cancelled")
        self.verdict = verdict


def current_time_fields() -> dict[str, str]:
    """Return the local and UTC forms of the same instant."""
    now_utc = datetime.now(UTC)
    return {
        "Current time (local)": now_utc.astimezone().isoformat(timespec="seconds"),
        "Current time (UTC)": now_utc.isoformat(timespec="seconds"),
    }


def current_time_context() -> str:
    """Return current local and UTC times for the judge prompt."""
    return "\n".join(f"{label}: {value}" for label, value in current_time_fields().items())


def normalize_user_messages(user_message: str, user_messages: list[str] | None) -> list[str]:
    """Keep nonblank current-turn messages and the latest prompt."""
    messages = [message.strip() for message in (user_messages or []) if message.strip()]
    latest = user_message.strip() or (messages[-1] if messages else "")
    if latest and (not messages or messages[-1] != latest):
        messages.append(latest)
    return messages


def format_user_messages(messages: list[str]) -> tuple[str, str]:
    """Return formatted current-turn messages and latest prompt text."""
    if not messages:
        return "(no user message available)", "(no user message available)"

    formatted = "\n".join(
        f"{index}. {message.replace('\n', '\n   ')}" for index, message in enumerate(messages, start=1)
    )
    return formatted, messages[-1]


def build_user_prompt(request: ApprovalJudgeInput) -> str:
    """Build the user-turn prompt for the judge LLM call."""
    workspace = ", ".join(model_path(path) for path in request.workspace_roots) or "(not specified)"
    user_ctx, latest_user_ctx = format_user_messages(request.user_messages)
    formatted_args = model_json(request.args, default=str, indent=2)

    return (
        f"{current_time_context()}\n\n"
        f"Workspace directories: {workspace}\n\n"
        f"Current-turn user prompts:\n{user_ctx}\n\n"
        f"Latest user prompt:\n{latest_user_ctx}\n\n"
        f"Proposed action:\n"
        f"Tool: {request.tool_name} (kind: {request.tool_kind})\n"
        f"Arguments:\n{formatted_args}"
    )


@dataclass(slots=True)
class CallMeter:
    """Track calls and actual usage within a single review deadline."""

    deadline: float
    usage: dict[str, int] = field(default_factory=dict)
    started: bool = False

    async def get_final_response(
        self,
        call: Callable[..., Awaitable[ChatResponse]],
        client: Any,
        messages: list[Message],
        stream: bool,
        options: dict[str, Any] | None,
    ) -> ChatResponse:
        """Account for late responses without accepting expired decisions."""
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if time.monotonic() >= self.deadline:
            raise TimeoutError
        self.started = True
        # For streams, this timeout bounds stalls and resets on progress. The enclosing
        # timeout_at in FormalEvaluator enforces the absolute review deadline.
        response = await call(
            client, messages, stream=stream, options=options, timeout=self.deadline - time.monotonic()
        )
        for key, value in (response.usage_details or {}).items():
            if type(value) is int:
                self.usage[key] = self.usage.get(key, 0) + value
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if time.monotonic() >= self.deadline:
            raise TimeoutError
        return response
