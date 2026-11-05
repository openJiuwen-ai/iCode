# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SessionHistoryManager — single owner of all ``chrys_history`` state mutations.

Centralizes the 16+ history-mutation methods that were scattered across
AgentEngine.  All marker insertion, message trimming, metadata persistence,
and state repair operations go through this class.

The manager holds a reference to the live ``chrys_history`` provider state
dict (shared with CompressibleHistoryProvider). Mutations
are visible to Agent's history provider immediately.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import (
    ANSWER_SEGMENT_KEY,
    AWAITING_SUB_AGENTS_MESSAGE,
    EXECUTION_INTERRUPTED_MESSAGE,
    HistoryMarkerKind,
    copy_reminder_record,
)
from chrys.foundation.models.turns import (
    UserMessageKind,
    current_turn_start,
    is_continuation_message,
    user_text_matches,
)
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.text.images import LoadedImage, data_uri_payload_info
from chrys.foundation.tool_invocation_order import read_tool_invocation_order
from chrys.foundation.tool_kinds import KIND_SUB_AGENT
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id
from chrys.kernel import TOOL_CALL_CONTENT_TYPES, TOOL_RESULT_CONTENT_TYPES, Content, Message
from chrys.kernel.exchanges import (
    PREVALIDATION_ERROR_KINDS,
    EmptyIdPolicy,
    LiveAccessor,
    NoneIdPolicy,
    PairingPolicy,
    iter_exchanges,
    pair_results,
)
from chrys.kernel.images import is_image_content
from chrys.service.agent_middleware.events.tool_events import ToolBatchRecord
from chrys.service.agent_middleware.injection import ConsumedInjection
from chrys.service.session.message_metadata import (
    TOOL_RESULT_METADATA_KEY,
    normalize_created_at,
    persisted_tool_call_kind,
    stamp_message_created_at,
    try_normalize_created_at,
)
from chrys.service.session.sub_agent_logs import SUB_AGENT_RESTORE_CONSUMED_KEY
from chrys.service.trajectory.items import ensure_history_item_ids
from chrys.service.vision import invalidate_image_token_count

if TYPE_CHECKING:
    from chrys.kernel.exchanges import Exchange, ExchangePairing, Occurrence, PairingKey

logger = logging.getLogger(__name__)


class MergeableLoopRecorder(Protocol):
    """Recorder view consumed by history recovery."""

    @property
    def initial_count(self) -> int | None: ...

    @property
    def captured_count(self) -> int | None: ...

    @property
    def loop_messages(self) -> list[Message] | None: ...


_USER_APPROVAL_STATUSES = frozenset({"user_approved", "user_rejected"})


def _select_message_approval_from_contents(message: Message) -> dict[str, str] | None:
    """Return the best legacy message-level approval from function-call contents."""
    selected: dict[str, str] | None = None
    for content in message.contents:
        if content.type != "function_call" or content.informational_only:
            continue
        approval = content.additional_properties.get("_approval")
        if not isinstance(approval, dict):
            continue
        if selected is None or (
            selected.get("status") not in _USER_APPROVAL_STATUSES and approval.get("status") in _USER_APPROVAL_STATUSES
        ):
            selected = approval
    return selected


def _sync_message_approval_from_contents(message: Message) -> None:
    """Refresh legacy message-level approval after function-call contents change."""
    approval = _select_message_approval_from_contents(message)
    if approval is None:
        message.additional_properties.pop("_approval", None)
    else:
        message.additional_properties["_approval"] = approval


def _sub_agent_result_metadata(record: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    invocation_id = record.get("invocation_id")
    if isinstance(invocation_id, str) and invocation_id:
        metadata["sub_agent_invocation_id"] = invocation_id
    log_file = record.get("sub_agent_log_file")
    if isinstance(log_file, str) and log_file:
        metadata["sub_agent_log_file"] = log_file
    return metadata


def _content_is_image(content: Any) -> bool:
    media_type = content.media_type
    return isinstance(media_type, str) and media_type.startswith("image/")


def _message_has_image_content(message: Message) -> bool:
    return any(_content_is_image(content) for content in message.contents)


def _contents_have_image_content(contents: list[Any]) -> bool:
    return any(not isinstance(content, str) and _content_is_image(content) for content in contents)


@dataclass(frozen=True)
class OversizedImage:
    """An inline image of the history over a send limit, with the payload it was read with."""

    message: Message
    content: Content
    uri: str


def _inline_image_over(content: Content, max_bytes: int) -> bool:
    uri = content.uri
    if content.type != "data" or not isinstance(uri, str) or not uri.startswith("data:"):
        return False
    if not is_image_content(content):
        return False
    # Base64 takes four characters for every three bytes: most images are
    # told apart by the length of their URI alone.
    if len(uri) * 3 // 4 <= max_bytes:
        return False
    size, _payload = data_uri_payload_info(uri)
    return size is not None and size > max_bytes


# Trim's pairing row: actionable local function calls only (hosted
# informational calls have no function_result by design), function results
# only, id-less calls answered solely by id-less PREVALIDATION failures
# (complete-or-delete fails closed), empty-id calls paired positionally,
# and malformed non-string ids normalized by their string form.
_TRIM_PAIRING_POLICY = PairingPolicy(
    call_types=frozenset({"function_call"}),
    include_informational_calls=False,
    result_types=frozenset({"function_result"}),
    none_id=NoneIdPolicy.POSITIONAL_FAILURES,
    empty_id=EmptyIdPolicy.POSITIONAL,
    malformed_id="stringify",
)

# Approval's pairing row: local actionable calls only; id-less calls and
# results are UNPAIRABLE occurrences — visible to eligibility and the
# block-wide falsy failure boolean without consuming pair slots; malformed
# ids compare by string form at the pairing layer while every
# decision-matching stage keeps its raw reads.
_APPROVAL_PAIRING_POLICY = PairingPolicy(
    call_types=frozenset({"function_call"}),
    include_informational_calls=False,
    result_types=frozenset({"function_result"}),
    none_id=NoneIdPolicy.UNPAIRABLE_OCCURRENCE,
    empty_id=EmptyIdPolicy.UNPAIRABLE_OCCURRENCE,
    malformed_id="stringify",
)


def _approval_exclusions(
    messages: list[Message], exchange: Exchange, accessor: LiveAccessor
) -> tuple[set[PairingKey], bool]:
    """One exchange's never-approvable evidence, from the eligible view.

    Returns the truthy keys answered by an ELIGIBLE prevalidation-failure
    result, plus the block-wide falsy boolean: whether any eligible id-less
    failure occurrence exists in either falsy stream (stream-blind — an
    unpairable failure can only answer SOME id-less call of the block, so
    every id-less call fails closed).
    """
    pairing = pair_results(messages, exchange, accessor, _APPROVAL_PAIRING_POLICY)
    excluded_keys = {
        key
        for key, occurrences in pairing.eligible_results.truthy.items()
        if any(occurrence.error_kind in PREVALIDATION_ERROR_KINDS for occurrence in occurrences)
    }
    falsy_failure = any(
        occurrence.error_kind in PREVALIDATION_ERROR_KINDS
        for stream in pairing.eligible_results.falsy.values()
        for occurrence in stream
    )
    return excluded_keys, falsy_failure


# Sub-agent repair's pairing row: local function calls and results only.
# None-id calls are IGNORED (sub-agent calls carry minted persisted ids, so
# an id-less call is not repair's to answer); empty-id calls pair
# positionally against empty-id results; malformed ids pair by string form
# while record lookups and synthetic results keep the original value.
_REPAIR_PAIRING_POLICY = PairingPolicy(
    call_types=frozenset({"function_call"}),
    include_informational_calls=False,
    result_types=frozenset({"function_result"}),
    none_id=NoneIdPolicy.IGNORE,
    empty_id=EmptyIdPolicy.POSITIONAL,
    malformed_id="stringify",
)


def _repair_call_entries(pairing: ExchangePairing) -> list[tuple[Occurrence, object, bool]]:
    """One exchange's repairable call occurrences in transcript order.

    Returns ``(occurrence, truthy pairing key or None, answered)`` per call.
    Truthy keys are answered at KEY level (``answered_keys``): duplicate ids
    inside one response are shadowed by dispatch and one result answers
    every occurrence, so sibling duplicates are one dangling invocation.
    Empty-id calls are answered per POSITIONAL slot — each unanswered
    occurrence is its own dangling invocation.
    """
    answered_keys = pairing.answered_keys
    entries: list[tuple[Occurrence, object, bool]] = []
    for key, assignments in pairing.truthy_assignments.items():
        for call, _result in assignments:
            entries.append((call, key, key in answered_keys))
    for call, result in pairing.falsy_assignments["empty"]:
        entries.append((call, None, result is not None))
    entries.sort(key=lambda entry: (entry[0].message_index, entry[0].content_index))
    return entries


def _trim_exchange_siblings(messages: list[Message], exchange: Exchange, pairing: ExchangePairing) -> bool:
    """Trim one exchange's unanswered calls, sibling by sibling.

    Every response sibling is checked against the exchange-wide pairing
    (the shared output block and embedded results answer any sibling's
    calls). Per sibling, the deletion branches mirror what trim has always
    applied to a single message: all answered → keep; partially answered →
    drop only the unanswered call contents; nothing answered → pop the
    whole message, unless a hosted informational call shields it into the
    surgical branch. A popped sibling's answer text stays, as a message of
    its own: the user saw it stream.

    Truthy ids are answered at KEY level: duplicate ids are shadowed by
    dispatch and one result answers every occurrence, so no duplicate of
    an answered id is ever trimmed. Falsy ids stay occurrence-level —
    each unanswered positional slot is its own dangling invocation.

    Returns True when any call-carrying sibling survives (complete,
    surgically trimmed, or shielded) — trim stops there; False when every
    call-carrying sibling was popped, letting the caller continue walking
    into earlier history exactly as the message-by-message walk did. A
    run head with no in-policy call occurrences (informational-only,
    out-of-scope call types, or plain text) also survives: the walk has
    always stopped at an assistant run it has no actionable calls in.
    """
    matched_by_sibling: dict[int, list[Occurrence]] = {}
    unmatched_by_sibling: dict[int, list[Occurrence]] = {}
    answered_keys = pairing.answered_keys
    for key, assignments in pairing.truthy_assignments.items():
        bucket = matched_by_sibling if key in answered_keys else unmatched_by_sibling
        for call, _result in assignments:
            bucket.setdefault(call.message_index, []).append(call)
    for assignments in pairing.falsy_assignments.values():
        for call, result in assignments:
            bucket = matched_by_sibling if result is not None else unmatched_by_sibling
            bucket.setdefault(call.message_index, []).append(call)

    head_index = exchange.response_indices[0]
    survived = head_index not in matched_by_sibling and head_index not in unmatched_by_sibling
    for sibling_index in reversed(exchange.response_indices):
        matched = matched_by_sibling.get(sibling_index, [])
        unmatched = unmatched_by_sibling.get(sibling_index, [])
        if not unmatched:
            if matched:
                survived = True
            continue
        message = messages[sibling_index]
        informational = any(c.type == "function_call" and c.informational_only for c in message.contents)
        if matched or informational:
            dropped = {occurrence.content_index for occurrence in unmatched}
            message.contents = [c for index, c in enumerate(message.contents) if index not in dropped]
            _sync_message_approval_from_contents(message)
            survived = True
            continue
        dropped = {occurrence.content_index for occurrence in unmatched}
        if any(
            c.type == "text" and (c.text or "").strip()
            for index, c in enumerate(message.contents)
            if index not in dropped
        ):
            # The kept text answers nothing, so the walk goes on as if the sibling was popped.
            message.contents = [c for index, c in enumerate(message.contents) if index not in dropped]
            _sync_message_approval_from_contents(message)
            continue
        messages.pop(sibling_index)
    return survived


def _effective_metadata_start_index(messages: list[Message], start_index: int | None) -> int:
    """Clamp a post-run metadata start index to the current logical turn."""
    if start_index is None or start_index < 0:
        start_index = 0
    elif start_index > len(messages):
        start_index = len(messages)

    # Tools like compress_context can remove earlier messages before
    # post-run metadata is persisted.  The previous turn marker is the
    # stable logical boundary for a normal run, so use it as a floor when
    # it lands before the numeric index.
    if start_index > 0:
        turn_start = current_turn_start(messages)
        if 0 < turn_start < start_index:
            start_index = turn_start
    return start_index


def stamp_history_item_ids(state: dict[str, Any]) -> None:
    """Give restored history occurrence identities before it becomes live."""
    messages = state.get("messages")
    if isinstance(messages, list) and messages:
        ensure_history_item_ids(messages)


class SessionHistoryManager:
    """Owns all mutations to the ``chrys_history`` session state dict.

    Usage::

        history = SessionHistoryManager()
        stamp_history_item_ids(executor.history_state)
        history.bind(executor.history_state)
        history.insert_turn_marker()
    """

    def __init__(self) -> None:
        self._state: dict[str, Any] | None = None

    @property
    def is_bound(self) -> bool:
        """Whether this manager has been bound to a state dict."""
        return self._state is not None

    def bind(self, state: dict[str, Any]) -> None:
        """Bind to a live ``chrys_history`` state dict.

        Called after agent construction.  The manager mutates this dict
        directly — CompressibleHistoryProvider sees the changes immediately.

        The caller stamps restored history before binding it. Binding itself
        only replaces the live state reference.
        """
        self._state = state

    @property
    def state(self) -> dict[str, Any]:
        """The full ``chrys_history`` state dict.

        Raises ``RuntimeError`` if not yet bound.
        """
        if self._state is None:
            raise RuntimeError("SessionHistoryManager not bound — call bind() first")
        return self._state

    @property
    def messages(self) -> list:
        """The active message list (may be empty)."""
        return self.state.get("messages", [])

    def get_deep_copy(self) -> dict[str, Any] | None:
        """Return a deep copy of the current state, or ``None`` if empty."""
        if self._state is None:
            return None
        raw = self._state
        return copy.deepcopy(raw) if raw else None

    # ------------------------------------------------------------------
    # Marker operations
    # ------------------------------------------------------------------

    def insert_turn_marker(self, started_at: datetime | str | None = None) -> None:
        """Insert a turn marker at the end of history, with when its pass began (*started_at*) if known."""
        state = self._state
        if state is None or not state.get("messages"):
            return
        from chrys.service.context.providers.history import CompressibleHistoryProvider

        turn_counter = state.get("turn_counter", 0) + 1
        CompressibleHistoryProvider.insert_marker(state, turn_counter, started_at=try_normalize_created_at(started_at))

    def insert_interrupted_marker(
        self,
        reason: str | None = None,
        source: str = "user",
        status_code: str | None = None,
    ) -> None:
        """Insert an interruption marker so it replays on session restore."""
        if reason is None:
            reason = format_message(EXECUTION_INTERRUPTED_MESSAGE.bind())
            status_code = HistoryMarkerKind.STATUS_EXECUTION_INTERRUPTED
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        marker = Message("assistant", [reason])
        marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
        marker.additional_properties["_interrupted_by"] = source
        ensure_analytics_item_id(marker.additional_properties)
        if status_code is not None:
            marker.additional_properties[HistoryMarkerKind.STATUS_CODE_KEY] = status_code
        insert_at = len(messages)
        while insert_at > 0:
            kind = messages[insert_at - 1].additional_properties.get(HistoryMarkerKind.KEY)
            if kind != HistoryMarkerKind.TURN:
                break
            insert_at -= 1
        messages.insert(insert_at, marker)

    def trailing_status_marker(self) -> tuple[str, str] | None:
        """Return the trailing status marker ``(kind, source)`` if present.

        Used to detect restored sessions that ended with a failure,
        where the FSM has been reset to IDLE but the history still
        carries the error state.  ``awaiting_sub_agents`` counts as an
        error marker too: a session that reloaded while sub-agents were
        paused should be treated the same as an interrupted run for the
        purposes of the user's next message.

        Structural turn markers at the tail are skipped.  ``source`` is the
        marker's ``_interrupted_by`` value when present; non-interrupted
        status markers return an empty source.
        """
        state = self._state
        if state is None:
            return None
        messages = state.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        # SessionHistoryManager is the sole writer and stores kernel Messages.
        typed_messages = cast("list[Message]", messages)
        for m in reversed(typed_messages):
            chrys_kind = m.additional_properties.get(HistoryMarkerKind.KEY)
            if chrys_kind == HistoryMarkerKind.TURN:
                continue
            if chrys_kind not in HistoryMarkerKind.STATUS_MARKERS:
                return None
            source = m.additional_properties.get("_interrupted_by", "")
            return chrys_kind, source if isinstance(source, str) else ""
        return None

    def has_trailing_error_markers(self) -> bool:
        """Check if history ends with error/interrupted markers."""
        return self.trailing_status_marker() is not None

    def remove_trailing_markers(self) -> None:
        """Remove interrupted/error/awaiting markers and turn markers from end of history.

        Every trailing ``turn`` marker popped also rolls
        ``state["turn_counter"]`` back by one.  Without that, a
        subsequent :meth:`insert_turn_marker` (which computes the next
        index as ``turn_counter + 1``) would advance past the slot we
        just freed, desynchronising ``turn_counter`` from the number of
        markers actually present in history.  That desync surfaces most
        visibly in the retry flow: ``_on_user_retry`` calls this method
        to drop the failed turn's marker before ``_post_run`` re-inserts
        one on successful resume, and before the fix the restored-session
        counter would read ``N + 1`` for an ``N``-turn conversation
        (breaking the rollback picker's "Turn N (Current)" sentinel).
        Turn markers are inserted sequentially, so decrementing per
        removed marker keeps ``turn_counter`` aligned with ``max(_turn)``
        of the surviving markers.
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        # SessionHistoryManager is the sole writer and stores kernel Messages.
        typed_messages = cast("list[Message]", messages)
        while typed_messages:
            chrys_kind = typed_messages[-1].additional_properties.get(HistoryMarkerKind.KEY)
            if chrys_kind in HistoryMarkerKind.SESSION_COUNT_EXCLUDED:
                if chrys_kind == HistoryMarkerKind.TURN:
                    state["turn_counter"] = max(0, state.get("turn_counter", 0) - 1)
                typed_messages.pop()
            else:
                break

    # ------------------------------------------------------------------
    # Sub-agent pause marker
    # ------------------------------------------------------------------

    def upsert_awaiting_sub_agents_marker(self, invocation_ids: list[str]) -> None:
        """Ensure history carries an ``awaiting_sub_agents`` marker listing ``invocation_ids``.

        Idempotent — if a marker already exists (as the last non-turn
        message), its ``_invocation_ids`` list is updated in place.
        Otherwise a fresh marker is appended.  Empty ``invocation_ids``
        removes any existing marker (use :meth:`remove_awaiting_sub_agents_marker`
        for that explicitly — this is defensive).
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        # SessionHistoryManager is the sole writer and stores kernel Messages.
        typed_messages = cast("list[Message]", messages)

        if not invocation_ids:
            self.remove_awaiting_sub_agents_marker()
            return

        # Find an existing marker to update in place.
        for m in reversed(typed_messages):
            kind = m.additional_properties.get(HistoryMarkerKind.KEY)
            if kind == HistoryMarkerKind.TURN:
                continue
            if kind == HistoryMarkerKind.AWAITING_SUB_AGENTS:
                m.additional_properties["_invocation_ids"] = list(invocation_ids)
                m.additional_properties[HistoryMarkerKind.STATUS_CODE_KEY] = (
                    HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS
                )
                return
            break

        marker = Message(
            "assistant",
            [format_message(AWAITING_SUB_AGENTS_MESSAGE.bind(count=len(invocation_ids)))],
        )
        marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.AWAITING_SUB_AGENTS
        marker.additional_properties[HistoryMarkerKind.STATUS_CODE_KEY] = HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS
        marker.additional_properties["_invocation_ids"] = list(invocation_ids)
        ensure_analytics_item_id(marker.additional_properties)
        messages.append(marker)

    def remove_awaiting_sub_agents_marker(self) -> None:
        """Strip every ``awaiting_sub_agents`` marker from history.

        Called when the paused-invocation set empties (last sub-agent
        resolved) or when the parent run transitions past AWAITING_SUB_AGENTS
        into IDLE/INTERRUPTED/FAILED (the marker is superseded by those
        terminal markers).
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        state["messages"] = [
            m
            for m in messages
            if m.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.AWAITING_SUB_AGENTS
        ]

    def remove_orphaned_user_message(self) -> None:
        """Remove a trailing user message that received no model response.

        After ``remove_trailing_markers()`` strips error/turn markers, the
        last message may be a user message with zero assistant or tool
        responses after it.  This means the model produced nothing before
        failing (e.g. 401 auth error).  Removing it keeps the persisted
        history consistent with what the user saw during the live session
        — the failed message disappeared when they typed a new one.

        If the model DID produce output (tool calls, partial response)
        before failing, the user message and model output are preserved.
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list) or not messages:
            return
        if messages[-1].role == "user":
            logger.debug("remove_orphaned_user_message: removing '%s'", (messages[-1].text or "")[:60])
            messages.pop()

    def remove_all_status_markers(self) -> None:
        """Remove ALL interrupted/error/awaiting-sub-agents markers from history.

        Covers every ephemeral status marker class.  ``turn`` markers are
        structural (they drive compression boundaries) and stay put.
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        state["messages"] = [
            m
            for m in messages
            if m.additional_properties.get(HistoryMarkerKind.KEY) not in HistoryMarkerKind.STATUS_MARKERS
        ]

    def keep_partial_answer(self, contents: Sequence[Content]) -> None:
        """Append the answer a failed pass showed for a response history does not hold.

        The user saw it, so the saved turn keeps it, and the model reads it
        when the user goes on with a message. A response that landed as the
        pass ended may already be in history: contents it holds are skipped.
        Each segment it was shown in stays one text content with its stamp,
        so a later bare Continue can name what it drops.
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        held = {id(content) for message in messages for content in message.contents}
        segments: list[tuple[object, list[str]]] = []
        for content in contents:
            if id(content) in held or not content.text:
                continue
            segment = content.additional_properties.get(ANSWER_SEGMENT_KEY)
            if not segments or segments[-1][0] != segment:
                segments.append((segment, []))
            segments[-1][1].append(content.text)
        kept = [
            Content.from_text(text, additional_properties={ANSWER_SEGMENT_KEY: segment} if segment else None)
            for segment, parts in segments
            if (text := "".join(parts)).strip()
        ]
        if not kept:
            return
        message = Message("assistant", kept)
        ensure_analytics_item_id(message.additional_properties)
        messages.append(message)

    def remove_trailing_answer(self) -> tuple[str, ...]:
        """Drop the answer a failed or interrupted pass left at the end of history; name its segments.

        A Continue asks for that answer again, and its request must not end
        with assistant text, which some providers read as a prefill to extend.
        Tool exchange work is never dropped: of a message whose exchange
        ends with an answer, only the contents after its last tool content
        go. Returns the segment ids of the text dropped, which frontends
        withdraw; text written before segments were stamped has none.

        The request still ends with assistant text where its codec writes the
        kept hosted exchange as a text summary (Chat Completions, foreign
        hosted history, Responses image history under local storage).
        """
        state = self._state
        if state is None:
            return ()
        messages = state.get("messages")
        if not isinstance(messages, list):
            return ()
        dropped: list[Content] = []
        while messages and _is_answer_message(messages[-1]):
            dropped[:0] = messages.pop().contents
        if messages and (split := _split_exchange_answer(messages[-1])):
            # The landed wrapper may be held elsewhere (a retry snapshot): replace it, never trim it in place.
            trimmed = copy.copy(messages[-1])
            trimmed.contents, tail = split
            trimmed.additional_properties = dict(messages[-1].additional_properties)
            messages[-1] = trimmed
            dropped[:0] = tail
        segments = (
            content.additional_properties.get(ANSWER_SEGMENT_KEY)
            for content in dropped
            if content.type == "text" and (content.text or "").strip()
        )
        return tuple(dict.fromkeys(segment for segment in segments if isinstance(segment, str) and segment))

    # ------------------------------------------------------------------
    # Message repair
    # ------------------------------------------------------------------

    def ensure_user_message(
        self,
        text: str,
        created_at: datetime | str | None = None,
        contents: list[Any] | None = None,
        *,
        kind: UserMessageKind = "opener",
        item_id: str | None = None,
        reminder_source: Mapping[str, Any] | None = None,
    ) -> None:
        """Ensure the user message is present in session state.

        When Agent.run errors before HistoryProvider.after_run fires, the user
        message is never stored.  This adds it so ``resume()`` can find it.

        With ``SystemReminderMiddleware``, session state messages are always
        clean (no ``<system-reminder>`` tags), so plain text comparison is
        sufficient for dedup.  When the caller supplies multimodal contents,
        preserve them so retry/follow-up validation can still tell that the
        turn included image bytes.

        Dedup is scoped to the **current turn region** — messages after
        the last ``_chrys_kind='turn'`` marker.  Without scoping, a
        same-text user message from an earlier turn (or an injection
        anchored into one) would make the fallback skip the append and
        lose this turn's user message on interrupt-before-persist.
        Real-world repro: user typed "what time is it?" as an injection
        during turn 1, later typed the same text again as a fresh
        turn-3 user message and interrupted within 1 s → framework
        hadn't stored it yet → fallback saw the earlier injection →
        no append → saved state had interrupted + turn markers for
        turn 3 but no user message, so reload dropped the prompt.
        See ``test_ensure_user_message_appends_when_text_matches_earlier_turn``.

        Dedup is also **kind-aware**: a candidate matches only when text AND
        *kind* agree (``user_text_matches``) — retry guidance (``"injected"``)
        worded identically to the turn opener must append a second, flagged
        message rather than silently dedup against the opener (and vice
        versa).  The matched message's flags are never backfilled: stamping
        the real opener ``_injected`` would stop it opening the turn.
        Recovery never synthesizes a ``"continuation"`` kind — bare-resume
        nudge branches clear the recovery input instead.

        *reminder_source* is the properties of the copy the request sent: an
        appended message carries its reminder record, so later requests
        re-render the reminders it went out with.  A matched message keeps
        its own record.
        """
        state = self._state
        if state is None:
            return
        messages = state.setdefault("messages", [])
        start = current_turn_start(messages)
        for m in messages[start:]:
            if user_text_matches(m, text, kind=kind):
                stamp_message_created_at(m, created_at)
                ensure_analytics_item_id(m.additional_properties, item_id=item_id)
                if (
                    contents is not None
                    and _contents_have_image_content(contents)
                    and not _message_has_image_content(m)
                ):
                    m.contents = Message("user", contents).contents
                return
        msg = Message("user", contents if contents is not None else [text])
        if kind == "injected":
            msg.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
        stamp_message_created_at(msg, created_at)
        ensure_analytics_item_id(msg.additional_properties, item_id=item_id)
        copy_reminder_record(reminder_source, msg.additional_properties)
        messages.append(msg)

    def tag_last_user_message(self, key: str, value: Any) -> None:
        """Set an ``additional_properties`` key on the last user message.

        Deliberately the LITERAL last user message, whatever its kind
        (§2.3): with mid-turn injections that is the last injection — the
        message actually on the wire.  Flagged legacy ``continue`` nudges
        are the one exception: the provider filters them off the wire and
        replay hides them before reading metadata, so a tag written there
        would vanish from every consumer.
        """
        if self._state is None:
            return
        for m in reversed(self._state.get("messages", [])):
            if m.role == "user" and not is_continuation_message(m):
                m.additional_properties[key] = value
                return

    def remove_continuation_message(self) -> None:
        """Remove the synthetic ``continue`` nudge(s) injected by ``resume()``.

        Two passes over the current turn region (messages after the last
        ``_chrys_kind='turn'`` marker):

        1. Remove **all** ``_continuation``-flagged user messages, with no
           ``seen_work`` precondition.  The flag is authoritative — every
           flagged message is a synthetic placeholder by construction — and
           first-only would strand a crash-leftover nudge (a pass that
           persisted its nudge but died before cleanup).  This also covers
           the no-prior-user-message synthesis, where nothing precedes the
           nudge in its region.
        2. Legacy fallback, run **unconditionally** after pass 1: the
           ``text == "continue"`` + ``seen_work`` match for pre-flag
           histories, skipping ``_injected``-flagged messages — a user-typed
           literal ``continue`` follow-up must never be popped in place of a
           synthetic nudge.  Gating pass 2 on "pass 1 removed nothing" would
           strand the legacy nudge of a MIXED region (pre-upgrade unflagged
           leftover + new flagged nudge).  Always running is safe:
           ``seen_work`` requires work *before* the match, so an opener
           whose text is literally ``continue`` is untouchable.

        Scoping matters: a user may legitimately type ``"continue"``
        as a nudge mid-turn in an earlier turn (e.g. "explain X" →
        partial response → user types "continue" → assistant
        finishes).  A global forward scan would trip ``seen_work`` on
        that earlier turn's assistant message and pop the earlier
        nudge — losing it and leaving the current turn's injected
        ``"continue"`` in history forever.  See
        ``test_remove_continuation_message_scopes_to_current_turn``.
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        start = current_turn_start(messages)
        for i in range(len(messages) - 1, start - 1, -1):
            if is_continuation_message(messages[i]):
                messages.pop(i)
        seen_work = False
        for i in range(start, len(messages)):
            msg = messages[i]
            if msg.role in ("assistant", "tool"):
                seen_work = True
            elif (
                msg.role == "user"
                and seen_work
                and (msg.text or "").strip() == "continue"
                and not msg.additional_properties.get(HistoryMarkerKind.INJECTED_KEY)
            ):
                messages.pop(i)
                return

    def merge_loop_messages(self, loop_recorder: MergeableLoopRecorder, *, insert_index: int | None = None) -> None:
        """Recover completed tool loop iterations into session state.

        When the tool loop terminates early (interrupt or error),
        ``after_run`` only stores the last iteration's messages.
        ``LoopRecorder`` snapshots the accumulated messages before each
        LLM call.  This method inserts them at the correct position.

        ``insert_index`` is the caller's pass-start boundary (where this
        run's output chronologically belongs). It beats the last-user-message
        anchor when retained work from an earlier same-turn pass follows that
        anchor: after an empty retry the synthetic continuation was already
        removed, so anchoring to the original user message alone would insert
        this pass BEFORE the earlier pass's retained messages, inverting the
        transcript. The anchor still wins when it sits later (e.g. a mid-run
        injection user message persisted after the boundary).
        """
        loop_msgs = loop_recorder.loop_messages
        if not loop_msgs:
            logger.debug(
                "merge_loop_messages: no loop messages (initial_count=%s, captured=%s)",
                loop_recorder.initial_count,
                loop_recorder.captured_count,
            )
            return

        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list) or not messages:
            return

        logger.debug(
            "merge_loop_messages: %d loop messages to merge, %d existing messages",
            len(loop_msgs),
            len(messages),
        )

        # A recovery checkpoint may already have replayed a clean injection
        # that LoopRecorder also captured for the next tool request. Drop only
        # exact consumption-id duplicates; same-text distinct injections stay.
        current_region = messages[current_turn_start(messages) :]
        persisted_injection_ids = {
            injection_id
            for message in current_region
            if isinstance(
                injection_id := message.additional_properties.get(HistoryMarkerKind.INJECTION_ID_KEY),
                str,
            )
        }
        if persisted_injection_ids:
            loop_msgs = [
                message
                for message in loop_msgs
                if message.additional_properties.get(HistoryMarkerKind.INJECTION_ID_KEY) not in persisted_injection_ids
            ]
            if not loop_msgs:
                return

        # Check if loop messages are already present (normal completion path).
        first_loop = loop_msgs[0]
        for m in messages:
            if m is first_loop:
                logger.debug("merge_loop_messages: already present (identity match), skipping")
                return

        # Find the last user message — loop messages go right after it.
        user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "user":
                user_idx = i
                break

        if user_idx < 0:
            logger.debug("merge_loop_messages: no user message found")
            return

        insert_at = user_idx + 1
        if insert_index is not None:
            insert_at = max(insert_at, min(insert_index, len(messages)))
        for j, lm in enumerate(loop_msgs):
            messages.insert(insert_at + j, lm)
        logger.debug(
            "merge_loop_messages: inserted %d messages at index %d, total now %d",
            len(loop_msgs),
            insert_at,
            len(messages),
        )

    def inject_error_results_for_sub_agents(
        self,
        records: list[dict[str, Any]],
        sub_agent_tool_names: set[str],
    ) -> int:
        """Insert synthetic error tool-results for dangling sub-agent function_calls.

        ``records`` is a list of serialized paused-sub-agent dicts
        (as produced by :meth:`SubAgentSessionArtifactService.drain_paused_records`) and
        ``sub_agent_tool_names`` is the current registry of sub-agent
        tool names.  For each assistant message with unmatched
        sub-agent function_calls (no corresponding ``function_result``),
        we append a tool message carrying an ``Error:`` result.

        **Pairing policy** (two-pass):

        1. Prefer direct pairing by ``parent_call_id`` — records that
           carry it identify the exact framework call_id of the
           assistant function_call that invoked the sub-agent, so we
           can reattach each record to its own call even when the same
           sub-agent tool was invoked concurrently.
        2. Fall back to tool_name + appearance order for records
           without a usable ``parent_call_id`` (older saved sessions
           or edge cases where the field is empty).

        Dangling calls that have no matching record (e.g. profile
        removed the sub-agent) get a generic error so history stays
        well-formed.  Returns the number of error results injected.
        """
        state = self._state
        if state is None:
            return 0
        messages = state.get("messages")
        if not isinstance(messages, list) or not messages:
            return 0
        record_tool_names: set[str] = set()
        for rec in records:
            name = rec.get("tool_name")
            invocation_id = rec.get("invocation_id")
            if isinstance(name, str) and name and isinstance(invocation_id, str) and invocation_id:
                record_tool_names.add(name)
        candidate_tool_names = set(sub_agent_tool_names) | record_tool_names

        def _is_sub_agent_call(content: Any) -> bool:
            # Match by current/record tool_name OR by the call's OWN persisted
            # kind marker. A profile change can drop the tool from the registry
            # AND drain can lose every record (over-cap/corrupt/missing), yet
            # the persisted function_call still self-identifies as a sub-agent
            # via ``_chrys_tool_kind``. Repair it regardless, or the parent
            # assistant call is left dangling and the next provider request
            # rejects the history. This is why there is no early return on an
            # empty ``candidate_tool_names`` — kind-marked calls still qualify.
            if content.name in candidate_tool_names:
                return True
            return persisted_tool_call_kind(content.additional_properties) == KIND_SUB_AGENT

        # Import locally — ``Content`` is only needed here and keeping
        # it out of the module-level import surface mirrors how other
        # methods pull in framework-internal content helpers.
        # Two pools so pass-1 (by parent_call_id) and pass-2 (by
        # tool_name appearance order) don't trip each other.  A record
        # with a usable parent_call_id goes into ``by_call_id`` only;
        # records without it fall through to the ordered pool.
        from collections import defaultdict, deque

        from chrys.kernel import Content

        by_call_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
        name_pool: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        for rec in records:
            parent_call_id = rec.get("parent_provider_call_id") or rec.get("parent_call_id") or ""
            if parent_call_id:
                by_call_id[parent_call_id].append(rec)
            else:
                name_pool[rec.get("tool_name", "")].append(rec)

        # Materialize exchange boundaries and pairings once; injections are
        # queued and inserted after the scan, so coordinates stay valid.
        accessor = LiveAccessor()
        exchange_list = list(iter_exchanges(messages, accessor))
        entries_per_exchange = [
            _repair_call_entries(pair_results(messages, exchange, accessor, _REPAIR_PAIRING_POLICY))
            for exchange in exchange_list
        ]

        # Count distinct unresolved exchanges per ORIGINAL call id, not raw
        # call occurrences (sibling duplicates in one exchange are one
        # dangling invocation). Reuse across separate exchanges remains
        # ambiguous and must continue to fail closed. Unhashable originals
        # are skipped — they can never match a record-keyed direct lookup.
        dangling_exchanges: dict[Any, set[int]] = defaultdict(set)
        for position, entries in enumerate(entries_per_exchange):
            for call, key, answered in entries:
                if answered or key is None:
                    continue
                content = messages[call.message_index].contents[call.content_index]
                if not _is_sub_agent_call(content) or not isinstance(content.call_id, Hashable):
                    continue
                dangling_exchanges[content.call_id].add(position)
        dangling_call_counts = {call_id: len(positions) for call_id, positions in dangling_exchanges.items()}

        def _format_err(name: str, record: dict[str, Any] | None) -> str:
            if record is None:
                msg = f"Error: sub-agent '{name}' was paused when the session was saved; state discarded on reload."
            else:
                last_error = record.get("last_error") or "unknown"
                msg = (
                    f"Error: sub-agent '{name}' was paused when the session was saved; "
                    f"state discarded on reload — {last_error}"
                )
            # This string is injected into provider-visible repaired history; a restored
            # pending record can carry a lone surrogate (round-tripped from an owner-valid
            # audit) that the LLM request serializer's strict encode would crash on. The
            # file sink cannot cover a network encode, so neutralize at the interpolation.
            return surrogate_safe_text(msg)

        injections: list[tuple[int, Message]] = []  # (insert_before_index, msg)
        injected = 0
        for position, exchange in enumerate(exchange_list):
            # Every synthetic result lands at the exchange's shared output
            # boundary, after the whole consecutive assistant run. Truthy
            # keys schedule at most once per exchange: queued injections do
            # not enter ``messages`` until after this scan, and sibling
            # duplicates of one id are a single dangling invocation.
            insert_at = exchange.output_indices[0] if exchange.output_indices else exchange.response_indices[-1] + 1
            scheduled_keys: set[object] = set()
            for call, key, answered in entries_per_exchange[position]:
                if answered or (key is not None and key in scheduled_keys):
                    continue
                content = messages[call.message_index].contents[call.content_index]
                if not _is_sub_agent_call(content):
                    continue
                original_id = content.call_id

                # Pass 1 — direct pair by parent_call_id, always through the
                # ORIGINAL persisted id. An unhashable original is an
                # unmatched lookup (never a normalized substitution) and
                # falls through to the name pool exactly as an unknown id.
                record = None
                direct_ambiguous = False
                direct_records = by_call_id.get(original_id) if isinstance(original_id, Hashable) else None
                if direct_records:
                    if len(direct_records) == 1 and dangling_call_counts.get(original_id, 0) == 1:
                        record = direct_records.pop()
                        by_call_id.pop(original_id, None)
                    else:
                        direct_ambiguous = True
                        logger.warning(
                            "Sub-agent reload found ambiguous parent_call_id=%s for '%s'; "
                            "injecting generic repair and preserving pending records.",
                            original_id,
                            content.name,
                        )
                # Pass 2 — fall back to name+order for records that
                # didn't carry a parent_call_id (legacy sessions).
                if record is None and not direct_ambiguous and name_pool.get(content.name):
                    record = name_pool[content.name].popleft()
                    logger.warning(
                        "Sub-agent reload paired '%s' via tool_name+order fallback "
                        "(call_id=%s); re-save the session to persist parent_call_id "
                        "for precise recovery on next reload.",
                        content.name,
                        original_id,
                    )

                err_text = _format_err(content.name, record)
                result = Content.from_function_result(call_id=original_id, result=err_text)
                if record is not None:
                    record[SUB_AGENT_RESTORE_CONSUMED_KEY] = True
                    metadata = _sub_agent_result_metadata(record)
                    if metadata:
                        result.additional_properties[TOOL_RESULT_METADATA_KEY] = metadata
                injections.append((insert_at, Message("tool", [result])))
                if key is not None:
                    scheduled_keys.add(key)
                injected += 1

        # Insert from the end so earlier indices stay valid. Every synthetic
        # result lands at the response's shared result-block boundary, after
        # all consecutive assistant siblings. Multiple results targeting the
        # same boundary retain appearance order through stable reverse
        # iteration and repeated insertion at that boundary.
        for insert_at, msg in reversed(injections):
            messages.insert(insert_at, msg)
        return injected

    def trim_to_last_complete_tool_results(self) -> None:
        """Trim messages to the last complete tool exchange on interrupt.

        Walks backwards to the last exchange (a run of assistant siblings
        plus the tool output answering it) and removes function calls the
        exchange never answered — checking EVERY sibling of the run against
        the shared output block and embedded results, per trim's pairing
        row. Exchanges whose call siblings all pop cascade the walk into
        earlier history; any surviving sibling stops it.
        """
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list) or not messages:
            return

        # Materialize boundaries and pairings BEFORE any mutation: the walk
        # only pops or edits messages at indices at-or-above everything it
        # still has left to visit, so earlier exchanges' coordinates stay
        # valid as it descends.
        accessor = LiveAccessor()
        exchange_list = list(iter_exchanges(messages, accessor))
        pairings = [pair_results(messages, exchange, accessor, _TRIM_PAIRING_POLICY) for exchange in exchange_list]
        owner_by_response_index = {
            index: position for position, exchange in enumerate(exchange_list) for index in exchange.response_indices
        }
        output_indices = {index for exchange in exchange_list for index in exchange.output_indices}

        i = len(messages) - 1
        while i >= 0:
            msg = messages[i]
            if msg.role != "assistant":
                i -= 1
                continue

            if HistoryMarkerKind.KEY in msg.additional_properties:
                # Assistant role alone does not make a message a response
                # sibling. In particular, the awaiting-sub-agents marker is
                # a deliberate shield for its dangling parent call until
                # reload repair can answer it.
                break

            position = owner_by_response_index.get(i)
            if position is None:
                if i in output_indices:
                    # A result-only assistant continuing an exchange's output
                    # block; the exchange is processed at its response run.
                    i -= 1
                    continue
                if (
                    i > 0
                    and messages[i - 1].role == "assistant"
                    and HistoryMarkerKind.KEY not in messages[i - 1].additional_properties
                ):
                    # A call-less assistant run is not a safe trim boundary
                    # while it may still lead back to a response's calls.
                    i -= 1
                    continue
                break

            exchange = exchange_list[position]
            if _trim_exchange_siblings(messages, exchange, pairings[position]):
                break
            i = exchange.response_indices[0] - 1

    # ------------------------------------------------------------------
    # Metadata persistence
    # ------------------------------------------------------------------

    def persist_batch_ids(self, batch_records: list[ToolBatchRecord]) -> dict[int, Message]:
        """Assign ``_batch_id`` to assistant messages by record identity.

        Matching is keyed by ``(provider_call_id, tool_name)`` — the
        persisted ``function_call.call_id`` IS the raw provider id — never
        by position: positional zips misattribute whenever a pre-run
        unbatched straggler, an interrupted pass, or mid-pass compression
        shifts the pairing.  Deliberately NO ``tool_order`` and NO chrys-id
        fallback (an order-only match hits a stale slot *uniquely*, and the
        chrys id may be a fresh uuid that matches nothing); a record
        without ``provider_call_id`` is dropped with a debug log — for
        batch attribution, loss beats misattribution.

        Duplicate keys are matched **bucketed, never per record**:
        sequential-id providers reset ids per response, so the same key
        legitimately repeats within one pass.  Records group per key in
        drain (chronological) order and un-batched slots per key in message
        order; for N records only the NEWEST N slots stay eligible (surplus
        oldest slots are stale stragglers and stay un-batched), then
        records assign chronologically within the bucket.  Slots are
        (message, function_call content) pairs; the first record assigned
        to a message writes ``_batch_id``, later records verify the id
        matches and no-op (debug log on mismatch, never overwrite).  The
        scan bound is the current turn region — identity matching is what
        makes the wide bound safe; a strict numeric floor would orphan
        same-pass pre-compression batches.

        Returns ``{batch_id: anchor message}`` — the first message of each
        batch in record (chronological) order — for
        :meth:`persist_intermediate_texts`.  Must be called before it.
        """
        anchors: dict[int, Message] = {}
        if not batch_records:
            return anchors
        state = self._state
        if state is None:
            return anchors
        messages = state.get("messages")
        if not isinstance(messages, list):
            return anchors

        record_buckets: dict[tuple[str, str], list[tuple[int, ToolBatchRecord]]] = {}
        for drain_idx, record in enumerate(batch_records):
            if not record.provider_call_id:
                logger.debug(
                    "persist_batch_ids: dropping record without provider_call_id (tool=%s order=%s batch=%s)",
                    record.tool_name,
                    record.tool_order,
                    record.batch_id,
                )
                continue
            record_buckets.setdefault((record.provider_call_id, record.tool_name), []).append((drain_idx, record))
        if not record_buckets:
            return anchors

        # Slots: (provider_call_id, tool_name) → messages in order, from
        # un-batched assistant function_call messages in the current turn
        # region.  The un-batched filter stays message-level.
        slot_buckets: dict[tuple[str, str], list[Message]] = {}
        start = current_turn_start(messages)
        for msg in messages[start:]:
            if msg.role != "assistant" or "_batch_id" in msg.additional_properties:
                continue
            for c in msg.contents:
                if c.type != "function_call" or c.informational_only or not c.call_id:
                    continue
                slot_buckets.setdefault((c.call_id, c.name or ""), []).append(msg)

        assignments: list[tuple[int, int, Message]] = []
        for key, keyed_records in record_buckets.items():
            slots = slot_buckets.get(key, [])
            if len(slots) < len(keyed_records):
                logger.debug(
                    "persist_batch_ids: %d record(s) for key %s found no slot",
                    len(keyed_records) - len(slots),
                    key,
                )
            eligible = slots[-len(keyed_records) :]
            # strict=False: a slot shortfall is tolerated (logged above) —
            # surplus records simply stay un-stamped.
            for (drain_idx, record), msg in zip(keyed_records, eligible, strict=False):
                assignments.append((drain_idx, record.batch_id, msg))

        # Stamp in global chronological (drain) order so the anchor of a
        # multi-message batch is the batch's FIRST message.
        for _drain_idx, batch_id, msg in sorted(assignments, key=lambda a: a[0]):
            existing = msg.additional_properties.get("_batch_id")
            if existing is None:
                msg.additional_properties["_batch_id"] = batch_id
            elif existing != batch_id:
                logger.debug(
                    "persist_batch_ids: batch id mismatch on message (existing=%s record=%s) — not overwritten",
                    existing,
                    batch_id,
                )
                continue
            anchors.setdefault(batch_id, msg)
        return anchors

    def persist_intermediate_texts(self, texts: dict[int, str], batch_anchors: dict[int, Message]) -> None:
        """Embed captured intermediate texts into their batches' anchor messages.

        ``texts`` maps ``batch_id → text`` recorded at capture time;
        ``batch_anchors`` is :meth:`persist_batch_ids`'s return value (which
        **must** run first).  Assignment is a pure ``batch_id`` lookup — the
        old last-user-message anchor and positional pairing are gone.  A
        text whose batch matched no message this pass (e.g. every call in
        the batch failed argument validation and produced no record) is
        DROPPED with a debug log, never positionally re-attached: loss
        beats misattribution.  Anchors also keep a fresh text off any
        stale same-id ``_batch_id`` from a restored session's earlier
        process, whose counter restarted.
        """
        if not texts:
            return
        for batch_id, text in sorted(texts.items()):
            if not text:
                continue
            msg = batch_anchors.get(batch_id)
            if msg is None:
                logger.debug(
                    "persist_intermediate_texts: dropping text for unmatched batch %s (%d chars)",
                    batch_id,
                    len(text),
                )
                continue
            # Match intermediate_text_signal(), which concatenates text
            # parts without separators before deciding whether a sidecar is
            # needed. Replay also recognizes this form for compatibility.
            existing_text = "".join((c.text or "") for c in msg.contents if c.type == "text")
            if existing_text == text:
                continue
            msg.additional_properties["_intermediate_text"] = text

    def persist_approval_decisions(
        self,
        decisions: list[dict[str, str]],
        *,
        start_index: int | None = None,
    ) -> None:
        """Tag assistant messages with approval decision info.

        Matches decisions to function-call contents by the kernel-stamped
        invocation ordinal first, then by persisted call_id, then by legacy
        tool-name order. Order matching is a pure stamp read — this method
        never counts function calls itself, so calls that failed pre-pipeline
        validation cannot shift later matches. Ordinals restart at zero for
        every kernel run and one logical turn can hold several runs (a
        retried pass after a failure, interrupt/resume), so the scan region
        can retain same-ordinal calls from an earlier pass — typically a
        pre-validation failure that never reached the approval middleware.
        Calls whose immediate results carry a pre-pipeline ``tool_error_kind``
        are excluded from every stage (they can never hold a decision);
        beyond that, decisions always describe the most recent pass, so each
        order decision attaches to the LAST stamp match, never the first. An
        unstamped content (legacy
        history) skips the order stage and falls through to the id/name
        stages. When ``start_index`` is supplied, matching is scoped to the
        current turn so older same-name tool calls cannot consume new
        approval decisions.
        """
        if not decisions:
            return
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return

        start_index = _effective_metadata_start_index(messages, start_index)

        usable_decisions = [decision for decision in decisions if decision.get("status") != "approval_pending"]
        if not usable_decisions:
            return

        # An unhashable original id stays OUT of the scoped set (a string
        # decision id could never equal it; including it would only crash
        # the build) — its call still matches by stamp or name below.
        scoped_call_ids = {
            c.call_id
            for msg in messages[start_index:]
            if msg.role == "assistant"
            for c in msg.contents
            if c.type == "function_call" and not c.informational_only and c.call_id and isinstance(c.call_id, Hashable)
        }
        used_decision_indexes: set[int] = set()
        fallback_cursor = 0

        def _decision_order(decision: dict[str, str]) -> int | None:
            raw = decision.get("tool_order")
            if raw is None:
                return None
            try:
                return int(raw)
            except ValueError:
                return None

        def _pop_by_call_id(call_id: str, tool_name: str) -> dict[str, str] | None:
            if not call_id:
                return None
            for idx, decision in enumerate(usable_decisions):
                if idx in used_decision_indexes:
                    continue
                if decision.get("call_id") == call_id and decision.get("tool_name") == tool_name:
                    used_decision_indexes.add(idx)
                    return decision
            return None

        def _fallback_eligible(decision: dict[str, str]) -> bool:
            if _decision_order(decision) is not None:
                return False
            call_id = decision.get("call_id", "")
            return not call_id or call_id not in scoped_call_ids

        def _pop_fallback(tool_name: str) -> dict[str, str] | None:
            nonlocal fallback_cursor
            while fallback_cursor < len(usable_decisions):
                idx = fallback_cursor
                decision = usable_decisions[idx]
                if idx in used_decision_indexes or not _fallback_eligible(decision):
                    fallback_cursor += 1
                    continue
                if decision.get("tool_name") != tool_name:
                    return None
                used_decision_indexes.add(idx)
                fallback_cursor += 1
                return decision
            return None

        def _approval_for(content: Content, decision: dict[str, str]) -> dict[str, str]:
            approval: dict[str, str] = {
                "tool_name": decision.get("tool_name", ""),
                "status": decision.get("status", ""),
                "request_id": decision.get("request_id", ""),
            }
            if decision.get("call_id") and decision["call_id"] == (content.call_id or ""):
                approval["call_id"] = decision["call_id"]
            if decision.get("reason"):
                approval["reason"] = decision["reason"]
            if decision.get("grant_ids"):
                approval["grant_ids"] = decision["grant_ids"]
            return approval

        def _apply_modified_args(content: Content, decision: dict[str, str]) -> bool:
            raw = decision.get("modified_args")
            if not raw:
                return False
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("persist_approval_decisions: invalid modified_args JSON for %s", content.call_id)
                return False
            if not isinstance(parsed, dict):
                return False
            content.arguments = parsed
            return True

        # Calls answered by a pre-pipeline validation failure (unparseable or
        # invalid arguments, unknown tool) never reached the approval
        # middleware, so they can never legitimately hold a decision —
        # exclude them from every matching stage, independent of message
        # order. The evidence is exchange-scoped (call ids are not globally
        # unique) and read from the ELIGIBLE pairing view, so embedded
        # failure results count while a marker- or foreign-role-carried
        # result never does.
        never_approvable_ids: set[int] = set()
        accessor = LiveAccessor()
        owner_by_response_index: dict[int, int] = {}
        exchange_exclusions: list[tuple[set[PairingKey], bool]] = []
        for position, exchange in enumerate(iter_exchanges(messages, accessor)):
            exchange_exclusions.append(_approval_exclusions(messages, exchange, accessor))
            for index in exchange.response_indices:
                owner_by_response_index[index] = position

        # Stage 1 — kernel-stamped ordinal matching. Attach each order
        # decision to the LAST eligible stamp match in the region, never the
        # first: an earlier same-turn pass can leave behind a same-ordinal
        # call of the same name (ordinals restart per kernel run, and a
        # retried or resumed turn retains the failed pass's completed
        # messages), and decisions always belong to the most recent pass.
        # Decision call ids cannot disambiguate here — they carry the
        # chrys-minted id while contents keep the provider wire id, two
        # namespaces that never compare equal.
        stamped_candidates: list[tuple[Message, Content, int]] = []
        for msg_idx in range(start_index, len(messages)):
            msg = messages[msg_idx]
            if msg.role != "assistant":
                continue
            position = owner_by_response_index.get(msg_idx)
            if position is not None:
                excluded_keys, falsy_failure = exchange_exclusions[position]
            else:
                excluded_keys, falsy_failure = set(), False
            for c in msg.contents:
                if c.type != "function_call" or c.informational_only:
                    continue
                if c.call_id is None:
                    # The kernel raises on a None call id before the
                    # middleware context is created, so such a call can never
                    # hold a decision. An EMPTY id is different: provider
                    # adapters mint "" for absent wire ids and those calls do
                    # reach approval — they stay eligible unless their block
                    # holds an unpairable id-less failure (below).
                    never_approvable_ids.add(id(c))
                    continue
                raw_id = c.call_id
                normalized_id = raw_id if isinstance(raw_id, str) else (str(raw_id) if raw_id else "")
                if ("call_id", normalized_id) in excluded_keys or (falsy_failure and not normalized_id):
                    never_approvable_ids.add(id(c))
                    continue
                if "_approval" not in c.additional_properties:
                    stamped = read_tool_invocation_order(c.additional_properties)
                    if stamped is not None:
                        stamped_candidates.append((msg, c, stamped))

        for idx, order_decision in enumerate(usable_decisions):
            order = _decision_order(order_decision)
            if order is None:
                continue
            for msg, content, stamped in reversed(stamped_candidates):
                if (
                    stamped == order
                    and (content.name or "") == order_decision.get("tool_name")
                    and "_approval" not in content.additional_properties
                ):
                    content.additional_properties["_approval"] = _approval_for(content, order_decision)
                    _apply_modified_args(content, order_decision)
                    _sync_message_approval_from_contents(msg)
                    used_decision_indexes.add(idx)
                    break

        for msg in messages[start_index:]:
            if len(used_decision_indexes) == len(usable_decisions):
                break
            if msg.role != "assistant":
                continue

            message_changed = False
            for c in msg.contents:
                if c.type != "function_call" or c.informational_only or id(c) in never_approvable_ids:
                    continue
                if "_approval" not in c.additional_properties:
                    decision = _pop_by_call_id(c.call_id or "", c.name or "")
                    if decision is None:
                        decision = _pop_fallback(c.name or "")
                    if decision is not None:
                        c.additional_properties["_approval"] = _approval_for(c, decision)
                        _apply_modified_args(c, decision)
                        message_changed = True
            if message_changed:
                # Preserve the legacy message-level field for existing
                # replay/read paths. Multi-call messages keep exact per-call
                # approvals on the contents above.
                _sync_message_approval_from_contents(msg)

    def backfill_missing_created_at(
        self,
        created_at: datetime | str | None = None,
        *,
        start_index: int | None = None,
    ) -> int:
        """Stamp missing timestamp metadata on current-run edge-case messages."""
        state = self._state
        if state is None:
            return 0
        messages = state.get("messages")
        if not isinstance(messages, list):
            return 0
        start_index = len(messages) if start_index is None else _effective_metadata_start_index(messages, start_index)
        timestamp = normalize_created_at(created_at)
        count = 0
        for msg in messages[start_index:]:
            if stamp_message_created_at(msg, timestamp):
                count += 1
        return count

    def images_over(self, max_bytes: int) -> list[OversizedImage]:
        """Return the inline images over *max_bytes* that every request sends again.

        Those are the images of user messages and of tool results. An image
        in a reply belongs to the provider's record of that reply and stays
        as it is.
        """
        oversized: list[OversizedImage] = []
        for message in self.messages:
            for content in message.contents:
                if message.role == "user" and _inline_image_over(content, max_bytes):
                    oversized.append(OversizedImage(message, content, content.uri))
                elif content.type == "function_result":
                    oversized.extend(
                        OversizedImage(message, item, item.uri)
                        for item in content.items or ()
                        if _inline_image_over(item, max_bytes)
                    )
        return oversized

    def shrink_images(self, shrunk: Sequence[tuple[OversizedImage, LoadedImage]]) -> int:
        """Write each shrunk image over the image it was made from, and return how many were written.

        The content object stays the same, as the anchors that match a
        request's messages back to the history go by it; an image whose
        content changed since it was read is left alone.
        """
        written = 0
        for image, replacement in shrunk:
            content = image.content
            if content.uri is not image.uri:
                continue
            encoded = base64.b64encode(replacement.data).decode("ascii")
            content.uri = f"data:{replacement.media_type};base64,{encoded}"
            content.media_type = replacement.media_type
            properties = content.additional_properties
            properties["width"] = replacement.width
            properties["height"] = replacement.height
            for key, value in (("media_type", replacement.media_type), ("size", replacement.size)):
                if key in properties:
                    properties[key] = value
            # The message's cached estimate counted the larger image.
            invalidate_image_token_count(image.message)
            written += 1
        return written

    def persist_consumed_injections(self, injections: list[ConsumedInjection]) -> None:
        """Insert consumed injection messages into session history.

        Each consumed injection records a text, timestamp, and ``anchor``
        is an :class:`InjectionAnchor` captured by :class:`InjectionMiddleware`
        at consumption time — describing the message the injection should
        land *after*.

        :meth:`InjectionAnchor.find_in` tries exact identity
        (``id(contents)``) and ``message_id`` first, then falls back to
        structural ``role`` + ``call_ids`` matching when message assembly
        has replaced the captured objects. If no key
        matches, fall back to appending at the end.
        """
        if not injections:
            return
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        current_region = messages[current_turn_start(messages) :]
        persisted_by_id = {
            consumption_id: message
            for message in current_region
            if isinstance(
                consumption_id := message.additional_properties.get(HistoryMarkerKind.INJECTION_ID_KEY),
                str,
            )
        }
        missing: list[ConsumedInjection] = []
        for injection in injections:
            existing = persisted_by_id.get(injection.consumption_id) if injection.consumption_id is not None else None
            if existing is None:
                missing.append(injection)
            else:
                _stamp_consumption(existing, injection)
        _insert_consumed_injection_messages(messages, missing)

    def replay_consumed_injections(self, injections: list[ConsumedInjection]) -> None:
        """Re-insert consumed injections missing from history (crash-recovery).

        Consumed injections normally reach history only at finalization
        (:meth:`persist_consumed_injections`); a recovery checkpoint built
        mid-run must replay them itself or a hard crash loses the injected
        user message entirely.  Dedup against copies already present in the
        current turn region is IDENTITY-first: an injection is a duplicate
        only when a region message carries its ``_injection_id`` stamp —
        text matching cannot tell a persisted copy of *this* consumption
        from a distinct earlier injection that happens to share the text
        (e.g. a resumed turn whose prior run persisted a same-text note),
        and dropping the new one loses user input.  Kind-aware text matching
        (``user_text_matches`` with ``kind="injected"``) remains only as the
        fallback for id-less legacy entries.  The survivors route through
        the same anchor-resolution + insertion routine as the finalizer
        path, preserving consumption order (an anchor miss appends at the
        end, which is still before the checkpoint's terminal markers —
        callers run this in the pre-shaping window).
        """
        if not injections:
            return
        state = self._state
        if state is None:
            return
        messages = state.get("messages")
        if not isinstance(messages, list):
            return
        start = current_turn_start(messages)
        region = messages[start:]
        region_injections_by_id = {
            injection_id: message
            for message in region
            if isinstance(
                injection_id := message.additional_properties.get(HistoryMarkerKind.INJECTION_ID_KEY),
                str,
            )
        }

        missing: list[ConsumedInjection] = []
        for injection in injections:
            if injection.consumption_id is not None:
                existing = region_injections_by_id.get(injection.consumption_id)
            else:
                existing = next(
                    (message for message in region if user_text_matches(message, injection.text, kind="injected")),
                    None,
                )
            if existing is None:
                missing.append(injection)
            else:
                _stamp_consumption(existing, injection)
        _insert_consumed_injection_messages(messages, missing)


def _stamp_consumption(message: Message, injection: ConsumedInjection) -> None:
    """Give a persisted copy of *injection* the identity, time and reminders its consumption had."""
    ensure_analytics_item_id(message.additional_properties, item_id=injection.analytics_item_id)
    if injection.created_at is not None:
        stamp_message_created_at(message, injection.created_at)
    copy_reminder_record(injection.wire_properties, message.additional_properties)


def _insert_consumed_injection_messages(messages: list[Message], injections: list[ConsumedInjection]) -> None:
    """Insert consumed-injection messages at their anchor positions.

    Shared core of :meth:`SessionHistoryManager.persist_consumed_injections`
    (finalizer path) and :meth:`~SessionHistoryManager.replay_consumed_injections`
    (crash-recovery checkpoint path) — both must place injections identically.

    Anchors resolve against the **pre-insertion** list (batch resolve), then
    insertions apply highest index first so earlier indices are not shifted.
    For equal indices (multiple injections sharing one anchor), LATER-queued
    inserts first — each insert pushes prior items down, so the last insert
    lands at the anchor position, which is the earliest queued.  Final
    ascending-index order matches queue order.  An anchor miss appends at
    the end, which also preserves queue order.
    """
    insertions: list[tuple[int, int, Message]] = []
    for queue_order, injection in enumerate(injections):
        match = injection.anchor.find_in(messages)
        insert_idx = match + 1 if match is not None else len(messages)
        inj_msg = Message("user", [injection.text])
        inj_msg.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
        if injection.consumption_id:
            # Per-consumption identity: replay_consumed_injections dedups on
            # this stamp, never on text alone.
            inj_msg.additional_properties[HistoryMarkerKind.INJECTION_ID_KEY] = injection.consumption_id
        _stamp_consumption(inj_msg, injection)
        insertions.append((insert_idx, queue_order, inj_msg))

    insertions.sort(key=lambda t: (t[0], t[1]), reverse=True)
    for idx, _order, msg in insertions:
        messages.insert(idx, msg)


def _is_answer_message(message: Message) -> bool:
    """An assistant message outside every tool exchange and marker: the answer text of a response."""
    return (
        message.role == "assistant"
        and HistoryMarkerKind.KEY not in message.additional_properties
        and not any(c.type in TOOL_CALL_CONTENT_TYPES or c.type in TOOL_RESULT_CONTENT_TYPES for c in message.contents)
    )


def _split_exchange_answer(message: Message) -> tuple[list[Content], list[Content]] | None:
    """Split an assistant message in a tool exchange into its exchange work and the answer after its last tool content.

    None unless that answer holds text: reasoning alone asks nothing again.
    """
    if message.role != "assistant" or HistoryMarkerKind.KEY in message.additional_properties:
        return None
    tool_types = TOOL_CALL_CONTENT_TYPES | TOOL_RESULT_CONTENT_TYPES
    last_tool = max((index for index, content in enumerate(message.contents) if content.type in tool_types), default=-1)
    if last_tool < 0:
        return None
    work, tail = list(message.contents[: last_tool + 1]), list(message.contents[last_tool + 1 :])
    return (work, tail) if any(content.type == "text" and (content.text or "").strip() for content in tail) else None
