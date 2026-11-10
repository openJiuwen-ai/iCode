# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serializers for session state objects (CompressedBlock, Message, etc.).

Provides JSON-compatible serialization/deserialization for persisting
session state to disk via JsonFileStateStore.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from chrys.kernel import Message
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.session.runtime_metadata import (
    CONTEXT_CALIBRATION_KEY,
    LAST_USAGE_KEY,
    RELAY_RISK_SEEN_KEY,
    TOTAL_SESSION_CACHE_HIT_TOKENS_KEY,
    TOTAL_SESSION_INPUT_TOKENS_KEY,
    TOTAL_SESSION_OUTPUT_TOKENS_KEY,
    TOTAL_SESSION_TOKENS_KEY,
)
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY

# ``context_calibration`` must survive the round trip: restore-time strategy
# rehydration reads it from the persisted state (an allowlist miss here would
# silently disable rehydration while in-memory round-trip tests stay green).
_COPY_AS_DICT_KEYS = (
    LAST_USAGE_KEY,
    CONTEXT_CALIBRATION_KEY,
)
_COPY_AS_LIST_KEYS = ("agent_profile_switches",)
_PASS_THROUGH_OPTIONAL_KEYS = (
    "chrys_mutations",
    "chrys_workspace_baseline",
    # Session todo list — truthy filter drops empty lists (empty ≡ absent).
    "chrys_todos",
    # Phase 4 LAST_WORDS note — replaces the compacted turn's dropped
    # tool-call history, so a resume after restart can re-inject it.
    "last_words",
    # JSON-only siblings: code-generated dropped-call stubs and the
    # attempt/spend breaker for the interrupted logical turn.
    "last_words_manifest",
    "last_words_breaker",
    TOTAL_SESSION_TOKENS_KEY,
    TOTAL_SESSION_INPUT_TOKENS_KEY,
    TOTAL_SESSION_OUTPUT_TOKENS_KEY,
    # Relay-risk latch — truthy filter drops False (absent ≡ never relayed).
    RELAY_RISK_SEEN_KEY,
)
# Keys where ``None`` and ``0`` are semantically distinct (e.g. provider
# didn't report vs. reported zero), so the truthy-only filter above would
# corrupt round-trips.
_PASS_THROUGH_NULLABLE_KEYS = (
    CATALOG_POINTER_RECORD_COUNT_STATE_KEY,
    TOTAL_SESSION_CACHE_HIT_TOKENS_KEY,
    # Trajectory turn registry — identity, opening item and client message id
    # per turn; rollback reads it to name the superseded turns, and replays
    # read it to label history. Empty is a value here (a session whose turns
    # were all rolled away), so the truthy filter must not drop it.
    TRAJECTORY_STATE_KEY,
)


def serialize_message(msg: Message) -> dict[str, Any]:
    """Convert a Message to a JSON-serializable dict via framework serialization."""
    return msg.to_dict()


def deserialize_message(data: dict[str, Any]) -> Message:
    """Reconstruct a Message from a serialized dict via framework deserialization."""
    return Message.from_dict(data)


def serialized_message_payload(msg: Message) -> str:
    """Canonical JSON payload string for a message.

    Injected into ``chrys.kernel.LoopRecorder`` as the
    ``message_hasher`` (the kernel package cannot import chrys, so the
    serializer is supplied from here) — checkpoint suppression keys on this
    payload's hash staying stable across equal snapshots.
    """
    return json.dumps(
        serialize_message(msg),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def serialize_compressed_block(block: CompressedBlock) -> dict[str, Any]:
    """Convert a CompressedBlock to a JSON-serializable dict."""
    return {
        "compressed_context_id": block.compressed_context_id,
        "messages": [serialize_message(m) for m in block.messages],
        "summary_text": block.summary_text,
        "marker_id": block.marker_id,
        "turn_range": list(block.turn_range),
        "created_at": block.created_at,
    }


def deserialize_compressed_block(data: dict[str, Any]) -> CompressedBlock:
    """Reconstruct a CompressedBlock from a serialized dict."""
    return CompressedBlock(
        compressed_context_id=data["compressed_context_id"],
        messages=[deserialize_message(m) for m in data.get("messages", [])],
        summary_text=data["summary_text"],
        marker_id=data.get("marker_id", ""),
        turn_range=tuple(data.get("turn_range", [0, 0])),
        created_at=data.get("created_at", ""),
    )


def serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Serialize the full provider state to JSON-safe dict."""
    result: dict[str, Any] = {
        "messages": [serialize_message(m) for m in state.get("messages", [])],
        "compressed_msgs": [serialize_compressed_block(b) for b in state.get("compressed_msgs", [])],
        "turn_counter": state.get("turn_counter", 0),
    }
    _copy_optional_state_keys(state, result)
    return result


def deserialize_state(data: dict[str, Any]) -> dict[str, Any]:
    """Deserialize provider state from a JSON dict."""
    result: dict[str, Any] = {
        "messages": [deserialize_message(m) for m in data.get("messages", [])],
        "compressed_msgs": [deserialize_compressed_block(b) for b in data.get("compressed_msgs", [])],
        "turn_counter": data.get("turn_counter", 0),
    }
    _copy_optional_state_keys(data, result)
    return result


def _copy_optional_state_keys(source: dict[str, Any], target: dict[str, Any]) -> None:
    """Copy optional provider-state keys, preserving existing truthy-only behavior."""
    for key in _COPY_AS_DICT_KEYS:
        value = source.get(key)
        # Mapping-gated: a corrupted state file must degrade to "record
        # absent" (downstream restore validates leniently), not crash the
        # whole session load inside ``dict()``.  Truthiness keeps the
        # table-wide empty ≡ absent filter.
        if value and isinstance(value, Mapping):
            target[key] = dict(value)
    for key in _COPY_AS_LIST_KEYS:
        if source.get(key):
            target[key] = list(source[key])
    for key in _PASS_THROUGH_OPTIONAL_KEYS:
        if source.get(key):
            target[key] = source[key]
    for key in _PASS_THROUGH_NULLABLE_KEYS:
        if source.get(key) is not None:
            target[key] = source[key]
