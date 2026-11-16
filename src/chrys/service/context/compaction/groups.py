# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Message content signatures and compaction group walkers."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from typing import Any

from chrys.foundation.text.images import hashable_image_identity
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import (
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    Content,
    Message,
    is_image_content,
)
from chrys.kernel.compaction import _group_id, _group_kind
from chrys.kernel.identity import WeakIdentityRegistry
from chrys.service.context.tool_content import tool_call_name

_log = logging.getLogger(__name__)


def _tool_call_name(content: Content) -> str | None:
    """Compatibility facade for compaction and recall consumers."""
    return tool_call_name(content)


def _message_call_ids(msg: Message) -> tuple[str, ...]:
    """Return call ids carried by framework content items."""
    return tuple(c.call_id for c in msg.contents if c.call_id)


def _render_tool_result_value(value: Any) -> str:
    """Render one result payload without leaking Content object reprs."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return "\n".join(part for item in value if (part := _render_tool_result_value(item)))
    if isinstance(value, Content):
        if value.type == "text":
            return value.text or ""
        if value.type == "shell_command_output":
            parts = []
            if value.stdout:
                parts.append(value.stdout)
            if value.stderr:
                parts.append(f"stderr: {value.stderr}")
            if value.exit_code is not None:
                parts.append(f"exit code: {value.exit_code}")
            if value.timed_out:
                parts.append("timed out")
            return "\n".join(parts)
        if value.type == "hosted_file":
            return f"[file: {value.name or value.file_id or 'hosted file'}]"
        if value.type == "hosted_vector_store":
            return f"[vector store: {value.vector_store_id or 'hosted vector store'}]"
        if value.type in {"data", "uri"}:
            media_type = value.media_type or "binary"
            if media_type.startswith("image/") or (value.uri or "").startswith("data:image/"):
                return f"[{media_type} image]"
            return f"[{media_type} artifact]"
        return json.dumps(value.to_dict(exclude={"raw_representation"}), ensure_ascii=False, default=str)
    if isinstance(value, dict):
        return json.dumps(_without_inline_binary(value), ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _without_inline_binary(value: Any) -> Any:
    """Return *value* with inline base64 payloads replaced by a short placeholder.

    Provider payloads kept as plain data (an Anthropic ``{"type": "base64"}``
    source, such as a fetched PDF) would otherwise put their encoded bytes
    into summaries.
    """
    if isinstance(value, dict):
        if value.get("type") == "base64" and isinstance(value.get("data"), str):
            return _inline_binary_placeholder(value.get("media_type"))
        return {key: _without_inline_binary(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_without_inline_binary(item) for item in value]
    return value


def _inline_binary_placeholder(media_type: object) -> str:
    kind = media_type if isinstance(media_type, str) and media_type else "binary"
    return f"[{kind} image]" if kind.startswith("image/") else f"[{kind} artifact]"


def _tool_result_text(content: Content) -> str:
    """Render live or restored local/hosted result payloads for summaries.

    ``items`` is the canonical representation; ``result`` only mirrors the
    concatenated text items and is ``""`` when a result carries non-text
    payloads, so it must never be preferred over ``items``.
    """
    if content.type == "function_result":
        value = content.items or content.result
        return _render_tool_result_value(value)
    if content.type in {"hosted_tool_result", "search_tool_result"}:
        value = content.items or content.result
        return _render_tool_result_value(value)
    if content.type == "mcp_server_tool_result":
        return _render_tool_result_value(content.output)
    if content.type in {
        "code_interpreter_tool_result",
        "image_generation_tool_result",
        "shell_tool_result",
    }:
        return _render_tool_result_value(content.outputs)
    return ""


def _content_signature(msg: Message) -> tuple[tuple[str, str | None, str | None, str], ...]:
    """Return a stable, compact signature for message contents."""
    parts: list[tuple[str, str | None, str | None, str]] = []
    for content in msg.contents:
        content_type = content.type
        call_id = content.call_id or None
        name = content.name or None
        payload = _content_payload(content, content_type)
        parts.append((content_type, call_id, name, _payload_digest(payload)))
    return tuple(parts)


def _content_payload(content: Any, content_type: str) -> Any:
    if content_type == "function_call":
        return getattr(content, "arguments", None)
    if content_type == "function_result":
        if isinstance(content, Content) and content.items:
            image_payload = _function_result_image_payload(content)
            if image_payload:
                return {"result": content.result, "images": image_payload}
        return getattr(content, "result", None)
    if content_type == "text":
        return getattr(content, "text", None)
    if isinstance(content, str):
        return content
    to_dict = getattr(content, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return repr(content)


def _function_result_image_payload(content: Content) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    if not content.items:
        return images
    for item in content.items:
        if not is_image_content(item):
            continue
        byte_length, identity = hashable_image_identity(item.uri or "")
        images.append(
            {
                "media_type": item.media_type,
                "byte_length": byte_length,
                "hash": hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()[:16],
            }
        )
    return images


def _payload_digest(payload: Any) -> str:
    try:
        data = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    except TypeError:
        data = str(payload)
    return hashlib.sha256(data.encode("utf-8", errors="surrogatepass")).hexdigest()


# ---------------------------------------------------------------------------
# Group walkers over annotation metadata (chrys-owned data-structure tools;
# the per-message accessors ``_group_id``/``_group_kind`` come from
# ``chrys.kernel.compaction``)
# ---------------------------------------------------------------------------


def _ordered_group_ids(messages: list[Message]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for msg in messages:
        gid = _group_id(msg)
        if gid is not None and gid not in seen:
            seen.add(gid)
            ids.append(gid)
    return ids


def _group_messages_by_id(messages: list[Message]) -> dict[str, list[Message]]:
    grouped: dict[str, list[Message]] = {}
    for msg in messages:
        gid = _group_id(msg)
        if gid is not None:
            grouped.setdefault(gid, []).append(msg)
    return grouped


def _included_group_ids(messages: list[Message], ordered_ids: list[str]) -> list[str]:
    grouped = _group_messages_by_id(messages)
    return [
        gid
        for gid in ordered_ids
        if any(not m.additional_properties.get(EXCLUDED_KEY, False) for m in grouped.get(gid, []))
    ]


def _group_start_indices(messages: list[Message]) -> dict[str, int]:
    starts: dict[str, int] = {}
    for idx, msg in enumerate(messages):
        gid = _group_id(msg)
        if gid is not None and gid not in starts:
            starts[gid] = idx
    return starts


def _group_kind_map(messages: list[Message]) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for msg in messages:
        gid = _group_id(msg)
        kind = _group_kind(msg)
        if gid is not None and kind is not None and gid not in kinds:
            kinds[gid] = kind
    return kinds


def _fold_summary_ids(messages: list[Message]) -> set[str]:
    """Collect summary occurrences owned or referenced by a fold.

    Positional group IDs can be reused in a later wire view. Only the stable
    summary reference and analytics identity can associate its replacement
    across copies. Legacy summaries without that identity still match through
    the fold's shared content objects, never through their group labels.
    """
    summary_ids: set[str] = set()
    for message in messages:
        annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
        if not isinstance(annotation, dict):
            continue
        summary_id = annotation.get(SUMMARIZED_BY_SUMMARY_ID_KEY)
        if isinstance(summary_id, str):
            summary_ids.add(summary_id)
        if isinstance(annotation.get(SUMMARY_OF_GROUP_IDS_KEY), list):
            item_id = read_analytics_item_id(message.additional_properties)
            if item_id is not None:
                summary_ids.add(f"tool_summary_{item_id}")
    return summary_ids


def _matches_summary_id(message: Message, summary_ids: set[str] | frozenset[str]) -> bool:
    """Match a replacement by occurrence, independently of its wire labels."""
    item_id = read_analytics_item_id(message.additional_properties)
    return item_id is not None and f"tool_summary_{item_id}" in summary_ids


def _message_present(messages: list[Message], needle: Message) -> bool:
    """Return whether *needle* is already represented in *messages*."""
    if any(msg is needle for msg in messages):
        return True
    needle_block_id = needle.additional_properties.get("_block_id")
    if needle_block_id is None:
        return False
    return any(msg.additional_properties.get("_block_id") == needle_block_id for msg in messages)


def _content_object_ids(messages: Iterable[Message]) -> set[int]:
    """Identity set of every content OBJECT carried by *messages*.

    The stable cross-view currency: the kernel wire hands every layer
    per-call message VIEWS — fresh wrapper + fresh contents list — so
    wrapper identity and ``id(msg.contents)`` both break across the
    state↔wire boundary, while the content objects stay shared. Bare-id
    form for FRAME-LOCAL use only — a caller persisting identities across
    calls builds a ``WeakIdentityRegistry`` instead, whose entries die
    with their objects.
    """
    return {id(c) for m in messages for c in m.contents}


def _carries_only(msg: Message, members: WeakIdentityRegistry | set[int] | frozenset[int]) -> bool:
    """Whether *msg*'s contents are drawn entirely from *members*.

    *members* is either a frame-local bare-id set or a persisted weak
    registry (whose dead entries never match). Empty messages never match:
    an empty contents list carries no identity evidence, and ``all()``
    over it would vacuously claim membership.
    """
    if not msg.contents:
        return False
    if isinstance(members, WeakIdentityRegistry):
        return all(c in members for c in msg.contents)
    return all(id(c) in members for c in msg.contents)


def _tool_groups_in_range(
    start: int,
    end: int,
    ordered_ids: list[str],
    kinds: dict[str, str],
    starts: dict[str, int],
    grouped: dict[str, list[Message]],
) -> list[str]:
    """Return included tool-call group IDs whose first message is in [start, end)."""
    return [
        gid
        for gid in ordered_ids
        if kinds.get(gid) == "tool_call"
        and start <= starts.get(gid, -1) < end
        and any(not m.additional_properties.get(EXCLUDED_KEY, False) for m in grouped.get(gid, []))
    ]


def _any_tool_groups_in_range(
    start: int,
    end: int,
    ordered_ids: list[str],
    kinds: dict[str, str],
    starts: dict[str, int],
) -> list[str]:
    """Return ALL tool-call group IDs in [start, end), regardless of exclusion.

    Used by Phase 2/3 which need to find groups that were already summarised
    by Phase 1 (originals excluded, summary still visible).
    """
    return [gid for gid in ordered_ids if kinds.get(gid) == "tool_call" and start <= starts.get(gid, -1) < end]


# ---------------------------------------------------------------------------
# Message ID deduplication
# ---------------------------------------------------------------------------


def _dedup_message_ids(messages: list[Message]) -> None:
    """Assign unique ``message_id`` values to messages with duplicate IDs.

    Response aggregation can reuse sequential IDs (``msg_1``, ``msg_2``, …)
    across runs within the same session. ``annotate_message_groups`` uses
    ``message_id`` to form group IDs, so duplicates cause unrelated tool calls
    from different turns to merge into a single mega-group, breaking
    compaction completely.

    This function detects duplicates and reassigns them with a positional
    suffix (e.g. ``msg_2`` → ``msg_2_i42``) so each message gets its own
    unique identity.  Already-unique IDs and messages without IDs are left
    untouched.
    """
    seen: dict[str, int] = {}
    for idx, msg in enumerate(messages):
        mid = msg.message_id
        if not mid:
            continue
        if mid in seen:
            # Duplicate — reassign with positional suffix
            msg.message_id = f"{mid}_i{idx}"
        else:
            seen[mid] = idx
