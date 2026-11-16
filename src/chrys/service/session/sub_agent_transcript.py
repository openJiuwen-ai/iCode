# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safe, runner-neutral replay input for persisted sub-agent transcripts.

Kernel replay contract
----------------------
Identified audits explicitly declare ``analytics_item_id_v1`` and use the
existing message-level ``_chrys_analytics_item_id`` as occurrence identity.
Compression must stamp that ID before copying a fold range. Replay emits
``compressed_msgs`` in persisted order, removes their summary placeholders,
then emits only unseen live occurrences. Equal content with different IDs is
distinct; ``message_id``, ``call_id``, and content equality are not identity.

Never infer the capability from IDs that merely happen to be present. A false
or incomplete declaration fails closed to the isolated legacy projector. Old
audits remain best-effort because archived/live twins without identity or a
summary boundary are fundamentally ambiguous. Any new removal rule needs a
paired test proving the nearest equal-content occurrence that must survive.

ACP replay contract
-------------------
New translated updates carry their monotonic ``attempt`` and a successful
terminal audit names ``successful_attempt``. The detail view may fingerprint
parent-result shapes only from that attempt; flattened legacy trails fail
closed to the parent fallback. All attempts remain available for audit replay,
but for the answer a retry withdrew: a local ``answer_withdrawn`` entry drops
the text its attempt showed after its last tool card, as the live views did.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.platform.files import read_owner_verified_bounded
from chrys.foundation.tool_kinds import TOOL_CALL_KIND_METADATA_KEY, TOOL_KINDS
from chrys.foundation.tool_result_metadata import (
    TOOL_FAILED_METADATA_KEY,
    TOOL_INTERRUPTED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import Message
from chrys.kernel.exchanges import TOOL_CALL_CONTENT_TYPES
from chrys.service.agent_middleware.events.hosted_tools import (
    ResponsePresentationPlan,
    normalize_hosted_replay_content,
)
from chrys.service.session.sub_agent_logs import (
    ACP_ANSWER_WITHDRAWN_UPDATE,
    MAX_SUB_AGENT_AUDIT_BYTES,
    SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY,
    is_safe_sub_agent_log_basename,
    sessions_dir,
)

logger = logging.getLogger(__name__)

_TERMINAL_AUDIT_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "aborted", "cascade_aborted", "setup_failed", "orphaned"}
)


@dataclass(frozen=True, slots=True)
class PersistedSubAgentTranscript:
    """Canonical messages loaded from one sub-agent audit envelope."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    profile_name: str = ""
    terminal_audit: bool = True
    includes_final_message: bool = False
    represented_result_fingerprints: frozenset[bytes] = field(default_factory=frozenset, repr=False)
    requires_result_fingerprint: bool = False

    def represents_result_text(self, text: str) -> bool:
        """Whether a successful replay already contains this exact ACP parent result."""
        return bool(text) and _result_text_fingerprint(text) in self.represented_result_fingerprints

    def covers_result_text(self, text: str) -> bool:
        """Whether replay is authoritative enough to replace the parent result.

        Kernel audits retain their complete message history, so a successful
        final assistant message is sufficient. ACP audits retain a bounded
        update tail; those replays must prove an exact result shape instead of
        letting a truncated final chunk suppress the authoritative parent
        result.
        """
        if self.requires_result_fingerprint:
            return self.represents_result_text(text)
        return self.includes_final_message


@dataclass(slots=True)
class _AcpToolRecord:
    """Latest replay-relevant state for one ACP tool occurrence."""

    call_id: str
    tool_name: str = "tool"
    tool_kind: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    status: str = "running"


async def load_persisted_sub_agent_transcript(
    parent_session_dir: Path,
    log_file: str,
) -> PersistedSubAgentTranscript | None:
    """Read one safe audit basename and normalize it for transcript replay.

    The owner-verifying, size-capped read is deliberately kept in the service
    layer. TUI callers receive canonical messages and never join or parse a raw
    runner-specific audit path themselves.
    """
    if not is_safe_sub_agent_log_basename(log_file):
        return None
    return await asyncio.to_thread(_load_persisted_sub_agent_transcript, parent_session_dir, log_file)


def _load_persisted_sub_agent_transcript(
    parent_session_dir: Path,
    log_file: str,
) -> PersistedSubAgentTranscript | None:
    path = sessions_dir(parent_session_dir) / log_file
    try:
        raw = read_owner_verified_bounded(path, max_bytes=MAX_SUB_AGENT_AUDIT_BYTES)
        envelope = json.loads(raw)
    except OSError, UnicodeDecodeError, ValueError:
        logger.debug("Unable to load persisted sub-agent transcript %s", log_file, exc_info=True)
        return None
    if not isinstance(envelope, dict):
        return None
    meta = envelope.get("meta")
    if not isinstance(meta, dict) or meta.get("record_type") != "sub_agent_session":
        return None
    return project_agent_transcript(envelope)


def project_agent_transcript(envelope: dict[str, Any]) -> PersistedSubAgentTranscript | None:
    """Project a validated agent archive, independently of its caller and storage layout.

    Sub-agent tools and workflow nodes share message occurrence identity,
    compressed-history expansion, ACP translation and replay metadata.
    """
    meta = envelope.get("meta")
    if not isinstance(meta, dict):
        return None
    profile_name = _profile_name(meta)
    status = meta.get("status")
    terminal_audit = status in _TERMINAL_AUDIT_STATUSES
    completed_successfully = status == "completed"
    runner = meta.get("runner")
    if runner == "kernel":
        state = envelope.get("state")
        if not isinstance(state, dict):
            return None
        if not isinstance(state.get("messages"), list):
            return None
        canonical = _complete_kernel_messages(
            state,
            occurrence_identity=meta.get("transcript_occurrence_identity"),
        )
        return PersistedSubAgentTranscript(
            messages=canonical,
            profile_name=profile_name,
            terminal_audit=terminal_audit,
            includes_final_message=completed_successfully and _includes_final_assistant_message(canonical),
        )
    if runner == "acp":
        acp_state = envelope.get("acp_state")
        if not isinstance(acp_state, dict):
            return None
        raw_updates = acp_state.get("translated_updates")
        messages = _acp_messages(raw_updates, terminal_audit=terminal_audit)
        successful_updates = _acp_successful_attempt_updates(
            raw_updates,
            successful_attempt=acp_state.get("successful_attempt"),
        )
        return PersistedSubAgentTranscript(
            messages=messages,
            profile_name=profile_name,
            terminal_audit=terminal_audit,
            includes_final_message=completed_successfully and _includes_final_assistant_message(messages),
            requires_result_fingerprint=True,
            represented_result_fingerprints=(
                _acp_result_text_fingerprints(_acp_messages(successful_updates, terminal_audit=True))
                if completed_successfully and successful_updates
                else frozenset()
            ),
        )
    return None


def _profile_name(meta: dict[str, Any]) -> str:
    for key in ("agent_display_name", "agent_profile", "tool_name"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _result_text_fingerprint(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).digest()


def _acp_result_text_fingerprints(messages: list[dict[str, Any]]) -> frozenset[bytes]:
    """Fingerprint the two parent-result shapes supported by ACP profiles."""
    segments: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        contents = message.get("contents")
        if not isinstance(contents, list):
            continue
        for content in contents:
            if not isinstance(content, dict) or content.get("type") != "text":
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                segments.append(text)
    if not segments:
        return frozenset()
    return frozenset(
        {
            _result_text_fingerprint(segments[-1]),
            _result_text_fingerprint("\n\n".join(segments)),
        }
    )


def _acp_successful_attempt_updates(raw_updates: Any, *, successful_attempt: Any) -> list[dict[str, Any]]:
    """Select only explicitly identified updates from the successful attempt.

    Legacy audits have no attempt boundary. They fail closed to the parent
    fallback instead of guessing across a flattened multi-attempt trail.
    """
    if type(successful_attempt) is not int or successful_attempt <= 0 or not isinstance(raw_updates, list):
        return []
    return [item for item in raw_updates if isinstance(item, dict) and item.get("attempt") == successful_attempt]


def _complete_kernel_messages(
    state: dict[str, Any],
    *,
    occurrence_identity: Any = None,
) -> list[dict[str, Any]]:
    """Restore the full chronological transcript from compacted kernel state.

    Compaction moves original messages into append-ordered blocks and leaves a
    summary placeholder in the live history. A sub-agent details modal is an
    audit view, so it needs the originals (including local tool calls), not the
    compacted prompt view used for future model requests.
    """
    archived_blocks = _kernel_archived_blocks(state)
    if occurrence_identity == SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY:
        identified = _complete_identified_kernel_messages(state, archived_blocks)
        if identified is not None:
            return identified
    return _complete_legacy_kernel_messages(state, archived_blocks)


def _kernel_archived_blocks(state: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Return serialized compressed blocks in their persisted chronological order."""
    archived_blocks: list[tuple[str, list[dict[str, Any]]]] = []
    compressed = state.get("compressed_msgs")
    if isinstance(compressed, list):
        for block in compressed:
            if not isinstance(block, dict):
                continue
            block_messages = block.get("messages")
            if isinstance(block_messages, list):
                block_id = block.get("compressed_context_id")
                archived_blocks.append(
                    (
                        block_id if isinstance(block_id, str) else "",
                        [message for message in block_messages if isinstance(message, dict)],
                    )
                )
    return archived_blocks


def _complete_identified_kernel_messages(
    state: dict[str, Any],
    archived_blocks: list[tuple[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]] | None:
    """Merge history deterministically by Chrys-owned message occurrence ID."""
    complete: list[dict[str, Any]] = []
    seen_occurrences: set[str] = set()
    archived_block_ids = {block_id for block_id, _messages in archived_blocks if block_id}

    def append_once(message: dict[str, Any]) -> bool:
        occurrence_id = _message_occurrence_id(message)
        if not occurrence_id:
            return False
        if occurrence_id not in seen_occurrences:
            complete.append(message)
            seen_occurrences.add(occurrence_id)
        return True

    for _block_id, block_messages in archived_blocks:
        for message in block_messages:
            if not append_once(message):
                return None

    live_messages = state.get("messages")
    if not isinstance(live_messages, list):
        return None
    for message in live_messages:
        if not isinstance(message, dict):
            return None
        summary_block_id = _compressed_summary_block_id(message)
        if summary_block_id and summary_block_id in archived_block_ids:
            continue
        if _is_archive_index(message):
            continue
        if not append_once(message):
            return None
    return complete


def _message_occurrence_id(message: dict[str, Any]) -> str:
    occurrence_id = read_analytics_item_id(message.get("additional_properties"))
    return occurrence_id or ""


def _complete_legacy_kernel_messages(
    state: dict[str, Any],
    archived_blocks: list[tuple[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Best-effort reconstruction for audits predating stable occurrence identity."""

    blocks_by_id = {block_id: messages for block_id, messages in archived_blocks if block_id}
    complete: list[dict[str, Any]] = []
    pending_live: list[dict[str, Any]] = []
    replayed_block_ids: set[str] = set()

    live_messages = state.get("messages")
    if isinstance(live_messages, list):
        for message in live_messages:
            if not isinstance(message, dict):
                continue
            if _is_archive_index(message):
                continue
            block_id = _compressed_summary_block_id(message)
            block_messages = blocks_by_id.get(block_id) if block_id else None
            if block_messages is None:
                pending_live.append(message)
                continue
            complete.extend(_without_archived_twins(pending_live, block_messages))
            complete.extend(block_messages)
            pending_live.clear()
            replayed_block_ids.add(block_id)

    orphaned_archives = [
        message
        for block_id, block_messages in archived_blocks
        if not block_id or block_id not in replayed_block_ids
        for message in block_messages
    ]
    reconstructed_live = [*complete, *pending_live]
    overlap = _orphan_live_overlap(orphaned_archives, reconstructed_live)
    return [*orphaned_archives, *reconstructed_live[overlap:]]


def _orphan_live_overlap(
    archived_messages: list[dict[str, Any]],
    live_messages: list[dict[str, Any]],
) -> int:
    """Return the longest archived-suffix/live-prefix twin sequence."""
    prefix_length = min(len(archived_messages), len(live_messages))
    if prefix_length == 0:
        return 0
    live_prefix = [_message_twin_key(message) for message in live_messages[:prefix_length]]
    archived = [_message_twin_key(message) for message in archived_messages]
    tokens: list[bytes | None] = [*live_prefix, None, *archived]
    failure = [0] * len(tokens)
    for index in range(1, len(tokens)):
        candidate = failure[index - 1]
        while candidate > 0 and tokens[index] != tokens[candidate]:
            candidate = failure[candidate - 1]
        if tokens[index] == tokens[candidate]:
            candidate += 1
        failure[index] = candidate
    return failure[-1]


def _without_archived_twins(
    live_segment: list[dict[str, Any]],
    archived_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove only cross-source twins immediately replaced by one summary."""
    archived_counts = Counter(_message_twin_key(message) for message in archived_messages)
    retained: list[dict[str, Any]] = []
    for message in live_segment:
        twin_key = _message_twin_key(message)
        archived_matches = archived_counts.get(twin_key, 0)
        if archived_matches == 0:
            retained.append(message)
        elif archived_matches == 1:
            del archived_counts[twin_key]
        else:
            archived_counts[twin_key] = archived_matches - 1
    return retained


def _message_twin_key(message: dict[str, Any]) -> bytes:
    """Hash persisted message content while ignoring its non-unique wrapper ID."""
    canonical = {key: value for key, value in message.items() if key != "message_id"}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).digest()


def _compressed_summary_block_id(message: dict[str, Any]) -> str:
    properties = message.get("additional_properties")
    if not isinstance(properties, dict) or properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.SUMMARY:
        return ""
    block_id = properties.get("_block_id")
    return block_id if isinstance(block_id, str) else ""


def _is_archive_index(message: dict[str, Any]) -> bool:
    """Whether *message* only lists archived blocks, whose messages the transcript already holds."""
    properties = message.get("additional_properties")
    return isinstance(properties, dict) and HistoryMarkerKind.ARCHIVE_INDEX_KEY in properties


def _includes_final_assistant_message(messages: list[dict[str, Any]]) -> bool:
    """Whether the last visible exchange already contains the final answer."""
    for message in reversed(messages):
        contents = message.get("contents")
        if not isinstance(contents, list) or not contents:
            continue
        if message.get("role") != "assistant":
            return False
        try:
            restored = Message("assistant", contents)
        except TypeError, ValueError:
            return False
        typed_message = Message(
            "assistant",
            [normalize_hosted_replay_content(content) for content in restored.contents],
        )
        has_text = any(content.type == "text" and bool(content.text) for content in typed_message.contents)
        has_call = any(content.type in TOOL_CALL_CONTENT_TYPES for content in typed_message.contents)
        if not any(content.provider_hosted for content in typed_message.contents):
            return has_text and not has_call
        return bool(ResponsePresentationPlan.from_messages([typed_message]).final_text)
    return False


def _acp_messages(raw_updates: Any, *, terminal_audit: bool = False) -> list[dict[str, Any]]:
    """Convert the bounded ACP audit ring into canonical replay messages."""
    if not isinstance(raw_updates, list):
        return []

    tokens: list[tuple[str, str]] = []
    text_segments: dict[str, str] = {}
    tools: dict[str, _AcpToolRecord] = {}
    current_tool_keys: dict[str, str] = {}
    occurrence_counts: dict[str, int] = {}
    open_text = ""
    open_message_id = ""
    text_sequence = 0
    current_attempt: int | None = None
    attempt_start = 0

    def flush_text() -> None:
        nonlocal open_text, text_sequence
        if not open_text.strip():
            open_text = ""
            return
        key = f"text:{text_sequence}"
        text_sequence += 1
        text_segments[key] = open_text
        tokens.append(("text", key))
        open_text = ""

    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        item_attempt = item.get("attempt")
        if type(item_attempt) is int and item_attempt > 0:
            if current_attempt is not None and item_attempt != current_attempt:
                flush_text()
                attempt_start = len(tokens)
                current_tool_keys.clear()
                open_message_id = ""
            current_attempt = item_attempt
        update = item.get("update")
        if not isinstance(update, dict):
            continue
        kind = update.get("sessionUpdate")
        if kind == ACP_ANSWER_WITHDRAWN_UPDATE:
            # A retry withdrew the answer its attempt showed after its last card, to ask for it again.
            flush_text()
            while len(tokens) > attempt_start and tokens[-1][0] == "text":
                tokens.pop()
            continue
        if kind == "agent_message_chunk":
            message_id = update.get("messageId")
            normalized_message_id = message_id if isinstance(message_id, str) else ""
            if normalized_message_id and open_message_id and normalized_message_id != open_message_id:
                flush_text()
            if normalized_message_id:
                open_message_id = normalized_message_id
            open_text += _acp_content_text(update.get("content"))
            continue
        if kind not in {"tool_call", "tool_call_update"}:
            continue
        raw_call_id = update.get("toolCallId")
        if not isinstance(raw_call_id, str) or not raw_call_id:
            continue
        record_key = current_tool_keys.get(raw_call_id, "")
        record = tools.get(record_key)
        status = update.get("status")
        terminal = record is not None and record.status in {"completed", "failed"}
        starts_new_occurrence = record is None or (
            terminal and (kind == "tool_call" or (isinstance(status, str) and status not in {"completed", "failed"}))
        )
        if starts_new_occurrence:
            flush_text()
            occurrence = occurrence_counts.get(raw_call_id, 0) + 1
            occurrence_counts[raw_call_id] = occurrence
            record_key = f"tool:{len(tools)}"
            record = _AcpToolRecord(call_id=f"acp:{raw_call_id}:{occurrence}")
            tools[record_key] = record
            current_tool_keys[raw_call_id] = record_key
            tokens.append(("tool", record_key))
        title = update.get("title")
        if isinstance(title, str) and title:
            record.tool_name = title
        tool_kind = update.get("kind")
        if isinstance(tool_kind, str):
            record.tool_kind = _acp_tool_kind(tool_kind)
        if "rawInput" in update:
            record.args = _acp_args(update.get("rawInput"))
        if "rawOutput" in update:
            record.result = _acp_value_text(update.get("rawOutput"))
        content_result = _acp_tool_content_text(update.get("content"))
        if content_result:
            record.result = content_result
        if isinstance(status, str):
            record.status = status

    flush_text()
    if terminal_audit:
        for record in tools.values():
            if record.status not in {"completed", "failed"}:
                record.status = "interrupted"
    return _messages_from_acp_tokens(tokens, text_segments, tools)


def _messages_from_acp_tokens(
    tokens: list[tuple[str, str]],
    text_segments: dict[str, str],
    tools: dict[str, _AcpToolRecord],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens):
        token_kind, key = tokens[index]
        if token_kind == "text":
            text_content = {"type": "text", "text": text_segments[key]}
            if index + 1 < len(tokens) and tokens[index + 1][0] == "tool":
                tool_key = tokens[index + 1][1]
                record = tools[tool_key]
                messages.append({"role": "assistant", "contents": [text_content, _acp_call_content(record)]})
                _append_acp_result(messages, record)
                index += 2
                continue
            messages.append({"role": "assistant", "contents": [text_content]})
            index += 1
            continue
        record = tools[key]
        messages.append({"role": "assistant", "contents": [_acp_call_content(record)]})
        _append_acp_result(messages, record)
        index += 1
    return messages


def _acp_call_content(record: _AcpToolRecord) -> dict[str, Any]:
    extra = {TOOL_CALL_KIND_METADATA_KEY: record.tool_kind} if record.tool_kind else {}
    content: dict[str, Any] = {
        "type": "function_call",
        "name": record.tool_name,
        "call_id": record.call_id,
        "arguments": dict(record.args),
    }
    if extra:
        content["additional_properties"] = extra
    return content


def _append_acp_result(messages: list[dict[str, Any]], record: _AcpToolRecord) -> None:
    if record.status not in {"completed", "failed", "interrupted"}:
        return
    result: dict[str, Any] = {
        "type": "function_result",
        "call_id": record.call_id,
        "result": "(interrupted)" if record.status == "interrupted" else record.result,
    }
    if record.status == "interrupted":
        result["additional_properties"] = {
            TOOL_RESULT_METADATA_KEY: {TOOL_INTERRUPTED_METADATA_KEY: True},
        }
    elif record.status == "failed":
        result["additional_properties"] = {
            TOOL_RESULT_METADATA_KEY: {TOOL_FAILED_METADATA_KEY: True},
        }
    messages.append({"role": "tool", "contents": [result]})


def _acp_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    return {"input": value}


def _acp_tool_kind(value: str) -> str:
    if value in TOOL_KINDS:
        return value
    return {
        "execute": "shell",
        "read": "filesystem.read",
        "edit": "filesystem.write",
        "delete": "filesystem.write",
        "move": "filesystem.write",
        "search": "search",
    }.get(value, "")


def _acp_content_text(value: Any) -> str:
    if not isinstance(value, dict) or value.get("type") != "text":
        return ""
    text = value.get("text")
    return text if isinstance(text, str) else ""


def _acp_tool_content_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "content":
            text = _acp_content_text(item.get("content"))
            if text:
                parts.append(text)
        elif item_type == "diff":
            path = item.get("path")
            if isinstance(path, str):
                parts.append(f"edited {path}")
        elif item_type == "terminal":
            terminal_id = item.get("terminalId", item.get("terminal_id", ""))
            parts.append(f"[terminal {terminal_id}]")
    return "\n".join(parts)


def _acp_value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)
