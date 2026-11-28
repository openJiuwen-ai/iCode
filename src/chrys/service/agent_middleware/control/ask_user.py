# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ask-user middleware — intercepts ask_user tool calls to show a dialog."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import uuid4

from chrys.foundation.config.settings import DEFAULT_ASK_USER_TIMEOUT_SECONDS
from chrys.foundation.events.types import AskUserResponse, AskUserTimedOut, QuestionToUser
from chrys.foundation.models.ask_user import (
    ASK_USER_EVIDENCE_KEY,
    MAX_ASK_USER_DESCRIPTION_CHARS,
    MAX_ASK_USER_HEADER_CHARS,
    MAX_ASK_USER_LABEL_CHARS,
    MAX_ASK_USER_OPTIONS,
    MAX_ASK_USER_QUESTION_CHARS,
    MAX_ASK_USER_QUESTIONS,
    AskUserAnswer,
    AskUserQuestion,
    format_ask_user_result,
    parse_ask_user_answers,
    parse_ask_user_questions,
)
from chrys.foundation.tool_kinds import KIND_ASK_USER, get_tool_kind
from chrys.foundation.tool_result_metadata import TOOL_RESULT_METADATA_KEY
from chrys.foundation.trajectory.event_types import WaitCategory
from chrys.foundation.util.once_close import finish_close
from chrys.foundation.util.time import utc_iso
from chrys.kernel.middleware import FunctionMiddleware
from chrys.service.agent_middleware._metadata_keys import _SHORT_ID_LEN
from chrys.service.agent_middleware.events.hook_dispatch import get_call_id
from chrys.service.approval.correlation import OneShotCorrelation
from chrys.service.tools.result_metadata import tool_error
from chrys.service.trajectory.tools import tool_operation_id
from chrys.service.trajectory.waits import WaitOutcome, WaitTrace

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.foundation.events.bus import EventBus
    from chrys.kernel.middleware import FunctionInvocationContext


def _format_timeout(seconds: int) -> str:
    """Render a timeout as whole minutes when divisible, else seconds."""
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def _validate_questions(questions: tuple[AskUserQuestion, ...]) -> str | None:
    if not questions:
        return tool_error("ask_user_no_question", "no usable question was provided.", retryable=True)
    if len(questions) > MAX_ASK_USER_QUESTIONS:
        return tool_error(
            "ask_user_too_many_questions",
            f"ask_user accepts at most {MAX_ASK_USER_QUESTIONS} questions.",
            retryable=True,
            details={"max_questions": MAX_ASK_USER_QUESTIONS},
        )
    for question_index, question in enumerate(questions):
        if len(question.options) > MAX_ASK_USER_OPTIONS:
            return tool_error(
                "ask_user_too_many_options",
                f"question {question_index + 1} has more than {MAX_ASK_USER_OPTIONS} options.",
                retryable=True,
                details={"question_index": question_index, "max_options": MAX_ASK_USER_OPTIONS},
            )
        field_lengths = (
            ("question", question.question, MAX_ASK_USER_QUESTION_CHARS),
            ("header", question.header, MAX_ASK_USER_HEADER_CHARS),
        )
        for field_name, value, cap in field_lengths:
            if len(value) > cap:
                return tool_error(
                    "ask_user_field_too_long",
                    f"question {question_index + 1} field {field_name!r} exceeds {cap} characters.",
                    retryable=True,
                    details={"question_index": question_index, "field": field_name, "max_chars": cap},
                )
        seen_labels: set[str] = set()
        for option_index, option in enumerate(question.options):
            option_fields = (
                ("label", option.label, MAX_ASK_USER_LABEL_CHARS),
                ("description", option.description, MAX_ASK_USER_DESCRIPTION_CHARS),
            )
            for field_name, value, cap in option_fields:
                if len(value) > cap:
                    return tool_error(
                        "ask_user_field_too_long",
                        f"question {question_index + 1} option {option_index + 1} field {field_name!r} "
                        f"exceeds {cap} characters.",
                        retryable=True,
                        details={
                            "question_index": question_index,
                            "option_index": option_index,
                            "field": field_name,
                            "max_chars": cap,
                        },
                    )
            if option.label in seen_labels:
                return tool_error(
                    "ask_user_duplicate_options",
                    f"question {question_index + 1} contains duplicate option labels.",
                    retryable=True,
                    details={"question_index": question_index, "label": option.label},
                )
            seen_labels.add(option.label)
    return None


def _answers_from_response(response: AskUserResponse, *, question_count: int) -> tuple[AskUserAnswer, ...]:
    return parse_ask_user_answers(response.answers or (), question_count=question_count)


class AskUserMiddleware(FunctionMiddleware):
    """Intercepts ``ask_user`` tool calls to show a dialog and wait for a response.

    Follows the same pattern as ``ApprovalMiddleware``: publishes a
    ``QuestionToUser`` event, awaits the ``AskUserResponse``, and writes
    the user's answer into ``context.result`` — the underlying tool function
    is never invoked.

    A cancelled call withdraws its unanswered question with ``AskUserTimedOut``,
    so a frontend stops showing it.

    ``timeout_seconds`` bounds how long to wait for the reply.  ``None`` waits
    indefinitely (no timeout) — used by frontends that own the
    interaction lifetime themselves (e.g. ACP clients).
    """

    def __init__(
        self,
        event_bus: EventBus,
        session_id: str | None = None,
        caller_name: str = "",
        *,
        timeout_seconds: int | None = DEFAULT_ASK_USER_TIMEOUT_SECONDS,
    ) -> None:
        self._bus = event_bus
        self._session_id = session_id
        self._caller_name = caller_name
        self._timeout_seconds = timeout_seconds

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        if get_tool_kind(context.function) != KIND_ASK_USER:
            await call_next()
            return

        args = context.arguments if isinstance(context.arguments, dict) else {}
        questions = parse_ask_user_questions(args)
        if error := _validate_questions(questions):
            context.result = error
            return

        request_id = uuid4().hex[:_SHORT_ID_LEN]

        # Subscribe before publishing. Publishing awaits frontend handlers and
        # can be cancelled before the response wait starts, so the subscription
        # is owned across that whole interval.
        async with OneShotCorrelation(self._bus, AskUserResponse, request_id=request_id) as correlation:
            future = correlation.future
            try:
                await self._bus.publish(
                    QuestionToUser(
                        request_id=request_id,
                        questions=questions,
                        session_id=self._session_id,
                        call_id=get_call_id(context),
                        caller_name=self._caller_name,
                    )
                )
            except asyncio.CancelledError:
                # A frontend handler that already ran shows the question,
                # unless it already answered it.
                if not future.done() or future.cancelled():
                    await self._withdraw(request_id)
                raise

            timeout = self._timeout_seconds
            wait = WaitTrace.open(WaitCategory.USER_INPUT, target_operation_id=tool_operation_id(context.metadata))
            try:
                # Inside the block that closes the wait and drops the subscription:
                # the start marker awaits its write ack, and an interrupt landing
                # there is exactly the case the cancellation branch below exists for.
                if wait is not None:
                    await wait.started()
                response = await (future if timeout is None else asyncio.wait_for(future, timeout=timeout))
                if response.cancelled:
                    context.result = tool_error(
                        "ask_user_no_response",
                        "user did not provide a response.",
                        retryable=True,
                    )
                else:
                    answers = _answers_from_response(response, question_count=len(questions))
                    result = format_ask_user_result(questions, answers)
                    context.result = result
                    if any(answer.answered for answer in answers):
                        context.metadata.setdefault(TOOL_RESULT_METADATA_KEY, {})[ASK_USER_EVIDENCE_KEY] = {
                            "questions": [asdict(question) for question in questions],
                            "result": result,
                            "answered_at": utc_iso(response.timestamp),
                        }
                if wait is not None:
                    await wait.finished()
            except TimeoutError:
                try:
                    if wait is not None:
                        await wait.finished(outcome=WaitOutcome.TIMED_OUT)
                finally:
                    # The finish marker awaits its write ack: a cancel landing
                    # there skips the handler below, so withdraw here.
                    await self._withdraw(request_id)
                context.result = tool_error(
                    "ask_user_timeout",
                    f"user did not respond within {_format_timeout(timeout or 0)}.",
                    retryable=True,
                    details={"timeout_seconds": timeout or 0},
                )
            except asyncio.CancelledError:
                if wait is not None:
                    wait.finished_soon(outcome=WaitOutcome.CANCELLED)
                # An interrupted wait cancels the future; only an answer settles it.
                if not future.done() or future.cancelled():
                    await self._withdraw(request_id)
                raise

    async def _withdraw(self, request_id: str) -> None:
        """Tell frontends the question no longer waits.

        The withdrawal finishes even when a cancel lands meanwhile; that cancel
        is raised once it has.
        """
        withdraw = asyncio.create_task(
            self._bus.publish(AskUserTimedOut(request_id=request_id, session_id=self._session_id))
        )
        await finish_close(withdraw)
