# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Compaction summary rendering and summary annotations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id, read_analytics_item_id
from chrys.kernel import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    TOOL_CALL_CONTENT_TYPES,
    TOOL_RESULT_CONTENT_TYPES,
    Content,
    Message,
    annotate_message_groups,
    included_token_count,
    set_excluded,
)
from chrys.kernel.compaction import _has_reasoning
from chrys.kernel.exchanges import (
    EmptyIdPolicy,
    LiveAccessor,
    NoneIdPolicy,
    PairingPolicy,
    iter_exchanges,
    pair_results,
)
from chrys.service.tools.spill import split_spill_notice

from .events import _REASON_COMPACTION, _REASON_REMOVAL, CachedSummary
from .groups import _group_start_indices, _tool_call_name, _tool_result_text

if TYPE_CHECKING:
    from chrys.kernel import TokenizerProtocol

    from .exclusions import ExclusionLedger

_log = logging.getLogger(__name__)


def _make_summary_message(
    summary_text: str, summary_id: str, original_ids: list[str], group_id: str, *, item_id: str | None = None
) -> Message:
    """Create a compaction summary message."""
    message = Message(
        role="assistant",
        contents=[summary_text],
        message_id=summary_id,
        additional_properties={
            GROUP_ANNOTATION_KEY: {
                SUMMARY_OF_MESSAGE_IDS_KEY: original_ids,
                SUMMARY_OF_GROUP_IDS_KEY: [group_id],
            },
        },
    )
    # The summary replaces the group in every later request, so it needs its
    # analytics identity now — not at the save that ends the turn.
    ensure_analytics_item_id(message.additional_properties, item_id=item_id)
    return message


_RESULT_SUMMARY_MAX = 100
"""Max characters kept from each end of a tool result (and from the start of narration) in compaction summaries."""

_MIN_SUMMARY_SAVING_TOKENS = 200
"""A group whose summary saves fewer tokens than this stays as it is.

Every summarised group rewrites one more place in the prompt, so a small one
costs more in lost prompt cache than it frees; Phase 2 still removes it when
a pass escalates.
"""

_ARGS_SUMMARY_MAX = 200
"""Max characters for tool call arguments in compaction summaries."""

_SUMMARY_TOTAL_MAX = 4096
"""Max characters for the complete tool-call compaction summary."""

_SUMMARY_TRUNCATION_MARKER = "... [truncated]"


_PAIRING_CONTENT_TYPES = TOOL_CALL_CONTENT_TYPES | TOOL_RESULT_CONTENT_TYPES
_SUMMARY_REPRESENTABLE_CONTENT_TYPES = frozenset({"text", "text_reasoning", "usage"})
_SUMMARY_PAIRING_POLICY = PairingPolicy(
    call_types=TOOL_CALL_CONTENT_TYPES,
    include_informational_calls=True,
    result_types=TOOL_RESULT_CONTENT_TYPES,
    none_id=NoneIdPolicy.POSITIONAL,
    empty_id=EmptyIdPolicy.POSITIONAL,
    malformed_id="stringify",
)


def _is_fused_narration(message: Message) -> bool:
    """Whether the message is a narration member of a fused tool-call group.

    Exchange fusion absorbs non-call assistant siblings between a call and
    its results into the tool-call group; members carrying no call, result,
    or reasoning content are wire-legal on their own, outside the group's
    projection-atomicity set. Reasoning-bearing members are NOT narration:
    a reasoning item must never outlive its exchange."""
    return (
        message.role == "assistant"
        and not _has_reasoning(message)
        and not any(content.type in _PAIRING_CONTENT_TYPES for content in message.contents)
    )


def _narration_survives_summary(message: Message) -> bool:
    """Whether a narration member must stay on the wire when its group is
    summarized: the text summary cannot represent contents like images, so
    excluding such a member would silently drop them."""
    return _is_fused_narration(message) and any(
        content.type not in _SUMMARY_REPRESENTABLE_CONTENT_TYPES for content in message.contents
    )


def _build_summary(group_msgs: list[Message]) -> str | None:
    """Build a compact summary preserving call args but trimming results."""
    paired_calls: dict[tuple[int, int], Content] = {}
    accessor = LiveAccessor()
    for exchange in iter_exchanges(group_msgs, accessor):
        pairing = pair_results(group_msgs, exchange, accessor, _SUMMARY_PAIRING_POLICY)
        assignments = [assignment for values in pairing.truthy_assignments.values() for assignment in values] + [
            assignment for values in pairing.falsy_assignments.values() for assignment in values
        ]
        for call_occurrence, result_occurrence in assignments:
            if result_occurrence is None:
                continue
            paired_calls[(result_occurrence.message_index, result_occurrence.content_index)] = group_msgs[
                call_occurrence.message_index
            ].contents[call_occurrence.content_index]

    # Collect results and absorbed narration with function context, in
    # transcript order \u2014 a fused group may carry assistant text between a
    # call and its results, and excluding the group must not drop that text.
    tool_parts: list[str] = []
    for message_index, msg in enumerate(group_msgs):
        for content_index, content in enumerate(msg.contents):
            if content.type in TOOL_RESULT_CONTENT_TYPES:
                call = paired_calls.get((message_index, content_index))
                result_text = _result_excerpt(_tool_result_text(content) or "completed")
                name = _tool_call_name(call) if call is not None else None
                arguments = _summary_call_arguments(call) if call is not None else None
                args_str = _format_summary_arguments(arguments)
                call_str = f"{name}({args_str})" if args_str else name
                tool_parts.append(f"{call_str or 'tool'} \u2192 {result_text}")
            elif msg.role == "assistant" and content.type == "text" and content.text and content.text.strip():
                narration = content.text.strip()
                if len(narration) > _RESULT_SUMMARY_MAX:
                    narration = f"{narration[:_RESULT_SUMMARY_MAX]}... ({len(narration)} chars)"
                tool_parts.append(f'assistant: "{narration}"')

    if not tool_parts:
        return None
    summary_label = "; ".join(tool_parts)
    prefix = "[Tool call: "
    suffix = "]"
    summary_text = f"{prefix}{summary_label}{suffix}"
    if len(summary_text) <= _SUMMARY_TOTAL_MAX:
        return summary_text
    allowed_label_chars = _SUMMARY_TOTAL_MAX - len(prefix) - len(suffix) - len(_SUMMARY_TRUNCATION_MARKER)
    truncated_label = summary_label[:allowed_label_chars].rstrip()
    return f"{prefix}{truncated_label}{_SUMMARY_TRUNCATION_MARKER}{suffix}"


def _result_excerpt(result_text: str) -> str:
    """Both ends of a tool result, plus the path of its full output when it was spilled.

    The end of command output (exit code, error, test summary) is worth at
    least as much as its start; a spilled result keeps its pointer so the
    model can read the file instead of running the tool again.
    """
    body, spill_path = split_spill_notice(result_text)
    if len(body) > 2 * _RESULT_SUMMARY_MAX:
        body = f"{body[:_RESULT_SUMMARY_MAX]} ... {body[-_RESULT_SUMMARY_MAX:]} ({len(result_text)} chars)"
    return f"{body} [full output: {spill_path}]" if spill_path is not None else body


def _summary_call_arguments(content: Content) -> Any:
    """Return field-aware arguments for every canonical call variant."""
    if content.type == "code_interpreter_tool_call":
        code = "".join(item.text or "" for item in content.inputs or [] if item.type == "text")
        return {"code": code} if code else None
    if content.type == "image_generation_tool_call":
        return {"image_id": content.image_id} if content.image_id else None
    if content.type == "shell_tool_call":
        arguments: dict[str, Any] = {}
        if content.commands is not None:
            arguments["commands"] = content.commands
        if content.timeout_ms is not None:
            arguments["timeout_ms"] = content.timeout_ms
        if content.max_output_length is not None:
            arguments["max_output_length"] = content.max_output_length
        return arguments or None
    return content.arguments


def _format_summary_arguments(arguments: Any) -> str:
    if not arguments:
        return ""
    if isinstance(arguments, dict):
        parts = []
        for key, value in arguments.items():
            value_str = f'"{value}"' if isinstance(value, str) else str(value)
            parts.append(f"{key}={value_str}")
        args_str = ", ".join(parts)
    else:
        args_str = str(arguments)
    if len(args_str) > _ARGS_SUMMARY_MAX:
        return f"{args_str[:_ARGS_SUMMARY_MAX]}..."
    return args_str


def _set_summarized(message: Message, summary_id: str) -> None:
    """Mark a message as summarized by a given summary ID.

    Creates a **new** dict for the ``_group`` annotation to avoid mutating
    a dict that may be shared with another Message: shallow-copying
    ``additional_properties`` via ``dict()`` retains nested annotation aliases.
    """
    existing: dict[str, Any] = message.additional_properties.get(GROUP_ANNOTATION_KEY, {})
    annotation = dict(existing) if isinstance(existing, dict) else {}
    annotation[SUMMARIZED_BY_SUMMARY_ID_KEY] = summary_id
    message.additional_properties[GROUP_ANNOTATION_KEY] = annotation


def _compact_group(
    messages: list[Message],
    group_id: str,
    group_msgs: list[Message],
    *,
    ledger: ExclusionLedger,
    tokenizer: TokenizerProtocol,
) -> list[str] | None:
    """Compact one tool-call group and update its ledger cache."""
    if (
        not group_msgs
        or group_id in ledger._removed_group_ids
        or any(msg.additional_properties.get(EXCLUDED_KEY, False) for msg in group_msgs)
    ):
        return None

    kept_ids = {id(msg) for msg in group_msgs if _narration_survives_summary(msg)}
    replaced_msgs = [msg for msg in group_msgs if id(msg) not in kept_ids]

    summary_text = _build_summary(replaced_msgs)
    if summary_text is None:
        return None
    # Untokenized messages count as zero, which says nothing about the saving.
    original_tokens = included_token_count(replaced_msgs)
    if 0 < original_tokens < tokenizer.count_tokens(summary_text) + _MIN_SUMMARY_SAVING_TOKENS:
        return None
    # Group IDs are positional wire-view labels and may be reused next turn.
    # The original-to-summary relationship needs its own occurrence identity.
    item_id = new_analytics_id()
    summary_id = f"tool_summary_{item_id}"
    original_ids = [m.message_id for m in replaced_msgs if m.message_id]

    tool_names = [
        tool_name for msg in replaced_msgs for content in msg.contents if (tool_name := _tool_call_name(content))
    ]

    # Mark the replaced originals as excluded.  Narration members the text
    # summary cannot represent (images and other non-text content) stay on
    # the wire: they are not pairing carriers, so the group still projects
    # atomically without them.
    any_changed = False
    for msg in replaced_msgs:
        _set_summarized(msg, summary_id)
        any_changed = set_excluded(msg, excluded=True, reason=_REASON_COMPACTION) or any_changed

    if not any_changed:
        return None

    summary_message = _make_summary_message(summary_text, summary_id, original_ids, group_id, item_id=item_id)
    # Cache the occurrence identity too: message_id may be rewritten each pass.
    ledger._summary_cache[summary_id] = CachedSummary(
        summary_text=summary_text,
        original_ids=original_ids,
        group_id=group_id,
        item_id=item_id,
        message=summary_message,
    )

    starts = _group_start_indices(messages)
    insertion_index = starts.get(group_id, 0)
    messages.insert(insertion_index, summary_message)

    # Only the summary needs counting: the messages after it keep their ids and
    # contents, and with them their counts. Counting every later message again
    # for each compacted group made a pass quadratic in the conversation.
    annotate_message_groups(messages, from_index=insertion_index, force_reannotate=False, tokenizer=tokenizer)

    return tool_names


def _remove_group(
    messages: list[Message],
    group_id: str,
    group_msgs: list[Message],
    *,
    ledger: ExclusionLedger,
) -> list[str] | None:
    """Remove one tool-call group and update its ledger state."""
    if not group_msgs:
        return None

    tool_names = [
        tool_name for msg in group_msgs for content in msg.contents if (tool_name := _tool_call_name(content))
    ]

    any_changed = False
    for msg in group_msgs:
        if _is_fused_narration(msg):
            continue
        any_changed = set_excluded(msg, excluded=True, reason=_REASON_REMOVAL) or any_changed

    summary_ids: set[str] = set()
    for msg in group_msgs:
        annotation = msg.additional_properties.get(GROUP_ANNOTATION_KEY)
        if isinstance(annotation, dict) and isinstance(sid := annotation.get(SUMMARIZED_BY_SUMMARY_ID_KEY), str):
            summary_ids.add(sid)
    for msg in messages:
        item_id = read_analytics_item_id(msg.additional_properties)
        if item_id is not None and f"tool_summary_{item_id}" in summary_ids:
            any_changed = set_excluded(msg, excluded=True, reason=_REASON_REMOVAL) or any_changed

    if not any_changed:
        return None

    for summary_id in summary_ids:
        ledger._summary_cache.pop(summary_id, None)
    ledger._removed_group_ids.add(group_id)

    return tool_names
