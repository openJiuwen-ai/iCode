# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixture-driven Anthropic hosted-tool normalization and replay tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage, BetaThinkingBlock

from chrys.foundation.hosted_tools import ANTHROPIC_HOSTED_WIRE_BLOCK_KEY, HostedToolPhase
from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, Message
from chrys.service.llm.anthropic_messages.decode import blocking_context_estimate, decode_blocks, decode_message
from chrys.service.llm.anthropic_messages.history import encode_messages
from chrys.service.llm.anthropic_messages.stream import StreamState
from tests.support.oversized_int import oversized_integer_json


def _replayed_blocks(message: Message) -> list[dict[str, Any]]:
    """The wire blocks *message* encodes to, as one wire message."""
    (wire_message,) = encode_messages([message])
    return wire_message["content"]


def _blocks(fixtures: list[dict[str, Any]]) -> list[Any]:
    message = BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": fixtures,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )
    return list(message.content)


def test_redacted_thinking_parse_and_round_trip() -> None:
    (block,) = _blocks([{"type": "redacted_thinking", "data": "opaque-redacted"}])

    contents = decode_blocks([block])

    assert len(contents) == 1
    reasoning = contents[0]
    assert reasoning.type == "text_reasoning"
    assert reasoning.text is None
    assert reasoning.protected_data == "opaque-redacted"
    assert reasoning.additional_properties["anthropic_redacted_thinking"] is True
    message = Message("assistant", contents)
    assert message.text == ""
    assert _replayed_blocks(message) == [{"type": "redacted_thinking", "data": "opaque-redacted"}]


def test_streamed_redacted_thinking_start_emits_reasoning_content() -> None:
    (block,) = _blocks([{"type": "redacted_thinking", "data": "opaque-redacted"}])
    state = StreamState()

    (update,) = state.updates_for(SimpleNamespace(type="content_block_start", index=0, content_block=block))

    assert len(update.contents) == 1
    assert update.contents[0].protected_data == "opaque-redacted"


@pytest.mark.parametrize("redacted_first", [True, False])
def test_streamed_redacted_and_ordinary_thinking_round_trip_separately(redacted_first: bool) -> None:
    (redacted_block,) = _blocks([{"type": "redacted_thinking", "data": "opaque-redacted"}])
    state = StreamState()
    redacted_event = SimpleNamespace(type="content_block_start", index=0, content_block=redacted_block)
    thinking_event = SimpleNamespace(
        type="content_block_delta",
        index=1,
        delta=SimpleNamespace(type="thinking_delta", thinking="ordinary thinking"),
    )
    signature_event = SimpleNamespace(
        type="content_block_delta",
        index=1,
        delta=SimpleNamespace(type="signature_delta", signature="thinking-signature"),
    )
    events = (
        [redacted_event, thinking_event, signature_event]
        if redacted_first
        else [thinking_event, signature_event, redacted_event]
    )
    updates = [update for event in events for update in state.updates_for(event)]

    response = ChatResponse.from_updates(updates)
    prepared = _replayed_blocks(response.messages[0])

    expected_types = ["redacted_thinking", "thinking"] if redacted_first else ["thinking", "redacted_thinking"]
    assert [block["type"] for block in prepared] == expected_types
    thinking = next(block for block in prepared if block["type"] == "thinking")
    assert thinking == {
        "type": "thinking",
        "thinking": "ordinary thinking",
        "signature": "thinking-signature",
    }
    redacted = next(block for block in prepared if block["type"] == "redacted_thinking")
    assert redacted == {"type": "redacted_thinking", "data": "opaque-redacted"}


@pytest.mark.parametrize("start_signature", [None, ""], ids=["start-without-signature", "start-with-empty-signature"])
@pytest.mark.parametrize(
    "texts", [("A", "B"), ("", ""), (None, None)], ids=["thinking", "omitted", "omitted-without-delta"]
)
def test_streamed_adjacent_thinking_blocks_keep_their_own_signatures(
    start_signature: str | None, texts: tuple[str | None, str | None]
) -> None:
    """Each block keeps its own signature: omitted thinking, with an empty delta or none, and a start without one."""
    state = StreamState()
    start_fields = {} if start_signature is None else {"signature": start_signature}
    events: list[Any] = []
    for index, (text, signature) in enumerate(zip(texts, ("S1", "S2"), strict=True)):
        block = BetaThinkingBlock.model_construct(type="thinking", thinking="", **start_fields)
        events.append(SimpleNamespace(type="content_block_start", index=index, content_block=block))
        if text is not None:
            events.append(
                SimpleNamespace(
                    type="content_block_delta", index=index, delta=SimpleNamespace(type="thinking_delta", thinking=text)
                )
            )
        events.append(
            SimpleNamespace(
                type="content_block_delta",
                index=index,
                delta=SimpleNamespace(type="signature_delta", signature=signature),
            )
        )
    updates = [update for event in events for update in state.updates_for(event)]

    response = ChatResponse.from_updates(updates)
    prepared = _replayed_blocks(response.messages[0])

    assert prepared == [
        {"type": "thinking", "thinking": texts[0] or "", "signature": "S1"},
        {"type": "thinking", "thinking": texts[1] or "", "signature": "S2"},
    ]


def _stream_parse(blocks: list[Any]) -> ChatResponse:
    state = StreamState()
    updates: list[ChatResponseUpdate] = []
    for index, block in enumerate(blocks):
        if block.type in {"server_tool_use", "mcp_tool_use"}:
            initial = block.model_copy(update={"input": {}})
            updates.extend(
                state.updates_for(SimpleNamespace(type="content_block_start", index=index, content_block=initial))
            )
            input_delta = SimpleNamespace(type="input_json_delta", partial_json=json.dumps(block.input))
            updates.extend(
                state.updates_for(SimpleNamespace(type="content_block_delta", index=index, delta=input_delta))
            )
        else:
            updates.extend(
                state.updates_for(SimpleNamespace(type="content_block_start", index=index, content_block=block))
            )
        if block.type in {"server_tool_use", "mcp_tool_use", "tool_use"}:
            list(state.updates_for(SimpleNamespace(type="content_block_stop", index=index)))
    updates.extend(state.updates_for(SimpleNamespace(type="message_stop")))
    state.finish()
    return ChatResponse.from_updates(updates)


def test_streamed_hosted_call_is_refreshed_in_place_after_it_is_emitted() -> None:
    """The block start's call object is re-emitted and mutated as its input streams.

    Response assembly skips a content object it has already seen, so the
    assembled message keeps that one object; a fresh copy per fragment would
    add a call per fragment instead.
    """
    (block,) = _blocks([{"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}}])
    state = StreamState()

    (start,) = state.updates_for(SimpleNamespace(type="content_block_start", index=0, content_block=block))
    (call,) = start.contents
    assert (call.provider_phase, call.arguments) == (HostedToolPhase.START, {})

    seen_arguments: list[Any] = []
    for fragment in ('{"query": ', '"Chrys"}'):
        delta = SimpleNamespace(type="input_json_delta", partial_json=fragment)
        (update,) = state.updates_for(SimpleNamespace(type="content_block_delta", index=0, delta=delta))
        (refreshed,) = update.contents
        assert refreshed is call
        assert call.provider_phase == HostedToolPhase.DELTA
        seen_arguments.append(call.arguments)
    assert seen_arguments == ['{"query": ', {"query": "Chrys"}]
    assert call.additional_properties[ANTHROPIC_HOSTED_WIRE_BLOCK_KEY]["input"] == {"query": "Chrys"}

    assert list(state.updates_for(SimpleNamespace(type="content_block_stop", index=0))) == []
    assert call.provider_phase == HostedToolPhase.START
    assert ChatResponse.from_updates([start, update]).messages[0].contents == [call]


def test_streamed_hosted_call_keeps_oversized_integer_input_as_raw_text() -> None:
    """An integer past Python's conversion limit is not decoded; the raw text is kept."""
    (block,) = _blocks([{"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {}}])
    state = StreamState()
    (start,) = state.updates_for(SimpleNamespace(type="content_block_start", index=0, content_block=block))
    (call,) = start.contents
    raw_input = oversized_integer_json()

    (update,) = state.updates_for(
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json=raw_input),
        )
    )

    assert update.contents == [call]
    assert call.arguments == raw_input
    assert call.provider_phase == HostedToolPhase.DELTA


def _content_dicts(contents: list[Content]) -> list[dict[str, Any]]:
    return [content.to_dict() for content in contents]


def _blocking_code_search_content() -> list[SimpleNamespace]:
    content: list[SimpleNamespace] = []
    for index in range(3):
        code_id = f"code_{index}"
        content.extend(
            [
                SimpleNamespace(type="server_tool_use", name="code_execution", id=code_id),
                SimpleNamespace(type="server_tool_use", name="web_search", id=f"search_{index}"),
                SimpleNamespace(type="web_search_tool_result", tool_use_id=f"search_{index}"),
                SimpleNamespace(type="code_execution_tool_result", tool_use_id=code_id),
            ]
        )
    content.append(SimpleNamespace(type="text"))
    return content


@pytest.mark.parametrize(
    ("cache_creation", "cache_read", "expected"),
    [
        (3_956, 147_443, 40_826),
        (40_344, 111_195, 40_353),
    ],
    ids=["cache-hit", "cache-miss"],
)
def test_blocking_hosted_context_estimate_handles_cache_hits_and_misses(
    cache_creation: int,
    cache_read: int,
    expected: int,
) -> None:
    usage = SimpleNamespace(
        input_tokens=9,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )

    estimate = blocking_context_estimate(usage, _blocking_code_search_content())

    assert estimate == expected


def test_blocking_message_publishes_context_estimate() -> None:
    message = BetaMessage.model_validate(
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": [
                {"type": "server_tool_use", "id": "search_1", "name": "web_search", "input": {"query": "x"}},
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "search_1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://example.test",
                            "title": "Example",
                            "encrypted_content": "opaque",
                        }
                    ],
                },
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 50,
                "cache_creation_input_tokens": 5_000,
                "cache_read_input_tokens": 70_000,
            },
        }
    )

    response = decode_message(message, response_format=None)

    assert response.usage_details is not None
    assert response.usage_details["context_input_token_floor"] == 5_010
    assert response.usage_details["context_input_token_estimate"] == 40_010


def _assert_parity(fixtures: list[dict[str, Any]]) -> tuple[list[Any], list[Content]]:
    blocks = _blocks(fixtures)
    blocking = decode_blocks(blocks)
    streaming = _stream_parse(blocks)
    assert _content_dicts(streaming.messages[0].contents) == _content_dicts(blocking)
    return blocks, blocking


@pytest.mark.parametrize(
    ("tool_name", "result_fixture", "expected_family"),
    [
        (
            "web_search",
            {
                "type": "web_search_tool_result",
                "tool_use_id": "srv_1",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": "https://example.test",
                        "title": "Example",
                        "encrypted_content": "opaque",
                    }
                ],
            },
            "search",
        ),
        (
            "web_fetch",
            {
                "type": "web_fetch_tool_result",
                "tool_use_id": "srv_1",
                "content": {
                    "type": "web_fetch_result",
                    "url": "https://example.test/article",
                    "content": {
                        "type": "document",
                        "source": {"type": "text", "media_type": "text/plain", "data": "article"},
                    },
                },
            },
            "fetch",
        ),
    ],
)
def test_web_search_and_fetch_pairs_have_blocking_streaming_parity(
    tool_name: str,
    result_fixture: dict[str, Any],
    expected_family: str,
) -> None:
    fixtures = [
        {"type": "server_tool_use", "id": "srv_1", "name": tool_name, "input": {"query": "Chrys"}},
        result_fixture,
    ]

    _blocks_value, contents = _assert_parity(fixtures)
    call, result = contents

    assert (call.type, result.type) == ("search_tool_call", "search_tool_result")
    assert call.hosted_family == result.hosted_family == expected_family
    assert call.retry_safety == result.retry_safety == "read_only"


@pytest.mark.parametrize("is_error", [False, True])
def test_anthropic_mcp_pair_and_error_status(is_error: bool) -> None:
    fixtures = [
        {
            "type": "mcp_tool_use",
            "id": "mcp_1",
            "name": "lookup",
            "server_name": "docs",
            "input": {"query": "Chrys"},
        },
        {
            "type": "mcp_tool_result",
            "tool_use_id": "mcp_1",
            "content": "remote failed" if is_error else "found",
            "is_error": is_error,
        },
    ]

    _blocks_value, contents = _assert_parity(fixtures)
    call, result = contents

    assert (call.type, result.type) == ("mcp_server_tool_call", "mcp_server_tool_result")
    assert call.retry_safety == "side_effectful"
    assert result.provider_status == ("failed" if is_error else "completed")
    assert result.additional_properties.get("is_error", False) is is_error


def test_code_execution_pair_includes_hosted_file() -> None:
    fixtures = [
        {
            "type": "server_tool_use",
            "id": "code_1",
            "name": "code_execution",
            "input": {"code": "print('hi')"},
        },
        {
            "type": "code_execution_tool_result",
            "tool_use_id": "code_1",
            "content": {
                "type": "code_execution_result",
                "stdout": "hi\n",
                "stderr": "",
                "return_code": 0,
                "content": [{"type": "code_execution_output", "file_id": "file_1"}],
            },
        },
    ]

    _blocks_value, contents = _assert_parity(fixtures)
    call, result = contents

    assert (call.type, result.type) == ("code_interpreter_tool_call", "code_interpreter_tool_result")
    assert [item.text for item in call.inputs or []] == ["print('hi')"]
    assert [output.type for output in result.outputs] == ["text", "hosted_file"]
    assert call.retry_safety == "sandboxed"


@pytest.mark.parametrize(
    ("result_content", "expected_status"),
    [
        (
            {
                "type": "bash_code_execution_result",
                "stdout": "ok",
                "stderr": "",
                "return_code": 0,
                "content": [],
            },
            "completed",
        ),
        (
            {"type": "bash_code_execution_tool_result_error", "error_code": "execution_time_exceeded"},
            "failed",
        ),
    ],
)
def test_bash_call_and_result_map_symmetrically(
    result_content: dict[str, Any],
    expected_status: str,
) -> None:
    fixtures = [
        {
            "type": "server_tool_use",
            "id": "bash_1",
            "name": "bash_code_execution",
            "input": {"command": "pwd"},
        },
        {"type": "bash_code_execution_tool_result", "tool_use_id": "bash_1", "content": result_content},
    ]

    _blocks_value, contents = _assert_parity(fixtures)
    call, result = contents

    assert (call.type, result.type) == ("shell_tool_call", "shell_tool_result")
    assert call.commands == ["pwd"]
    assert result.provider_status == expected_status
    assert call.retry_safety == result.retry_safety == "side_effectful"


@pytest.mark.parametrize(
    ("operation", "result_content", "expected_status"),
    [
        (
            {"command": "view", "path": "/tmp/a.txt"},
            {
                "type": "text_editor_code_execution_view_result",
                "content": "hello",
                "file_type": "text",
                "start_line": 1,
                "num_lines": 1,
                "total_lines": 1,
            },
            "completed",
        ),
        (
            {"command": "create", "path": "/tmp/a.txt", "file_text": "hello"},
            {"type": "text_editor_code_execution_create_result", "is_file_update": False},
            "completed",
        ),
        (
            {"command": "view", "path": "/missing"},
            {
                "type": "text_editor_code_execution_tool_result_error",
                "error_code": "file_not_found",
                "error_message": "missing",
            },
            "failed",
        ),
    ],
)
def test_text_editor_view_create_and_error_use_file_operation_pair(
    operation: dict[str, Any],
    result_content: dict[str, Any],
    expected_status: str,
) -> None:
    fixtures = [
        {
            "type": "server_tool_use",
            "id": "edit_1",
            "name": "text_editor_code_execution",
            "input": operation,
        },
        {
            "type": "text_editor_code_execution_tool_result",
            "tool_use_id": "edit_1",
            "content": result_content,
        },
    ]

    _blocks_value, contents = _assert_parity(fixtures)
    call, result = contents

    assert (call.type, result.type) == ("hosted_tool_call", "hosted_tool_result")
    assert call.hosted_family == result.hosted_family == "file_operation"
    assert result.provider_status == expected_status


def test_text_editor_error_without_message_uses_error_code_as_display_message() -> None:
    fixtures = [
        {
            "type": "server_tool_use",
            "id": "edit_error",
            "name": "text_editor_code_execution",
            "input": {"command": "view", "path": "/missing"},
        },
        {
            "type": "text_editor_code_execution_tool_result",
            "tool_use_id": "edit_error",
            "content": {
                "type": "text_editor_code_execution_tool_result_error",
                "error_code": "file_not_found",
            },
        },
    ]

    _blocks_value, contents = _assert_parity(fixtures)
    result = contents[1]
    error = result.items[0]

    assert result.provider_status == "failed"
    assert error.type == "error"
    assert error.error_code == "file_not_found"
    assert error.message == "file_not_found"


def test_tool_search_pair_uses_exact_official_name_and_round_trips() -> None:
    # doc-derived fixture; confirm via probe P-4
    fixtures = [
        {
            "type": "server_tool_use",
            "id": "search_1",
            "name": "tool_search_tool_bm25",
            "input": {"query": "weather"},
        },
        {
            "type": "tool_search_tool_result",
            "tool_use_id": "search_1",
            "content": {
                "type": "tool_search_tool_search_result",
                "tool_references": [{"type": "tool_reference", "tool_name": "get_weather"}],
            },
        },
    ]

    blocks, contents = _assert_parity(fixtures)
    replayed = encode_messages([Message("assistant", contents)])

    assert [content.type for content in contents] == ["hosted_tool_call", "hosted_tool_result"]
    assert all(content.hosted_family == "tool_discovery" for content in contents)
    assert replayed == [
        {
            "role": "assistant",
            "content": [block.model_dump(mode="json", exclude_none=True, exclude_unset=True) for block in blocks],
        }
    ]


def test_unknown_server_tool_falls_back_to_generic_call() -> None:
    fixtures = [{"type": "server_tool_use", "id": "advisor_1", "name": "advisor", "input": {"question": "why"}}]

    _blocks_value, contents = _assert_parity(fixtures)

    assert len(contents) == 1
    assert contents[0].type == "hosted_tool_call"
    assert contents[0].hosted_family == "generic"
    assert contents[0].retry_safety == "unknown"


def test_ordinary_tool_use_stays_client_executed_with_streaming_parity() -> None:
    fixtures = [{"type": "tool_use", "id": "tool_1", "name": "code_execution", "input": {"x": 1}}]

    _blocks_value, contents = _assert_parity(fixtures)

    assert len(contents) == 1
    assert contents[0].type == "function_call"
    assert contents[0].provider_hosted is False


def test_anthropic_error_results_set_structured_failure_metadata() -> None:
    fixtures = [
        {
            "type": "server_tool_use",
            "id": "code_1",
            "name": "code_execution",
            "input": {"code": "raise RuntimeError"},
        },
        {
            "type": "code_execution_tool_result",
            "tool_use_id": "code_1",
            "content": {"type": "code_execution_tool_result_error", "error_code": "execution_time_exceeded"},
        },
    ]

    _blocks_value, contents = _assert_parity(fixtures)
    result = contents[1]

    assert result.provider_status == "failed"
    assert result.additional_properties["is_error"] is True
    assert result.additional_properties["error"]["error_code"] == "execution_time_exceeded"


def test_anthropic_all_hosted_pairs_same_provider_round_trip() -> None:
    fixtures = [
        {"type": "server_tool_use", "id": "web_1", "name": "web_search", "input": {"query": "Chrys"}},
        {
            "type": "web_search_tool_result",
            "tool_use_id": "web_1",
            "content": [
                {
                    "type": "web_search_result",
                    "url": "https://example.test",
                    "title": "Example",
                    "encrypted_content": "opaque",
                }
            ],
        },
        {
            "type": "server_tool_use",
            "id": "bash_1",
            "name": "bash_code_execution",
            "input": {"command": "pwd"},
        },
        {
            "type": "bash_code_execution_tool_result",
            "tool_use_id": "bash_1",
            "content": {
                "type": "bash_code_execution_result",
                "stdout": "/tmp",
                "stderr": "",
                "return_code": 0,
                "content": [],
            },
        },
        {
            "type": "server_tool_use",
            "id": "edit_1",
            "name": "text_editor_code_execution",
            "input": {"command": "create", "path": "/tmp/a.txt", "file_text": "x"},
        },
        {
            "type": "text_editor_code_execution_tool_result",
            "tool_use_id": "edit_1",
            "content": {"type": "text_editor_code_execution_create_result", "is_file_update": False},
        },
    ]
    blocks = _blocks(fixtures)
    contents = decode_blocks(blocks)

    replayed = encode_messages([Message("assistant", contents)])

    assert replayed == [
        {
            "role": "assistant",
            "content": [block.model_dump(mode="json", exclude_none=True, exclude_unset=True) for block in blocks],
        }
    ]


@pytest.mark.parametrize("source_provider", ["openai", "deepseek-openai"])
@pytest.mark.parametrize("family", ["search", "generic"])
def test_anthropic_cross_provider_hosted_history_degrades_to_assistant_context(
    source_provider: str,
    family: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if family == "search":
        call = Content.from_search_tool_call(
            "foreign-1",
            tool_name="web_search",
            arguments={"query": "Chrys"},
            status="running",
            hosted_provider=source_provider,
            provider_phase="start",
        )
        result = Content.from_search_tool_result(
            "foreign-1",
            tool_name="web_search",
            result="found",
            status="completed",
            hosted_provider=source_provider,
            provider_phase="terminal",
        )
    else:
        call = Content.from_hosted_tool_call(
            "foreign-1",
            tool_name="remote_task",
            arguments={"task": "inspect"},
            status="running",
            hosted_provider=source_provider,
            provider_phase="start",
        )
        result = Content.from_hosted_tool_result(
            "foreign-1",
            tool_name="remote_task",
            result="found",
            status="completed",
            hosted_provider=source_provider,
            provider_phase="terminal",
        )

    with caplog.at_level("DEBUG", logger="chrys.service.agent_middleware.events.hosted_tools"):
        replayed = encode_messages([Message("assistant", [call]), Message("assistant", [result])])

    assert len(replayed) == 1
    assert replayed[0]["role"] == "assistant"
    assert [block["type"] for block in replayed[0]["content"]] == ["text"]
    summary = replayed[0]["content"][0]["text"]
    assert "[Provider-hosted tool context]" in summary
    assert "Status: completed" in summary
    assert 'Result: "found"' in summary
    assert "Degrading provider-hosted history to assistant context" in caplog.text


def test_foreign_hosted_summary_in_a_user_message_goes_out_as_assistant_context() -> None:
    result = Content.from_hosted_tool_result(
        "foreign-1",
        tool_name="remote_task",
        result="found",
        status="completed",
        hosted_provider="openai",
        provider_phase="terminal",
    )

    replayed = encode_messages([Message("user", ["before", result, "after"])])

    assert [(message["role"], [block["type"] for block in message["content"]]) for message in replayed] == [
        ("user", ["text"]),
        ("assistant", ["text"]),
        ("user", ["text"]),
    ]
    assert replayed[1]["content"][0]["text"].startswith("[Provider-hosted tool context]")
