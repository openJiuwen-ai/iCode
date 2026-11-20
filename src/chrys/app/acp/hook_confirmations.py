# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Show a hook's ``ask`` to the ACP client as a standard ``session/request_permission``.

One per ACP session, owned by it (``ManagedSession``) and closed with it, however
the session ends. Out of band: the requests and withdrawals arrive through
session-level subscriptions, never through the prompt's serialized event
loop, so a new input's own check (whose hooks run while that input is still
being published) reaches the client too, and a waiting question never holds
up the model's other output. A request handler only claims, records and
starts a task, then returns; the task asks the client and publishes the one
answer as soon as the client gives it. The question's card is then settled with
a terminal ``tool_call_update`` (``completed`` once allowed, ``failed``
otherwise), as is a withdrawn request's, whose task is cancelled: cancelling the
JSON-RPC wait alone leaves the client's dialog open until the turn ends, and an
answered card would stay pending. The update goes out once, as one step every
caller drains, so a withdrawal or close landing while it is being sent never
leaves the card open; it never holds up the answer, whose timeliness is the
user's, not the client's rendering.

The question has an id of its own (``hook-confirm-<request>``), which no
tool card carries. Clients show either the title or the content depending
on the kind, so each says on its own which hook asks, why, and about what.
All text here is protocol output and stays English.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from acp import schema as acp_schema
from acp.helpers import update_tool_call

from chrys.app.acp.bridge import acp_tool_kind
from chrys.foundation.events.types import HookConfirmationRequest, HookConfirmationResponse, HookConfirmationWithdrawn
from chrys.foundation.models.hook_confirmations import HookArgumentChange, HookConfirmationAnswer
from chrys.foundation.util.once_close import OnceClose, finish_close

if TYPE_CHECKING:
    from acp.interfaces import Client

    from chrys.foundation.events.bus import EventBus

logger = logging.getLogger(__name__)

_ALLOW = acp_schema.PermissionOption(optionId="allow", name="Allow once", kind="allow_once")
_REJECT = acp_schema.PermissionOption(optionId="reject", name="Reject once", kind="reject_once")
_TITLE_SUBJECT_MAX_CHARS = 120
_TITLE_NAMES_MAX_CHARS = 60
_SETTLE_CARD_TIMEOUT_SECONDS = 5.0


def confirmation_tool_call_id(request_id: str) -> str:
    """The ``toolCallId`` a confirmation is shown under: its own, never a tool card's."""
    return f"hook-confirm-{request_id}"


def confirmation_tool_call(event: HookConfirmationRequest) -> acp_schema.ToolCallUpdate:
    """The question a request puts to the client; title and content each say it all.

    The title names what is asked about (the tool and its arguments, what changed when asked
    again, or the message) on one line, for clients that show only the title; the content
    says the same with the arguments or message shortened to fit a dialog
    (``_shorten_arguments``, ``_clip_message``); a tool's arguments are whole in
    ``rawInput``.
    """
    asks = f"Hook '{event.hook_id}' asks: {event.reason}" if event.reason else f"Hook '{event.hook_id}' asks to confirm"
    if event.subject == "tool":
        tool = event.tool_name or "a tool"
        try:
            compact = json.dumps(event.arguments, ensure_ascii=False, default=str)
        except TypeError, ValueError, RecursionError:
            compact = str(event.arguments)
        shown, shortened = _shorten_arguments(event.arguments)
        more = "\n(Shortened; the whole arguments are in rawInput.)" if shortened else ""
        detail = f"Tool: {event.tool_name}\nArguments:\n{shown}{more}"
        changes = [
            f"{change.name}: {change.before or '(none)'} → {change.after or '(none)'}"
            for change in event.changed_arguments
        ]
        if event.asked_again:
            listed = "\n".join(f"  {change}" for change in changes)
            detail = f"Asked again: the arguments changed after you allowed them.\nChanged:\n{listed}\n\n{detail}"
            title = f"Asked again: {asks} — run {tool} with {_changes_title(event.changed_arguments)}?"
        else:
            title = f"{asks} — run {tool} {_one_line(compact)}?"
        kind = acp_tool_kind(event.tool_kind)
        raw_input: dict[str, object] | None = {"tool": event.tool_name, "args": event.arguments}
    else:
        shown, cut = _clip_message(event.input_text)
        detail = f"Your message:\n{shown}{'…' if cut else ''}"
        title = f'{asks} — send your message "{_one_line(event.input_text)}"?'
        kind = "other"
        raw_input = None
    source_name = "working directory" if event.hook_source == "workdir" else event.hook_source
    source = f" ({source_name} hook)" if source_name else ""
    body = f"{asks}{source}\n\n{detail}"
    return acp_schema.ToolCallUpdate(
        toolCallId=confirmation_tool_call_id(event.request_id),
        # One line: a reason a hook wrote may hold line breaks.
        title=" ".join(title.split()),
        kind=kind,
        status="pending",
        rawInput=raw_input,
        content=[
            acp_schema.ContentToolCallContent(
                type="content", content=acp_schema.TextContentBlock(type="text", text=body)
            )
        ],
    )


_MESSAGE_MAX_CHARS = 500
_MESSAGE_MAX_LINES = 10
_ARGUMENT_MAX_CHARS = 200
_MAX_ARGUMENTS = 10


def _clip_message(text: str) -> tuple[str, bool]:
    """The start of the user's message a question shows (its first ``_MESSAGE_MAX_LINES``
    lines, up to ``_MESSAGE_MAX_CHARS``), and whether that left anything out.

    A client shows the content in a permission dialog, where a long text is not read anyway.
    """
    shown = "\n".join(text[:_MESSAGE_MAX_CHARS].split("\n")[:_MESSAGE_MAX_LINES])
    return shown, len(shown) < len(text)


def _shorten_arguments(arguments: dict[str, object]) -> tuple[str, bool]:
    """*arguments* as a JSON-shaped block, each value on one line and cut to fit, and whether
    anything was cut or left out.

    Each name stays, so the call reads as what it is, and a long value (a script, a
    file's content) gives way; the whole arguments are in ``rawInput``.
    """
    lines: list[str] = []
    shortened = False
    for name, value in list(arguments.items())[:_MAX_ARGUMENTS]:
        text = _one_line_json(value)
        if len(text) > _ARGUMENT_MAX_CHARS:
            shortened = True
            closing = '"' if text.startswith('"') else ""
            text = text[: _ARGUMENT_MAX_CHARS - 1 - len(closing)] + "…" + closing
        lines.append(f"  {json.dumps(str(name), ensure_ascii=False)}: {text},")
    if (hidden := len(arguments) - _MAX_ARGUMENTS) > 0:
        shortened = True
        lines.append(f"  … ({hidden} more)")
    elif lines:
        lines[-1] = lines[-1].removesuffix(",")
    return ("\n".join(["{", *lines, "}"]) if lines else "{}"), shortened


def _one_line_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError, ValueError, RecursionError:
        return " ".join(str(value).split())


def _changes_title(changes: tuple[HookArgumentChange, ...]) -> str:
    """What changed, for a title: every changed name, then the first one's new value (its old one
    is left to the content, so a long old value never crowds the new one out of the title)."""
    if not changes:
        return "changed arguments"
    names = _one_line(", ".join(change.name for change in changes), _TITLE_NAMES_MAX_CHARS)
    first = changes[0]
    now = _one_line(first.after) if first.after is not None else "(removed)"
    return f"{names} changed; {first.name} is now {now}"


def _one_line(text: str, limit: int = _TITLE_SUBJECT_MAX_CHARS) -> str:
    """*text* on one line, cut to *limit* characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


@dataclass(slots=True)
class _Question:
    """One claimed request: its task, its card's settling, and whether it was answered or withdrawn
    (so close knows what is owed)."""

    event: HookConfirmationRequest
    task: asyncio.Task[None] | None = None
    settling: asyncio.Task[None] | None = None
    answered: bool = False
    withdrawn: bool = False


class AcpSessionHookConfirmations:
    """One ACP session's hook confirmations: claims its questions, asks the client, settles the cards."""

    def __init__(self, session_id: str, bus: EventBus, client: Callable[[], Client]) -> None:
        self._session_id = session_id
        self._bus = bus
        self._client = client
        self._questions: dict[str, _Question] = {}
        self._closed = False
        self._close_once = OnceClose(self._close)

    async def subscribe(self) -> None:
        await self._bus.subscribe(HookConfirmationRequest, self._on_request)
        await self._bus.subscribe(HookConfirmationWithdrawn, self._on_withdrawn)

    async def close(self) -> None:
        """Take no more; end every question's task; answer, as unavailable, every one the backend still waits on.

        The answer comes from here, not from the task: a task cancelled before
        its first step never runs its own handlers. Runs once, as one task every
        caller drains: a close that is itself cancelled still completes it.
        """
        await self._close_once()

    async def _close(self) -> None:
        self._closed = True
        await self._bus.unsubscribe(HookConfirmationRequest, self._on_request)
        await self._bus.unsubscribe(HookConfirmationWithdrawn, self._on_withdrawn)
        questions = list(self._questions.values())
        tasks = [question.task for question in questions if question.task is not None]
        for task in tasks:
            # A task settling its card finishes that first (``_settle_card``): cancelling never cuts it short.
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for question in questions:
            if not question.answered and not question.withdrawn:
                await self._answer(question, "frontend_unavailable")

    async def _on_request(self, event: HookConfirmationRequest) -> None:
        if self._closed or event.session_id != self._session_id or not event.claim.try_claim():
            return
        # Recorded with its task before the handler returns: a withdrawal published next finds both.
        question = _Question(event)
        self._questions[event.request_id] = question
        question.task = asyncio.create_task(self._ask(question), name=f"chrys.acp.hook_confirmation.{event.request_id}")
        question.task.add_done_callback(lambda _task: self._questions.pop(event.request_id, None))

    async def _on_withdrawn(self, event: HookConfirmationWithdrawn) -> None:
        if event.session_id != self._session_id:
            return
        question = self._questions.get(event.request_id)
        if question is not None and question.task is not None:
            # Kept until the task has closed the dialog: close still drains it.
            question.withdrawn = True
            question.task.cancel()

    async def _ask(self, question: _Question) -> None:
        tool_call = confirmation_tool_call(question.event)
        answer: HookConfirmationAnswer
        try:
            response = await self._client().request_permission(
                session_id=self._session_id, tool_call=tool_call, options=[_ALLOW, _REJECT]
            )
        except asyncio.CancelledError:
            # Withdrawn or closed: close the client's dialog either way.
            await self._settle_card(question, "failed")
            raise
        except Exception:
            logger.warning("ACP client could not show a hook confirmation", exc_info=True)
            answer = "frontend_unavailable"
        else:
            outcome = response.outcome
            if isinstance(outcome, acp_schema.AllowedOutcome):
                answer = "approved" if outcome.option_id == _ALLOW.option_id else "declined"
            else:
                # The client cancelled the request: the question was closed without a choice.
                answer = "dismissed"
        try:
            # The answer goes on first: how long the client takes to render the card's end never makes
            # an answer given in time late.
            await self._answer(question, answer)
        finally:
            await self._settle_card(question, "completed" if answer == "approved" else "failed")

    async def _answer(self, question: _Question, answer: HookConfirmationAnswer) -> None:
        question.answered = True
        with contextlib.suppress(Exception):
            await self._bus.publish(
                HookConfirmationResponse(
                    session_id=self._session_id, request_id=question.event.request_id, answer=answer
                )
            )

    async def _settle_card(self, question: _Question, status: Literal["completed", "failed"]) -> None:
        """End the question's card on the client with *status*: once, as one task every caller drains.

        The first caller's status is the card's; a caller cancelled while it waits (a withdrawal or close
        landing just then) still lets the update go out, so the card never stays open.
        """
        if question.settling is None:
            tool_call_id = confirmation_tool_call_id(question.event.request_id)
            question.settling = asyncio.create_task(
                self._send_card_status(tool_call_id, status),
                name=f"chrys.acp.hook_confirmation.settle.{question.event.request_id}",
            )
        await finish_close(question.settling)

    async def _send_card_status(self, tool_call_id: str, status: Literal["completed", "failed"]) -> None:
        # Bounded: it runs while the question winds down, and a hung client must not hold up an answer or a close.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_SETTLE_CARD_TIMEOUT_SECONDS):
                await self._client().session_update(
                    session_id=self._session_id,
                    update=update_tool_call(tool_call_id, status=status),
                )
