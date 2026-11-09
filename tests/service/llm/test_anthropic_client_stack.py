# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the production Anthropic client stack: headers, output_config, usage parsing, and thinking blocks."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from chrys.foundation.util.chrys_headers import (
    MODEL_ID_HEADER,
    PARENT_SESSION_ID_HEADER,
    SESSION_ID_HEADER,
    X_PARENT_SESSION_ID_HEADER,
    X_SESSION_ID_HEADER,
)
from chrys.kernel import Content, Message
from chrys.kernel.exceptions import ChatClientInvalidRequestException
from chrys.service.llm.anthropic_messages import AnthropicMessagesClient
from chrys.service.llm.anthropic_messages.decode import decode_usage
from chrys.service.llm.anthropic_messages.stream import StreamState
from chrys.service.llm.clients import _assemble_stack
from tests.support.images import image_bytes


def _make_anthropic_client(*, session_id: str | None = None, parent_session_id: str | None = None) -> Any:
    """Construct the production Anthropic stack over a placeholder SDK client."""
    return _assemble_stack(
        AnthropicMessagesClient,
        SimpleNamespace(base_url="https://api.anthropic.com", default_headers={}),  # type: ignore[arg-type]
        model_id="claude-default",
        session_id=session_id,
        parent_session_id=parent_session_id,
        use_route_session_context=False,
        on_intermediate_text=None,
        connection={},
        max_iterations=7777,
        max_consecutive_errors=10,
        tool_result_ceiling_tokens=None,
    )


def test_anthropic_build_request_sets_model_header_from_effective_model() -> None:
    chat_client = _make_anthropic_client()

    prepared = chat_client._build_request(
        [Message("user", ["hi"])],
        {
            "model": "claude-final",
            "extra_headers": {
                "chrys-debug": "drop-me",
                MODEL_ID_HEADER: "wrong",
            },
        },
        {},
    )

    assert prepared["model"] == "claude-final"
    assert prepared["extra_headers"][MODEL_ID_HEADER] == "claude-final"
    assert "chrys-debug" not in prepared["extra_headers"]


def test_anthropic_response_format_uses_ga_output_config() -> None:
    class StructuredPayload(BaseModel):
        answer: str

    chat_client = _make_anthropic_client()

    prepared = chat_client._build_request(
        [Message("user", ["hi"])],
        {"response_format": StructuredPayload},
        {},
    )

    assert "output_format" not in prepared
    assert prepared["output_config"]["format"] == {
        "type": "json_schema",
        "schema": {
            "properties": {"answer": {"title": "Answer", "type": "string"}},
            "required": ["answer"],
            "title": "StructuredPayload",
            "type": "object",
            "additionalProperties": False,
        },
    }
    assert "structured-outputs-2025-11-13" not in prepared["extra_headers"]["anthropic-beta"]
    assert "betas" not in prepared


def test_anthropic_response_format_preserves_output_config_without_mutating_caller() -> None:
    class StructuredPayload(BaseModel):
        answer: str

    chat_client = _make_anthropic_client()
    caller_output_config = {"effort": "high"}

    prepared = chat_client._build_request(
        [Message("user", ["hi"])],
        {"response_format": StructuredPayload, "output_config": caller_output_config},
        {},
    )

    assert prepared["output_config"]["effort"] == "high"
    assert prepared["output_config"]["format"]["type"] == "json_schema"
    assert caller_output_config == {"effort": "high"}


def test_anthropic_response_format_conflicts_with_explicit_output_config_format() -> None:
    class StructuredPayload(BaseModel):
        answer: str

    chat_client = _make_anthropic_client()

    with pytest.raises(ChatClientInvalidRequestException, match="cannot be combined"):
        chat_client._build_request(
            [Message("user", ["hi"])],
            {
                "response_format": StructuredPayload,
                "output_config": {"format": {"type": "json_schema", "schema": {}}},
            },
            {},
        )


def test_anthropic_without_response_format_does_not_add_output_config() -> None:
    chat_client = _make_anthropic_client()

    prepared = chat_client._build_request([Message("user", ["hi"])], {}, {})

    assert "output_config" not in prepared
    assert "output_format" not in prepared


def test_anthropic_build_request_sets_session_headers_from_session_id() -> None:
    chat_client = _make_anthropic_client(session_id="sess-456", parent_session_id="parent-456")

    prepared = chat_client._build_request(
        [Message("user", ["hi"])],
        {
            "extra_headers": {
                X_SESSION_ID_HEADER: "wrong",
                X_PARENT_SESSION_ID_HEADER: "wrong-parent",
                "X-Session-Id": "wrong-mixed-case",
                SESSION_ID_HEADER: "wrong",
                PARENT_SESSION_ID_HEADER: "wrong-parent",
            },
        },
        {},
    )

    assert prepared["extra_headers"][X_SESSION_ID_HEADER] == "sess-456"
    assert prepared["extra_headers"][SESSION_ID_HEADER] == "sess-456"
    assert prepared["extra_headers"][X_PARENT_SESSION_ID_HEADER] == "parent-456"
    assert prepared["extra_headers"][PARENT_SESSION_ID_HEADER] == "parent-456"
    assert "X-Session-Id" not in prepared["extra_headers"]


def test_anthropic_decode_usage_counts_cache_tokens_as_context_input() -> None:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=30,
    )

    details = decode_usage(usage)

    assert details is not None
    assert details["input_token_count"] == 150
    assert details["output_token_count"] == 50
    assert details["anthropic.cache_creation_input_tokens"] == 20
    assert details["anthropic.cache_read_input_tokens"] == 30
    assert details["cache_creation_input_token_count"] == 20
    assert details["cache_read_input_token_count"] == 30
    assert details["context_input_token_floor"] == 120


def test_anthropic_decode_usage_preserves_uncached_input_tokens() -> None:
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=None,
    )

    details = decode_usage(usage)

    assert details is not None
    assert details["input_token_count"] == 100
    assert details["output_token_count"] == 50


def test_anthropic_decode_usage_does_not_invent_input_for_sparse_delta() -> None:
    """Sparse stream deltas must not overwrite message_start input counts."""
    usage = SimpleNamespace(
        input_tokens=None,
        output_tokens=50,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=30,
    )

    details = decode_usage(usage)

    assert details is not None
    assert "input_token_count" not in details
    assert details["output_token_count"] == 50
    assert details["anthropic.cache_read_input_tokens"] == 30
    assert details["cache_read_input_token_count"] == 30


def test_anthropic_stream_usage_exposes_final_context_occupancy() -> None:
    """Terminal hosted-loop usage excludes cumulative internal cache reads from context."""
    state = StreamState()
    start_usage = SimpleNamespace(
        input_tokens=6,
        output_tokens=1,
        cache_creation_input_tokens=36_301,
        cache_read_input_tokens=100,
    )
    list(
        state.updates_for(
            SimpleNamespace(  # type: ignore[arg-type]
                type="message_start",
                message=SimpleNamespace(
                    usage=start_usage,
                    id="msg_1",
                    content=[],
                    model="claude-default",
                    stop_reason=None,
                ),
            )
        )
    )
    final_usage = SimpleNamespace(
        input_tokens=9,
        output_tokens=1_657,
        cache_creation_input_tokens=41_173,
        cache_read_input_tokens=111_175,
    )

    (update,) = state.updates_for(
        SimpleNamespace(  # type: ignore[arg-type]
            type="message_delta",
            usage=final_usage,
            delta=SimpleNamespace(stop_reason="end_turn"),
        )
    )

    assert len(update.contents) == 1
    details = update.contents[0].usage_details
    assert details is not None
    assert details["input_token_count"] == 152_357
    assert details["context_input_token_floor"] == 41_182
    assert details["context_input_token_count"] == 41_282


def test_anthropic_build_request_drops_unsigned_thinking_blocks() -> None:
    chat_client = _make_anthropic_client()

    prepared = chat_client._build_request(
        [
            Message("user", ["describe @one.png"]),
            Message(
                "assistant",
                [
                    Content.from_text_reasoning(text="unsigned private reasoning"),
                    Content.from_text("visible answer"),
                ],
            ),
            Message(
                "user",
                [
                    "compare these",
                    Content.from_data(data=image_bytes("PNG"), media_type="image/png"),
                    Content.from_data(data=image_bytes("JPEG"), media_type="image/jpeg"),
                ],
            ),
        ],
        {},
        {},
    )

    assistant = prepared["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [{"type": "text", "text": "visible answer"}]
    image_blocks = [block for block in prepared["messages"][2]["content"] if block["type"] == "image"]
    assert [block["source"]["media_type"] for block in image_blocks] == ["image/png", "image/jpeg"]


def test_anthropic_build_request_drops_empty_messages_after_unsigned_thinking_filter() -> None:
    chat_client = _make_anthropic_client()

    prepared = chat_client._build_request(
        [
            Message("user", ["before"]),
            Message("assistant", [Content.from_text_reasoning(text="unsigned private reasoning")]),
            Message("assistant", [""]),
            Message("user", ["after"]),
        ],
        {},
        {},
    )

    assert prepared["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "before"}]},
        {"role": "user", "content": [{"type": "text", "text": "after"}]},
    ]


def test_anthropic_build_request_preserves_signed_thinking_blocks() -> None:
    chat_client = _make_anthropic_client()

    prepared = chat_client._build_request(
        [
            Message(
                "assistant",
                [
                    Content.from_text_reasoning(text="signed private reasoning", protected_data="sig-123"),
                    Content.from_text("visible answer"),
                ],
            )
        ],
        {},
        {},
    )

    assert prepared["messages"][0]["content"][0] == {
        "type": "thinking",
        "thinking": "signed private reasoning",
        "signature": "sig-123",
    }
    assert prepared["messages"][0]["content"][1] == {"type": "text", "text": "visible answer"}


def test_anthropic_build_request_keeps_redacted_and_drops_unsigned_thinking() -> None:
    chat_client = _make_anthropic_client()
    prepared = chat_client._build_request(
        [
            Message(
                "assistant",
                [
                    Content.from_text_reasoning(
                        protected_data="opaque-redacted",
                        additional_properties={"anthropic_redacted_thinking": True},
                    ),
                    Content.from_text_reasoning(text="unsigned private reasoning"),
                ],
            )
        ],
        {},
        {},
    )

    assert prepared["messages"][0]["content"] == [{"type": "redacted_thinking", "data": "opaque-redacted"}]
