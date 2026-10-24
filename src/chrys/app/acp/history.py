# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project persisted Chrys history into ACP replay updates."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeGuard

from acp import schema as acp_schema
from acp.helpers import (
    start_tool_call,
    tool_content,
    update_tool_call,
    update_user_message_text,
)
from acp.interfaces import Client

from chrys.foundation.hosted_tools import (
    HostedToolFamily,
    HostedToolPhase,
    HostedToolStatus,
    normalize_hosted_tool_status,
)
from chrys.foundation.models.history_markers import ANSWER_SEGMENT_KEY, HistoryMarkerKind
from chrys.foundation.models.turns import turn_slices
from chrys.foundation.tool_result_metadata import tool_result_metadata_failure_state
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.kernel import Content
from chrys.kernel.exchanges import (
    LEGACY_CALL_CONTENT_TYPE,
    TOOL_CALL_CONTENT_TYPES,
    TOOL_RESULT_CONTENT_TYPES,
    DictAccessor,
    EmptyIdPolicy,
    NoneIdPolicy,
    PairingPolicy,
    iter_exchanges,
    pair_results,
)
from chrys.service.agent_middleware.events.hosted_tools import (
    HostedToolView,
    adapt_hosted_tool,
    hosted_replay_status,
)
from chrys.service.llm.reasoning_display import readable_reasoning
from chrys.service.session.message_metadata import (
    TOOL_RESULT_METADATA_KEY,
    is_compaction_tool_summary,
    persisted_tool_call_kind,
)
from chrys.service.state.store import StateStore
from chrys.service.trajectory.state import TurnRecord

from .bridge import acp_tool_kind, with_chrys_metadata, with_hosted_metadata
from .tool_status import tool_result_failed

# Replay streams every function call as a tool card, informational ones
# included. Id-less occurrences pair positionally within their exchange —
# the None-id and empty-id streams stay separate, and a non-string id joins
# the None stream. Presentation ids keep their own globally coerced
# missing-call-id minting space below, independent of pairing.
_REPLAY_PAIRING_POLICY = PairingPolicy(
    call_types=TOOL_CALL_CONTENT_TYPES | {LEGACY_CALL_CONTENT_TYPE},
    include_informational_calls=True,
    result_types=TOOL_RESULT_CONTENT_TYPES,
    none_id=NoneIdPolicy.POSITIONAL,
    empty_id=EmptyIdPolicy.POSITIONAL,
    malformed_id="treat_as_none",
)

_TEXT_FALLBACK_LIMIT = 4_000


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    """Narrow persisted JSON objects, whose keys are necessarily strings."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


@dataclass(frozen=True)
class _ReplayPairingIndex:
    """Exchange-scoped replay associations plus unpaired hosted outputs."""

    result_for_call: dict[tuple[int, int], tuple[int, int] | None]
    call_for_result: dict[tuple[int, int], tuple[int, int]]
    standalone_hosted_results: set[tuple[int, int]]


class _TurnMessageView:
    """A persisted message dict seen through :class:`SupportsTurnMessage`.

    ``turn_slices`` reads only these three fields, and the dict is the store's
    own object — nothing here copies or rewrites it.
    """

    __slots__ = ("_message",)

    def __init__(self, message: dict[str, object]) -> None:
        self._message = message

    @property
    def role(self) -> str:
        role = self._message.get("role", "")
        return role if isinstance(role, str) else ""

    @property
    def text(self) -> str | None:
        text = _message_text(self._message)
        return text or None

    @property
    def additional_properties(self) -> dict[str, object]:
        additional = self._message.get("additional_properties")
        return additional if isinstance(additional, dict) else {}


def index_turns(
    messages: list[dict[str, object]],
    records: list[TurnRecord],
) -> list[tuple[TurnRecord, int, int] | None]:
    """Align persisted messages with the turns that ran them.

    Returns one entry per :func:`turn_slices` slice — ``(record, start, end)``
    when the slice's opening message is the one the record registered as the
    turn's opener, and ``None`` when nothing claims it (a session saved before
    turn identity existed, or by a build that never registered it).

    The opening message is matched by ``_chrys_analytics_item_id``, the id
    that is minted once and copied verbatim by every later save, so an
    injected or retried copy of the text can never be mistaken for the
    opener. ``None`` entries stay ``None``: a plausible-looking pairing is
    worse than an admitted gap, and a client that gets no turn id simply
    shows the text without one.
    """
    by_opening_item = {record.opening_item_id: record for record in records if record.opening_item_id}
    slices = turn_slices([_TurnMessageView(message) for message in messages])
    aligned: list[tuple[TurnRecord, int, int] | None] = []
    for start, end in slices:
        additional = _TurnMessageView(messages[start]).additional_properties
        item_id = additional.get(ANALYTICS_ITEM_ID_KEY)
        record = by_opening_item.get(item_id) if isinstance(item_id, str) else None
        aligned.append((record, start, end) if record is not None else None)
    return aligned


def _paired_result_coordinates(
    messages: list[dict[str, object]],
) -> _ReplayPairingIndex:
    """Return paired coordinates and canonical hosted results without a call."""
    accessor = DictAccessor()
    result_for_call: dict[tuple[int, int], tuple[int, int] | None] = {}
    call_for_result: dict[tuple[int, int], tuple[int, int]] = {}
    standalone_hosted_results: set[tuple[int, int]] = set()
    for exchange in iter_exchanges(messages, accessor):
        pairing = pair_results(messages, exchange, accessor, _REPLAY_PAIRING_POLICY)
        for assignments in (*pairing.truthy_assignments.values(), *pairing.falsy_assignments.values()):
            for call, result in assignments:
                call_coordinate = (call.message_index, call.content_index)
                result_for_call[call_coordinate] = (
                    (result.message_index, result.content_index) if result is not None else None
                )
                if result is not None:
                    call_for_result[(result.message_index, result.content_index)] = (
                        call.message_index,
                        call.content_index,
                    )
        for call in pairing.unpairable_calls:
            result_for_call.setdefault((call.message_index, call.content_index), None)
        unconsumed_results = [
            result
            for results in (*pairing.unconsumed_results.values(), *pairing.falsy_unconsumed_results.values())
            for result in results
        ]
        for result in [*unconsumed_results, *pairing.unpairable_results]:
            coordinate = (result.message_index, result.content_index)
            candidate = _indexed_contents(messages[result.message_index])
            content = next((item for index, item in candidate if index == result.content_index), None)
            if content is not None and _is_hosted_result(content):
                standalone_hosted_results.add(coordinate)

    # Orphan output messages do not form exchanges by definition. The
    # exchange walker above remains the sole pairing authority; this pass
    # merely inventories hosted result occurrences that it did not assign.
    for message_index, message in enumerate(messages):
        additional = message.get("additional_properties", {})
        if isinstance(additional, dict) and HistoryMarkerKind.KEY in additional:
            continue
        if message.get("role", "") not in {"assistant", "tool"}:
            continue
        for content_index, content in _indexed_contents(message):
            coordinate = (message_index, content_index)
            if coordinate not in call_for_result and _is_hosted_result(content):
                standalone_hosted_results.add(coordinate)
    return _ReplayPairingIndex(
        result_for_call=result_for_call,
        call_for_result=call_for_result,
        standalone_hosted_results=standalone_hosted_results,
    )


async def replay_session_history(
    client: Client,
    state_store: StateStore,
    session_id: str,
    *,
    prefer_recovery: bool = False,
    tool_kind_resolver: Callable[[str], str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Emit basic ACP replay updates for a restored session.

    Returns the ``messageId``s each persisted answer segment replayed as:
    what a bare retry that drops the segment from history withdraws from
    the client.
    """
    transcript = await state_store.load_session_transcript(session_id, prefer_recovery=prefer_recovery)
    if transcript is None:
        return {}
    raw_messages = transcript.messages
    if not raw_messages:
        return {}
    # Compaction tool summaries are the model's context, not the client's; as
    # in the TUI session transcript, neither a summary nor the messages it
    # replaced replay. Drop them before pairing so coordinates and replay ids
    # ignore them.
    raw_messages = [message for message in raw_messages if not is_compaction_tool_summary(message)]
    turn_for_message, opening_user_message = _turn_labels(raw_messages, transcript.turns)
    pairing_index = _paired_result_coordinates(raw_messages)
    replay_id_by_call: dict[tuple[int, int], str] = {}
    replay_id_by_standalone_result: dict[tuple[int, int], str] = {}
    pending_tool_names: dict[str, str] = {}
    pending_tool_kinds: dict[str, str] = {}
    pending_hosted: dict[str, tuple[Content, HostedToolView]] = {}
    standalone_hosted: dict[str, tuple[Content, HostedToolView]] = {}
    open_replay_ids: dict[str, None] = {}
    message_index_by_replay_id: dict[str, int] = {}
    call_start_updates: dict[tuple[int, int], Any] = {}
    started_calls: set[tuple[int, int]] = set()
    occurrence_index = 0
    answer_message_ids: dict[str, list[str]] = {}

    # Allocate every call before publishing anything. Results can then find
    # their exchange-scoped card without forcing the normal replay path to
    # emit all starts ahead of the message's text and results.
    for message_index, message in enumerate(raw_messages):
        additional = message.get("additional_properties", {})
        if isinstance(additional, dict) and HistoryMarkerKind.KEY in additional:
            continue
        role = str(message.get("role", ""))
        if role not in {"assistant", "tool"}:
            continue
        for content_index, content in _indexed_contents(message):
            coordinate = (message_index, content_index)
            is_call = role == "assistant" and content.get("type") in _REPLAY_PAIRING_POLICY.call_types
            is_standalone_hosted_result = coordinate in pairing_index.standalone_hosted_results
            if not is_call and not is_standalone_hosted_result:
                continue
            occurrence_index += 1
            replay_id = _replay_tool_call_id(message_index, content_index, occurrence_index)
            open_replay_ids[replay_id] = None
            message_index_by_replay_id[replay_id] = message_index
            if is_standalone_hosted_result:
                result = Content.from_dict(content)
                view = adapt_hosted_tool(None, result)
                replay_id_by_standalone_result[coordinate] = replay_id
                standalone_hosted[replay_id] = (result, view)
                call_start_updates[coordinate] = _hosted_tool_call_start(view, replay_id)
                continue
            replay_id_by_call[coordinate] = replay_id
            if _is_hosted_call(content):
                call = Content.from_dict(content)
                view = adapt_hosted_tool(call)
                pending_hosted[replay_id] = (call, view)
                pending_tool_names[replay_id] = view.tool_name
                pending_tool_kinds[replay_id] = _hosted_acp_kind(view.family)
                call_start_updates[coordinate] = _hosted_tool_call_start(view, replay_id)
            else:
                pending_tool_names[replay_id] = _tool_name(content)
                pending_tool_kinds[replay_id] = _tool_kind(content, resolver=tool_kind_resolver)
                call_start_updates[coordinate] = _tool_call_start(content, replay_id, pending_tool_kinds[replay_id])

    async def emit_call_start(coordinate: tuple[int, int]) -> None:
        if coordinate in started_calls:
            return
        update = call_start_updates.get(coordinate)
        if update is None:
            return
        meta = _turn_meta(turn_for_message[coordinate[0]])
        if meta:
            update.field_meta = with_chrys_metadata(update.field_meta, meta)
        await client.session_update(session_id=session_id, update=update)
        started_calls.add(coordinate)

    for message_index, message in enumerate(raw_messages):
        additional = message.get("additional_properties", {})
        if isinstance(additional, dict) and HistoryMarkerKind.KEY in additional:
            # Key presence, matching the grammar's boundary rule: a falsy
            # kind is still chrome, never conversation or tool activity.
            continue
        role = str(message.get("role", ""))
        text = _message_text(message)
        turn_meta = _turn_meta(turn_for_message[message_index])
        if role == "user":
            # Skip crash-leftover synthetic ``continue`` nudges: streaming
            # one as a user message misrepresents the conversation to the
            # client.  Injections keep streaming as plain user messages —
            # the update schema has no injection concept.
            if isinstance(additional, dict) and additional.get(HistoryMarkerKind.CONTINUATION_KEY):
                continue
            message_id = opening_user_message.get(message_index)
            if text:
                # Only the message that OPENED the turn carries the id the
                # client submitted; an injection or a retry note inside the
                # turn is a different message and has no client-side id.
                update = update_user_message_text(text)
                update.message_id = message_id
                if turn_meta:
                    update.field_meta = with_chrys_metadata(update.field_meta, turn_meta)
                await client.session_update(session_id=session_id, update=update)
            for update in _user_image_updates(message, message_id):
                if turn_meta:
                    update.field_meta = with_chrys_metadata(update.field_meta, turn_meta)
                await client.session_update(session_id=session_id, update=update)
            continue
        if role not in ("assistant", "tool"):
            continue
        indexed_contents = _indexed_contents(message)
        # The text of one persisted message is one ACP message per answer
        # segment: ACP groups chunks by messageId, and the transcript
        # separates them by paragraph, not by id. Readable reasoning replays
        # in its place as thoughts, which start the answer text after them as
        # a new message, as the live segments did; opaque reasoning replays
        # nothing.
        answer_message_id: str | None = None
        answer_segment: str | None = None
        emitted_text = False
        thought: tuple[str, str] | None = None
        for content_index, content in indexed_contents:
            content_type = content.get("type")
            if role == "assistant" and content_type == "text_reasoning":
                shown = readable_reasoning(Content.from_dict(content))
                if shown is None:
                    continue
                channel, thought_text = shown
                if thought is None or thought[0] != channel:
                    thought = (channel, str(uuid.uuid4()))
                update = acp_schema.AgentThoughtChunk(
                    sessionUpdate="agent_thought_chunk",
                    content=acp_schema.TextContentBlock(type="text", text=thought_text),
                    messageId=thought[1],
                )
                update.field_meta = with_chrys_metadata(update.field_meta, {"channel": channel, **turn_meta})
                await client.session_update(session_id=session_id, update=update)
                answer_message_id = None
                emitted_text = False
                continue
            thought = None
            if role == "assistant" and content_type == "text":
                content_text = content.get("text", "")
                if isinstance(content_text, str) and content_text:
                    segment = _answer_segment(content)
                    if answer_message_id is None or segment != answer_segment:
                        answer_message_id, answer_segment, emitted_text = str(uuid.uuid4()), segment, False
                        if segment is not None:
                            answer_message_ids.setdefault(segment, []).append(answer_message_id)
                    if emitted_text:
                        content_text = f"\n\n{content_text}"
                    update = acp_schema.AgentMessageChunk(
                        sessionUpdate="agent_message_chunk",
                        content=acp_schema.TextContentBlock(type="text", text=content_text),
                        messageId=answer_message_id,
                    )
                    if turn_meta:
                        update.field_meta = with_chrys_metadata(update.field_meta, turn_meta)
                    await client.session_update(session_id=session_id, update=update)
                    emitted_text = True
                continue
            coordinate = (message_index, content_index)
            if role == "assistant" and content_type in _REPLAY_PAIRING_POLICY.call_types:
                await emit_call_start(coordinate)
                replay_id = replay_id_by_call.get(coordinate)
                hosted = pending_hosted.get(replay_id) if replay_id is not None else None
                if (
                    replay_id is not None
                    and coordinate in pairing_index.result_for_call
                    and pairing_index.result_for_call.get(coordinate) is None
                    and hosted is not None
                    and _hosted_view_is_terminal(hosted[1])
                ):
                    view = hosted[1]
                    status = hosted_replay_status(view, has_result=False)
                    fallback_text = view.result_text
                    if status == "interrupted" and not fallback_text:
                        fallback_text = "Provider-hosted tool was interrupted before the session was persisted."
                    update = _hosted_terminal_update(
                        replay_id,
                        view,
                        status=status,
                        fallback_text=fallback_text,
                    )
                    if turn_meta:
                        update.field_meta = with_chrys_metadata(update.field_meta, turn_meta)
                    await client.session_update(session_id=session_id, update=update)
                    open_replay_ids.pop(replay_id, None)
                    pending_hosted.pop(replay_id, None)
                    pending_tool_names.pop(replay_id, None)
                    pending_tool_kinds.pop(replay_id, None)
                continue
            if content_type not in _REPLAY_PAIRING_POLICY.result_types:
                continue
            call_coordinate = pairing_index.call_for_result.get(coordinate)
            if call_coordinate is None:
                replay_id = replay_id_by_standalone_result.get(coordinate)
                hosted = standalone_hosted.pop(replay_id, None) if replay_id is not None else None
                if replay_id is None or hosted is None:
                    continue
                await emit_call_start(coordinate)
                open_replay_ids.pop(replay_id, None)
                view = hosted[1]
                status = hosted_replay_status(view, has_result=True)
                update = _hosted_terminal_update(replay_id, view, status=status)
                if turn_meta:
                    update.field_meta = with_chrys_metadata(update.field_meta, turn_meta)
                await client.session_update(session_id=session_id, update=update)
                continue
            replay_id = replay_id_by_call.get(call_coordinate)
            if replay_id is None:
                continue
            # Malformed legacy histories can embed a result before its call.
            # The card must exist before its terminal update even though the
            # normal path otherwise follows exact content order.
            await emit_call_start(call_coordinate)
            open_replay_ids.pop(replay_id, None)
            tool_name = pending_tool_names.pop(replay_id, "")
            tool_kind = pending_tool_kinds.pop(replay_id, "")
            hosted = pending_hosted.pop(replay_id, None)
            update = (
                _hosted_tool_call_result(hosted[0], Content.from_dict(content), replay_id)
                if hosted is not None
                else _tool_call_result(content, replay_id, tool_name, tool_kind)
            )
            if turn_meta:
                update.field_meta = with_chrys_metadata(update.field_meta, turn_meta)
            await client.session_update(
                session_id=session_id,
                update=update,
            )
    for replay_id in open_replay_ids:
        # These calls get a synthesized terminal update instead of a persisted
        # one, but they are as much a part of their turn as any other record,
        # so the id a client keys its cards by travels with them.
        meta = _turn_meta(
            turn_for_message[index] if (index := message_index_by_replay_id.get(replay_id)) is not None else None
        )
        hosted = pending_hosted.get(replay_id)
        if hosted is not None:
            view = hosted[1]
            status = hosted_replay_status(view, has_result=False)
            text = view.result_text
            if status == "interrupted" and not text:
                text = "Provider-hosted tool was interrupted before the session was persisted."
            update = _hosted_terminal_update(replay_id, view, status=status, fallback_text=text)
            if meta:
                update.field_meta = with_chrys_metadata(update.field_meta, meta)
            await client.session_update(session_id=session_id, update=update)
            continue
        update = update_tool_call(
            replay_id,
            status="failed",
            raw_output="No persisted tool result.",
            content=[tool_content(acp_schema.TextContentBlock(type="text", text="No persisted tool result."))],
        )
        if meta:
            update.field_meta = with_chrys_metadata(update.field_meta, meta)
        await client.session_update(session_id=session_id, update=update)
    return {segment: tuple(message_ids) for segment, message_ids in answer_message_ids.items()}


def _answer_segment(content: dict[str, Any]) -> str | None:
    """The live segment a persisted answer text was shown in, if history recorded it."""
    properties = content.get("additional_properties")
    segment = properties.get(ANSWER_SEGMENT_KEY) if isinstance(properties, dict) else None
    return segment if isinstance(segment, str) and segment else None


def _turn_labels(
    messages: list[dict[str, object]],
    records: list[TurnRecord],
) -> tuple[list[TurnRecord | None], dict[int, str]]:
    """Map each message to its turn, and opener indexes to their user message id.

    Two questions with different answers: which turn a message belongs to
    (every message inside a slice) and whether an opener has a recorded user
    message id (only the opener, and only when one was recorded).
    """
    return _labels_from_alignment(index_turns(messages, records), len(messages))


def _labels_from_alignment(
    aligned: list[tuple[TurnRecord, int, int] | None],
    message_count: int,
) -> tuple[list[TurnRecord | None], dict[int, str]]:
    """Derive per-message turn ownership from an :func:`index_turns` result."""
    turn_for_message: list[TurnRecord | None] = [None] * message_count
    opening_user_message: dict[int, str] = {}
    for entry in aligned:
        if entry is None:
            continue
        record, start, end = entry
        for index in range(start, min(end, message_count)):
            turn_for_message[index] = record
        if record.user_message_id:
            opening_user_message[start] = record.user_message_id
    return turn_for_message, opening_user_message


def session_history_payload(
    session_id: str,
    messages: list[dict[str, object]],
    records: list[TurnRecord],
) -> dict[str, Any]:
    """Project persisted messages and turn records into the history shape.

    The client gets the raw persisted messages unchanged — this is the
    debugging surface, not a replay — with the turn each one belongs to named
    alongside it, plus one entry per turn the session ran. Attribution comes
    from :func:`index_turns`, the same alignment replay uses, so the two
    surfaces can never disagree about which turn a message belongs to.
    """
    aligned = index_turns(messages, records)
    turn_for_message, _opening_user_message = _labels_from_alignment(aligned, len(messages))

    def _turn_entry(record: TurnRecord) -> dict[str, Any]:
        turn: dict[str, Any] = {
            "turnId": record.turn_id,
            "turnNumber": record.turn_number,
            "userMessageId": record.user_message_id,
        }
        if record.origin_session_id:
            turn["inheritedFrom"] = record.origin_session_id
        return turn

    # One entry per turn the session knows, in start order: a turn with no
    # message left in the transcript — inherited by a fork, or compacted away —
    # still ran, and the client needs its id to join live and replayed updates.
    turns_payload: list[dict[str, Any]] = [_turn_entry(record) for record in records]

    messages_payload: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        record = turn_for_message[message_index] if message_index < len(turn_for_message) else None
        if record is None:
            messages_payload.append({"turnId": None, "turnNumber": None, "message": message})
            continue
        messages_payload.append({"turnId": record.turn_id, "turnNumber": record.turn_number, "message": message})

    return {"sessionId": session_id, "turns": turns_payload, "messages": messages_payload}


def _turn_meta(record: TurnRecord | None) -> dict[str, Any]:
    if record is None:
        return {}
    return {"turnId": record.turn_id, "turnNumber": record.turn_number}


def _message_text(message: dict[str, object]) -> str:
    contents = _contents(message)
    parts: list[str] = []
    for content in contents:
        if content.get("type") == "text":
            text = content.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n\n".join(parts)


def _user_image_updates(message: dict[str, object], message_id: str | None) -> list[Any]:
    """Replay persisted user image contents as ACP image/resource blocks."""
    updates: list[Any] = []
    for content in _contents(message):
        if content.get("type") not in {"data", "uri", "image"}:
            continue
        media_type = content.get("media_type")
        uri = content.get("uri")
        if not isinstance(media_type, str) or not media_type.startswith("image/"):
            continue
        if not isinstance(uri, str) or not uri:
            continue
        if uri.startswith("data:") and ";base64," in uri:
            header, data = uri.split(",", 1)
            mime_type = header[5:].split(";")[0] or media_type
            block: Any = acp_schema.ImageContentBlock(type="image", data=data, mimeType=mime_type)
        else:
            block = acp_schema.ResourceContentBlock(
                type="resource_link",
                name=str(content.get("name") or content.get("file_id") or "User image"),
                uri=uri,
                mimeType=media_type,
            )
        updates.append(
            acp_schema.UserMessageChunk(
                sessionUpdate="user_message_chunk",
                content=block,
                messageId=message_id,
            )
        )
    return updates


def _contents(message: dict[str, object]) -> list[dict[str, object]]:
    return [content for _, content in _indexed_contents(message)]


def _indexed_contents(message: dict[str, object]) -> list[tuple[int, dict[str, object]]]:
    """Return JSON object contents without changing persisted coordinates."""
    contents = message.get("contents", [])
    if not isinstance(contents, list):
        return []
    return [
        (content_index, content) for content_index, content in enumerate(contents) if _is_string_object_dict(content)
    ]


def _replay_tool_call_id(message_index: int, content_index: int, occurrence_index: int) -> str:
    """Mint a stable presentation id from persisted transcript coordinates."""
    return f"history:{message_index}:{content_index}:{occurrence_index}"


def _tool_call_start(content: dict[str, object], replay_id: str, kind: str) -> acp_schema.ToolCallStart:
    name = _tool_name(content)
    return start_tool_call(
        replay_id,
        name.replace("_", " ").strip() or "Tool call",
        kind=acp_tool_kind(kind),
        status="in_progress",
        raw_input=_tool_args(content),
    )


def _is_hosted_call(content: dict[str, object]) -> bool:
    return bool(content.get("provider_hosted")) or content.get("type") in TOOL_CALL_CONTENT_TYPES - {"function_call"}


def _is_hosted_result(content: dict[str, object]) -> bool:
    content_type = content.get("type")
    return content_type in TOOL_RESULT_CONTENT_TYPES - {"function_result"} or (
        content_type == "function_result" and bool(content.get("provider_hosted"))
    )


def _hosted_acp_kind(family: str) -> acp_schema.ToolKind:
    if family in {HostedToolFamily.SEARCH, HostedToolFamily.FETCH}:
        return "search"
    if family in {HostedToolFamily.CODE, HostedToolFamily.SHELL}:
        return "execute"
    if family == HostedToolFamily.FILE_OPERATION:
        return "edit"
    return "other"


def _hosted_view_is_terminal(view: HostedToolView) -> bool:
    status = normalize_hosted_tool_status(view.status)
    return (
        status
        in {
            HostedToolStatus.COMPLETED,
            HostedToolStatus.FAILED,
            HostedToolStatus.INTERRUPTED,
        }
        or view.phase == HostedToolPhase.TERMINAL
    )


def _hosted_tool_call_start(view: HostedToolView, replay_id: str) -> acp_schema.ToolCallStart:
    return with_hosted_metadata(
        start_tool_call(
            replay_id,
            view.display_title or view.tool_name or "Hosted tool",
            kind=_hosted_acp_kind(view.family),
            status="in_progress",
            raw_input=view.arguments,
        ),
        provider_hosted=True,
        hosted_family=view.family,
        provider=view.provider,
        provider_item_type=view.provider_item_type,
        provider_call_id=view.provider_call_id,
        provider_status=view.provider_status,
    )


def _bounded_text(text: str) -> str:
    return text if len(text) <= _TEXT_FALLBACK_LIMIT else f"{text[: _TEXT_FALLBACK_LIMIT - 1]}…"


def _hosted_tool_contents(view: HostedToolView) -> list[Any]:
    payloads: list[Any] = []
    if view.result_text:
        payloads.append(tool_content(acp_schema.TextContentBlock(type="text", text=_bounded_text(view.result_text))))
    for image in view.image_contents:
        uri = image.uri or ""
        if uri.startswith("data:") and ";base64," in uri:
            prefix, data = uri.split(",", 1)
            mime = image.media_type or prefix.removeprefix("data:").removesuffix(";base64")
            payloads.append(
                tool_content(acp_schema.ImageContentBlock(type="image", data=data, mimeType=mime or "image/png"))
            )
        elif uri:
            payloads.append(
                tool_content(
                    acp_schema.ResourceContentBlock(
                        type="resource_link",
                        name=image.name or image.file_id or "Hosted image",
                        uri=uri,
                        mimeType=image.media_type,
                    )
                )
            )
    for artifact in view.artifacts:
        uri = artifact.uri or ""
        if not uri:
            label = artifact.name or artifact.file_id or artifact.vector_store_id or "Hosted artifact"
            detail = f"{label} ({artifact.media_type})" if artifact.media_type else label
            payloads.append(
                tool_content(acp_schema.TextContentBlock(type="text", text=_bounded_text(f"Hosted artifact: {detail}")))
            )
            continue
        size = artifact.additional_properties.get("size")
        payloads.append(
            tool_content(
                acp_schema.ResourceContentBlock(
                    type="resource_link",
                    name=artifact.name or artifact.file_id or "Hosted artifact",
                    uri=uri,
                    mimeType=artifact.media_type,
                    size=size if isinstance(size, int) and not isinstance(size, bool) else None,
                )
            )
        )
    return payloads


def _hosted_terminal_update(
    replay_id: str,
    view: HostedToolView,
    *,
    status: str,
    fallback_text: str = "",
) -> acp_schema.ToolCallProgress:
    content = _hosted_tool_contents(view)
    text = _bounded_text(view.result_text or fallback_text)
    if not content and text:
        content = [tool_content(acp_schema.TextContentBlock(type="text", text=text))]
    return with_hosted_metadata(
        update_tool_call(
            replay_id,
            status="failed" if status in {"failed", "interrupted"} else "completed",
            raw_output=view.result_text or fallback_text or None,
            content=content or None,
        ),
        provider_hosted=True,
        hosted_family=view.family,
        provider=view.provider,
        provider_item_type=view.provider_item_type,
        provider_call_id=view.provider_call_id,
        provider_status=view.provider_status,
    )


def _hosted_tool_call_result(call: Content, result: Content, replay_id: str) -> acp_schema.ToolCallProgress:
    view = adapt_hosted_tool(call, result)
    status = hosted_replay_status(view, has_result=True)
    return _hosted_terminal_update(replay_id, view, status=status)


def _tool_call_result(
    content: dict[str, object], replay_id: str, tool_name: str, tool_kind: str
) -> acp_schema.ToolCallProgress:
    result = content.get("result", "")
    result_text = result if isinstance(result, str) else str(result)
    return update_tool_call(
        replay_id,
        status="failed" if _function_result_failed(content, result_text, tool_name, tool_kind) else "completed",
        raw_output=result,
        content=[tool_content(acp_schema.TextContentBlock(type="text", text=result_text))] if result_text else None,
    )


def _function_result_failed(
    content: dict[str, object], result_text: str, tool_name: str = "", tool_kind: str = ""
) -> bool:
    metadata_state = _function_result_metadata_failure_state(content)
    if metadata_state is not None:
        return metadata_state
    exception = content.get("exception")
    has_exception = isinstance(exception, str) and bool(exception)
    return tool_result_failed(result_text, None, tool_kind=tool_kind, tool_name=tool_name, has_exception=has_exception)


def _function_result_metadata_failure_state(content: dict[str, object]) -> bool | None:
    raw_additional = content.get("additional_properties")
    if not _is_string_object_dict(raw_additional):
        return None
    additional = raw_additional
    metadata_state = tool_result_metadata_failure_state(additional)
    if metadata_state is not None:
        return metadata_state
    raw_replay_metadata = additional.get(TOOL_RESULT_METADATA_KEY)
    if _is_string_object_dict(raw_replay_metadata):
        replay_metadata = raw_replay_metadata
        return tool_result_metadata_failure_state(replay_metadata)
    return None


def _tool_name(content: dict[str, object]) -> str:
    name = content.get("name", "")
    return name if isinstance(name, str) else ""


def _tool_kind(content: dict[str, object], *, resolver: Callable[[str], str] | None = None) -> str:
    additional = content.get("additional_properties")
    persisted = persisted_tool_call_kind(additional)
    if persisted:
        return persisted
    if resolver is None:
        return ""
    return resolver(_tool_name(content))


def _tool_args(content: dict[str, object]) -> object:
    args = content.get("arguments")
    if isinstance(args, str):
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            return args
    return args
