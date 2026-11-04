# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn input anchoring and recovery registration; execution belongs to the backend."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.models.history_markers import HistoryMarkerKind, copy_reminder_record
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.turns import (
    UserMessageKind,
    current_turn_start,
    is_continuation_message,
    user_text_matches,
)
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id, read_analytics_item_id
from chrys.kernel import Content, Message
from chrys.orchestration.invoker.contracts import Failed, InvocationOutcome, RunIntent, RunRequest
from chrys.orchestration.invoker.evidence import InvocationEvidence
from chrys.service.session.history import SessionHistoryManager
from chrys.service.session.message_metadata import (
    MESSAGE_CREATED_AT_KEY,
    stamp_message_created_at,
    try_normalize_created_at,
)

if TYPE_CHECKING:
    from chrys.orchestration.invoker.kernel import KernelConversation

logger = logging.getLogger(__name__)


class RecoveryInputRecorder(Protocol):
    """Register a destructively popped anchor in the Turn's recovery input."""

    def __call__(
        self, text: str, contents: list[Any] | None, created_at: datetime | str | None, kind: UserMessageKind = "opener"
    ) -> None: ...


@dataclass
class TurnPassState:
    """Turn presentation flags, separate from the sole L0 attempt task."""

    running: bool = False
    was_interrupted: bool = False
    run_failed: bool = False
    last_error: str = ""
    partial_answer: tuple[Content, ...] = ()
    """The answer contents of the response the ended pass left unrecorded."""


def _make_user_message(
    contents: list[Any], created_at: datetime | str | None = None, *, item_id: str | None = None
) -> Message:
    """Create a user message with Chrys persisted metadata."""
    user_message = Message("user", contents)
    stamp_message_created_at(user_message, created_at)
    ensure_analytics_item_id(user_message.additional_properties, item_id=item_id)
    return user_message


def _make_injected_message(
    contents: list[Any], created_at: datetime | str | None = None, *, item_id: str | None = None
) -> Message:
    """Create a mid-turn user message flagged as user-authored input."""
    user_message = _make_user_message(contents, created_at, item_id=item_id)
    user_message.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    return user_message


def _replay_user_message(
    contents: list[Any], created_at: datetime | str | None = None, *, item_id: str | None = None
) -> Message:
    """Recreate an existing user message without inventing missing metadata.

    *item_id* is the popped original's analytics id: the replay is the same
    persisted item, so it keeps the identity the trajectory already refers to.
    """
    user_message = Message("user", contents)
    if created_at:
        stamp_message_created_at(user_message, created_at)
    if item_id is not None:
        ensure_analytics_item_id(user_message.additional_properties, item_id=item_id)
    return user_message


def _existing_message_created_at(message: Message) -> datetime | str | None:
    value = message.additional_properties.get(MESSAGE_CREATED_AT_KEY)
    return try_normalize_created_at(value) if isinstance(value, datetime | str) else None


class TurnResumePolicy:
    """Prepare RunRequests without driving a second TurnRunner or model loop."""

    def __init__(self, backend: KernelConversation, session_id: str | None, state: TurnPassState) -> None:
        self.backend = backend
        self._session_id = session_id
        self.state = state
        self.origin = InvocationOrigin("turn", session_id or "", new_analytics_id(), None)
        self.outcome: InvocationOutcome | None = None
        self.evidence = InvocationEvidence(self.origin.invocation_id)
        self.pending_continuation_token: Any = None
        self.recovery_input_recorder: RecoveryInputRecorder | None = None
        self._opening_item_id: str | None = None
        # Live ``additional_properties`` of the message the last request sent
        # as new input (fresh opener, retry note or replayed anchor); None for
        # an empty-input continuation. The reminder middleware records what
        # the message carried there once a request is established, so every
        # rebuilt copy (failure fallback, recovery checkpoint) takes it over.
        self.input_properties: dict[str, Any] | None = None

    def set_opening_item_id(self, item_id: str | None) -> None:
        """Pre-assign the analytics item id the next opening user message takes."""
        self._opening_item_id = item_id

    def begin_invocation(self, *, typed_text: str | None = None) -> None:
        """Allocate the fresh operation's identity synchronously before preparation.

        *typed_text*, what the user typed when the message sent differs from it,
        is recorded at once: a Stop before the request goes out leaves the
        message to the failure fallback and the recovery checkpoint, which take
        their record from :attr:`input_properties`.
        """
        self.origin = InvocationOrigin("turn", self._session_id or "", new_analytics_id(), None)
        self.outcome = None
        self.evidence = InvocationEvidence(self.origin.invocation_id)
        self.pending_continuation_token = None
        self.input_properties = {HistoryMarkerKind.TYPED_TEXT_KEY: typed_text} if typed_text else None

    def _take_opening_item_id(self) -> str | None:
        item_id = self._opening_item_id
        self._opening_item_id = None
        return item_id

    def fresh_request(
        self, contents: list, created_at: datetime | str | None = None, *, typed_text: str | None = None
    ) -> RunRequest:
        """Prepare a new Turn input after checking the backend admission fence.

        *typed_text* is what the user typed when the message's text differs from
        it (a prompt shortcut's commands spliced in): recorded on the message
        (``HistoryMarkerKind.TYPED_TEXT_KEY``), never sent.
        """
        user_message = _make_user_message(contents, created_at, item_id=self._opening_item_id)
        if typed_text is not None and typed_text != user_message.text:
            user_message.additional_properties[HistoryMarkerKind.TYPED_TEXT_KEY] = typed_text
        request = RunRequest([user_message], RunIntent.FRESH, self.origin)
        self.backend.validate(request)
        self._take_opening_item_id()
        self.input_properties = user_message.additional_properties
        return request

    def continuation_request(self, messages: list[Message]) -> RunRequest:
        ticket = self.outcome.continuation if isinstance(self.outcome, Failed) else None
        if (
            isinstance(self.outcome, Failed)
            and ticket is not None
            and not self.backend.continuation_is_live(ticket, self.origin)
        ):
            self.outcome = replace(self.outcome, continuation=None)
            ticket = None
        return RunRequest(messages, RunIntent.RETRY if ticket is not None else RunIntent.CONTINUE, self.origin, ticket)

    @asynccontextmanager
    async def retry_request(
        self,
        additional_text: str = "",
        created_at: datetime | str | None = None,
        *,
        withdraw: Callable[[tuple[str, ...]], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[RunRequest | None]:
        """Resume from current conversation state.

        When *additional_text* is non-empty, it is used as the mid-turn
        continuation prompt. The text is sent as a real user message inside
        the current turn, so it appears as a proper user turn in history.

        When *additional_text* is empty, a trailing answer the failed pass
        showed is dropped first, so it is asked for again (*withdraw* hears
        the segments it was shown in, before anything else runs), and then:

        * If there are completed tool calls after the last user message,
          continues from the transcript with empty input.
        * If there is no completed work, re-sends the original user
          message so the LLM retries from scratch.

        With the ``SystemReminderMiddleware``, session state messages are
        always clean (no ``<system-reminder>`` tags), so no stripping is
        needed on resume.
        """
        self.input_properties = None
        self.backend.validate(self.continuation_request([]))
        state = self.backend.session.state.get("chrys_history", {})
        messages = state.get("messages", [])

        if additional_text:
            # Mid-turn user note: send as the continuation prompt and
            # preserve it in history.  No pop of the orphan user message
            # either — the original prompt stays, the note is appended
            # as a mid-turn follow-up (flagged ``_injected``: user-authored
            # input that must not open a new turn).  The note needs a fresh
            # create to reach the model — retrieving a pending background
            # response would silently ignore it.
            self.pending_continuation_token = None
            opening_item_id = self._take_opening_item_id()
            note = _make_injected_message([additional_text], created_at, item_id=opening_item_id)
            self.input_properties = note.additional_properties
            yield self.continuation_request([note])

            # Belt-and-suspenders: if the agent run errored before
            # persisting the user input, ensure the note survives so the
            # next retry can see it.  Scope dedup to the current turn
            # region (messages after the last ``_chrys_kind='turn'``
            # marker) — mirrors the fallback in the empty-text branch
            # below and :meth:`SessionHistoryManager.ensure_user_message`.
            # The dedup is kind-aware: guidance worded identically to the
            # turn opener must still be appended (flagged), not skipped.
            if self.state.run_failed or self.state.was_interrupted:
                messages = state.get("messages", [])
                start = current_turn_start(messages)
                has_note = any(user_text_matches(m, additional_text, kind="injected") for m in messages[start:])
                if not has_note:
                    fallback_note = _make_injected_message([additional_text], created_at, item_id=opening_item_id)
                    copy_reminder_record(note.additional_properties, fallback_note.additional_properties)
                    messages.append(fallback_note)
            return

        history = SessionHistoryManager()
        history.bind(state)
        if (withdrawn := history.remove_trailing_answer()) and withdraw is not None:
            await withdraw(withdrawn)

        # Find the last real user input. Legacy synthetic nudges stay in
        # persisted history for read compatibility but cannot become anchors.
        user_idx = -1
        user_text = ""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "user" and not is_continuation_message(messages[i]):
                user_idx = i
                user_text = messages[i].text or ""
                break

        if user_idx < 0:
            self.state.run_failed = True
            self.state.last_error = "Cannot resume without a real user message in history."
            logger.error("Turn resume rejected defensively: %s", self.state.last_error)
            yield None
            return

        # Check for completed work (assistant/tool) after the user message
        has_work_after = any(messages[j].role in ("assistant", "tool") for j in range(user_idx + 1, len(messages)))

        if has_work_after or self.pending_continuation_token is not None:
            # Completed tool calls exist — or an announced background
            # response is still in flight with no landed output yet.  Either
            # way the transcript continues: the history provider supplies it,
            # and a pending token retrieves the known response instead of
            # creating a duplicate that would re-run hosted work.
            yield self.continuation_request([])
        else:
            # No completed work — re-send the original user message.
            # History contains the accepted content, including image bytes.
            # Keep it intact for the request, crash recovery, and failed replay.
            original_created_at = _existing_message_created_at(messages[user_idx])
            popped = messages.pop(user_idx)
            popped_item_id = read_analytics_item_id(popped.additional_properties)
            has_content = any(content.type != "text" or bool(content.text) for content in popped.contents)
            # Imported/programmatic input can contain only empty text blocks.
            # Keep its non-empty fallback without adding text to image-only input.
            text = user_text if has_content else "continue"
            contents = list(popped.contents) if has_content else [text]
            replay_msg = _replay_user_message(contents, original_created_at, item_id=popped_item_id)
            # Preserve the popped anchor's mid-turn flags: re-sending a
            # popped injection unflagged would launder it into a
            # turn-splitting opener.
            for key in HistoryMarkerKind.MID_TURN_USER_KEYS:
                if popped.additional_properties.get(key):
                    replay_msg.additional_properties[key] = popped.additional_properties[key]
            # The replay is the same message re-sent: it renders the reminders
            # the original carried, not a fresh set (after a restart the turn's
            # reminders can no longer be rebuilt byte-identically).
            copy_reminder_record(popped.additional_properties, replay_msg.additional_properties)
            self.input_properties = replay_msg.additional_properties
            replay_kind: UserMessageKind = (
                "injected" if popped.additional_properties.get(HistoryMarkerKind.INJECTED_KEY) else "opener"
            )
            # The pop destructively removed the anchor from state while the
            # runner already cleared the recovery current input — until
            # ``after_run`` stores the input again, the replayed message
            # exists nowhere durable. Register it (kind included) so a
            # crash-recovery checkpoint can re-create it.
            if self.recovery_input_recorder is not None:
                self.recovery_input_recorder(text, contents, original_created_at, kind=replay_kind)
            yield self.continuation_request([replay_msg])

            # If the run failed, ensure the user message wasn't lost.
            # Scope the dedup to the current turn region (messages after
            # the last ``_chrys_kind='turn'`` marker) — mirrors the fix
            # in :meth:`SessionHistoryManager.ensure_user_message`.  A
            # global scan would be fooled by a same-text user message
            # from an earlier turn (e.g. an injection that got anchored
            # into turn 1 carries the same text the user later types
            # as a fresh turn's prompt) and drop the re-append, losing
            # this turn's prompt on interrupt-before-persist.
            # The dedup and the fallback append are both keyed to the
            # popped anchor's kind: a same-text flagged injection must not
            # suppress a popped opener's re-append, and a popped injection
            # dedups against its persisted flagged copy — and is re-created
            # FLAGGED when missing, never laundered into an opener.
            if self.state.run_failed or self.state.was_interrupted:
                messages = state.get("messages", [])
                start = current_turn_start(messages)
                has_user_msg = any(user_text_matches(m, text, kind=replay_kind) for m in messages[start:])
                if not has_user_msg:
                    fallback_msg = _replay_user_message(contents, original_created_at, item_id=popped_item_id)
                    if replay_kind == "injected":
                        fallback_msg.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
                    copy_reminder_record(replay_msg.additional_properties, fallback_msg.additional_properties)
                    messages.append(fallback_msg)
