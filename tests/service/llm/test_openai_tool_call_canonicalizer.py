# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Chat Completions tool-call canonicalizer and argument repair on history replay."""

from __future__ import annotations

from typing import Any

import pytest

from chrys.kernel import Content, Message
from chrys.service.llm.chat_completions.client import OPENAI
from chrys.service.llm.chat_completions.history import encode_messages
from tests.service.llm._wire_stacks import make_chat_client
from tests.support.openai_chat_wire import scripted_openai
from tests.support.oversized_int import oversized_integer_json


def _encode(chat_client: Any, message: Message) -> list[dict[str, Any]]:
    """The wire messages one kernel message becomes in a request."""
    return encode_messages([message], variant=chat_client.VARIANT)


def test_openai_encode_message_merges_text_and_tool_calls_for_vllm_replay() -> None:
    """Text + tool call from one kernel message must stay one wire message."""
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [
                Content.from_text("I'll read it."),
                Content.from_function_call(call_id="call_bad", name="read_file", arguments="{}"),
            ],
        ),
    )

    assert len(prepared) == 1
    assert prepared[0]["role"] == "assistant"
    assert prepared[0]["content"] == "I'll read it."
    assert prepared[0]["tool_calls"] == [
        {
            "id": "call_bad",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
    ]


def test_openai_encode_message_function_call_only_includes_empty_content() -> None:
    """Strict OpenAI-compatible servers reject assistant tool_calls without content."""
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [Content.from_function_call(call_id="call_bad", name="read_file", arguments="{}")],
        ),
    )

    assert prepared == [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
            "content": "",
        }
    ]


def test_openai_encode_message_repairs_malformed_tool_call_arguments() -> None:
    """Strict OpenAI-compatible servers reject malformed historical arguments."""
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [Content.from_function_call(call_id="call_bad", name="glob", arguments="{")],
        ),
    )

    assert prepared == [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
            "content": "",
        }
    ]


def test_openai_encode_message_repairs_oversized_integer_tool_call_arguments() -> None:
    """An integer past Python's conversion limit cannot be re-sent; replay falls back to "{}"."""
    chat_client = make_chat_client()
    arguments = oversized_integer_json()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [Content.from_function_call(call_id="call_bad", name="glob", arguments=arguments)],
        ),
    )

    assert prepared[0]["tool_calls"][0]["function"]["arguments"] == "{}"


@pytest.mark.parametrize("arguments", [None, [], 123])
def test_openai_encode_message_repairs_non_string_tool_call_arguments(arguments: object) -> None:
    """OpenAI Chat Completions requires function arguments to be a JSON string."""
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [Content.from_function_call(call_id="call_bad", name="glob", arguments=arguments)],
        ),
    )

    assert prepared[0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_openai_encode_message_repairs_only_bad_arguments_in_parallel_batch() -> None:
    """One malformed parallel call must not poison the whole vLLM replay."""
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [
                Content.from_text("I'll search these."),
                Content.from_function_call(
                    call_id="call_a",
                    name="grep",
                    arguments='{"pattern": "needle", "path": "."}',
                ),
                Content.from_function_call(
                    call_id="call_b",
                    name="glob",
                    arguments="{",
                ),
                Content.from_function_call(
                    call_id="call_c",
                    name="read_file",
                    arguments='{"path": "README.md"}',
                ),
            ],
        ),
    )

    assert len(prepared) == 1
    assert prepared[0]["role"] == "assistant"
    assert prepared[0]["content"] == "I'll search these."
    assert prepared[0]["tool_calls"] == [
        {
            "id": "call_a",
            "type": "function",
            "function": {"name": "grep", "arguments": '{"pattern": "needle", "path": "."}'},
        },
        {
            "id": "call_b",
            "type": "function",
            "function": {"name": "glob", "arguments": "{}"},
        },
        {
            "id": "call_c",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
        },
    ]


@pytest.mark.parametrize("text_first", [True, False])
def test_openai_canonicalizer_keeps_reasoning_aggregate_and_multimodal_fragment(text_first: bool) -> None:
    """The reasoning aggregate is complete by construction; the canonicalizer
    must not absorb the excluded image fragment into it (or vice versa)."""
    chat_client = make_chat_client()
    text = Content.from_text("Look at this.")
    image = Content.from_uri(uri="https://example.com/img.png", media_type="image/png")
    reasoning = Content.from_text_reasoning(
        text="chain",
        additional_properties={"openai_reasoning_format": "reasoning_content"},
    )
    function_call = Content.from_function_call(call_id="call_1", name="lookup", arguments="{}")
    contents = [text, image, reasoning, function_call] if text_first else [image, text, reasoning, function_call]

    prepared = _encode(chat_client, Message("assistant", contents))

    assert len(prepared) == 2
    image_message, aggregate = prepared
    assert image_message["content"][0]["type"] == "image_url"
    assert "reasoning_content" not in image_message
    assert "tool_calls" not in image_message
    assert aggregate["content"] == "Look at this."
    assert aggregate["reasoning_content"] == "chain"
    assert aggregate["tool_calls"][0]["function"]["name"] == "lookup"


def test_openai_canonicalizer_zero_text_reasoning_aggregate_not_merged_with_image() -> None:
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [
                Content.from_uri(uri="https://example.com/img.png", media_type="image/png"),
                Content.from_text_reasoning(
                    text="chain",
                    additional_properties={"openai_reasoning_format": "reasoning_content"},
                ),
                Content.from_function_call(call_id="call_1", name="lookup", arguments="{}"),
            ],
        ),
    )

    assert len(prepared) == 2
    image_message, aggregate = prepared
    assert image_message["content"][0]["type"] == "image_url"
    assert aggregate["content"] == ""
    assert aggregate["reasoning_content"] == "chain"
    assert aggregate["tool_calls"][0]["function"]["name"] == "lookup"


def test_openai_canonicalizer_keeps_vllm_reasoning_aggregate_separate_from_image() -> None:
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [
                Content.from_uri(uri="https://example.com/img.png", media_type="image/png"),
                Content.from_text("Look at this."),
                Content.from_text_reasoning(
                    text="chain",
                    additional_properties={"openai_reasoning_format": "reasoning"},
                ),
                Content.from_function_call(call_id="call_1", name="lookup", arguments="{}"),
            ],
        ),
    )

    assert len(prepared) == 2
    image_message, aggregate = prepared
    assert image_message["content"][0]["type"] == "image_url"
    assert "reasoning" not in image_message
    assert aggregate["content"] == "Look at this."
    assert aggregate["reasoning"] == "chain"
    assert aggregate["tool_calls"][0]["function"]["name"] == "lookup"


def test_openai_canonicalizer_preserves_multimodal_content_next_to_tool_calls() -> None:
    """A no-reasoning text+image+tool_calls history must not drop the image:
    non-empty str and list contents cannot combine, so the fragment moves
    ahead of the carrier, keeping the carrier adjacent to its tool results."""
    chat_client = make_chat_client()

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [
                Content.from_text("Preface"),
                Content.from_function_call(call_id="call_1", name="lookup", arguments="{}"),
                Content.from_uri(uri="https://example.com/img.png", media_type="image/png"),
            ],
        ),
    )

    assert len(prepared) == 2
    image_message, tool_call_message = prepared
    assert tool_call_message["content"] == "Preface"
    assert tool_call_message["tool_calls"][0]["function"]["name"] == "lookup"
    assert image_message["content"][0]["type"] == "image_url"
    assert "tool_calls" not in image_message


def test_openai_stack_no_reasoning_multimodal_keeps_carrier_adjacent_to_tool_result() -> None:
    """The tool result must directly follow the tool_calls carrier: a trailing
    image fragment may not strand between them in a full replayed history."""

    prepared = encode_messages(
        [
            Message("user", ["Q"]),
            Message(
                "assistant",
                [
                    Content.from_text("Preface"),
                    Content.from_function_call(call_id="call_1", name="lookup", arguments="{}"),
                    Content.from_uri(uri="https://example.com/img.png", media_type="image/png"),
                ],
            ),
            Message("tool", [Content.from_function_result(call_id="call_1", result="found")]),
        ],
        variant=OPENAI,
    )

    assert [message["role"] for message in prepared] == ["user", "assistant", "assistant", "tool"]
    image_message, carrier, tool_result = prepared[1], prepared[2], prepared[3]
    assert image_message["content"][0]["type"] == "image_url"
    assert carrier["content"] == "Preface"
    assert carrier["tool_calls"][0]["function"]["name"] == "lookup"
    assert tool_result["tool_call_id"] == "call_1"


@pytest.mark.parametrize("image_first", [True, False])
def test_openai_stack_plural_keeps_aggregate_adjacent_to_tool_result(image_first: bool) -> None:
    text = Content.from_text("Preface")
    function_call = Content.from_function_call(call_id="call_1", name="lookup", arguments="{}")
    image = Content.from_uri(uri="https://example.com/img.png", media_type="image/png")
    reasoning = Content.from_text_reasoning(
        text="chain",
        additional_properties={"openai_reasoning_format": "reasoning_content"},
    )
    contents = [image, text, function_call, reasoning] if image_first else [text, function_call, image, reasoning]

    prepared = encode_messages(
        [
            Message("user", ["Q"]),
            Message("assistant", contents),
            Message("tool", [Content.from_function_result(call_id="call_1", result="found")]),
        ],
        variant=OPENAI,
    )

    assert [message["role"] for message in prepared] == ["user", "assistant", "assistant", "tool"]
    image_message, aggregate, tool_result = prepared[1], prepared[2], prepared[3]
    assert image_message["content"][0]["type"] == "image_url"
    assert aggregate["content"] == "Preface"
    assert aggregate["reasoning_content"] == "chain"
    assert aggregate["tool_calls"][0]["function"]["name"] == "lookup"
    assert tool_result["tool_call_id"] == "call_1"


async def test_openai_tool_loop_replays_argument_error_with_canonical_tool_call_message() -> None:
    """Malformed tool args should not poison the next vLLM/OpenAI-compatible request."""

    from openai.types.chat.chat_completion import ChatCompletion, Choice
    from openai.types.chat.chat_completion_message import ChatCompletionMessage
    from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

    from chrys.kernel import FunctionTool
    from chrys.service.llm.chat_completions import ChatCompletionsClient
    from tests.service.llm._wire_stacks import assemble_openai_stack

    replies = [
        ChatCompletion(
            id="resp-1",
            object="chat.completion",
            created=1234567890,
            model="vllm-model",
            choices=[
                Choice(
                    index=0,
                    message=ChatCompletionMessage(
                        role="assistant",
                        content="I'll read it.",
                        tool_calls=[
                            ChatCompletionMessageToolCall(
                                id="call_bad",
                                type="function",
                                function=Function(name="read_file", arguments="{}"),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
        ),
        ChatCompletion(
            id="resp-2",
            object="chat.completion",
            created=1234567891,
            model="vllm-model",
            choices=[
                Choice(
                    index=0,
                    message=ChatCompletionMessage(role="assistant", content="Recovered."),
                    finish_reason="stop",
                )
            ],
        ),
    ]

    def read_file(path: str) -> str:
        return path

    tool = FunctionTool(name="read_file", description="Read a file", func=read_file)
    async with scripted_openai(replies) as wire:
        client = assemble_openai_stack(ChatCompletionsClient, wire.client, model_id="vllm-model")
        await client.get_response([Message("user", ["read x"])], options={"tools": [tool]})

    assert len(wire.requests) == 2
    assert wire.requests[1]["messages"] == [
        {"role": "user", "content": "read x"},
        {
            "role": "assistant",
            "content": "I'll read it.",
            "tool_calls": [
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_bad",
            "content": ("Error: Invalid arguments for 'read_file': missing 'path'. Expected: {path: string}."),
        },
    ]


def test_openai_canonicalizes_messages_after_session_json_round_trip() -> None:
    """Reloaded session.json messages must produce the same canonical wire shape.

    Chrys persists kernel Messages via ``Message.to_dict()`` and restores
    them via ``Message.from_dict()``. The per-Message canonicalization scope
    only holds if a single restored Message still bundles text and the
    matching function_call as separate Contents on one Message — the same
    shape the Chat Completions parser emits for a live choice.  Round-tripping the
    poisoned pair through serialization here pins that invariant: a new
    user message replayed against the reloaded history must still produce
    the merged, ``content``-bearing assistant tool-call wire message that
    vLLM accepts.
    """

    poisoned_assistant = Message(
        "assistant",
        [
            Content.from_text("I'll read it."),
            Content.from_function_call(call_id="call_bad", name="read_file", arguments="{}"),
        ],
    )
    tool_error = Message(
        "tool",
        [Content.from_function_result(call_id="call_bad", result="Error: Argument parsing failed.")],
    )

    # Mimic chrys's persistence pipeline (serializers.py:serialize_message /
    # deserialize_message): Message -> dict -> Message.
    persisted = [
        Message("user", ["read x"]).to_dict(),
        poisoned_assistant.to_dict(),
        tool_error.to_dict(),
    ]
    reloaded = [Message.from_dict(d) for d in persisted]

    # Replay: append a new user message to the reloaded history and prep.
    history_for_replay = [*reloaded, Message("user", ["please retry"])]
    prepared = encode_messages(history_for_replay, variant=OPENAI)

    assert prepared == [
        {"role": "user", "content": "read x"},
        {
            "role": "assistant",
            "content": "I'll read it.",
            "tool_calls": [
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_bad", "content": "Error: Argument parsing failed."},
        {"role": "user", "content": "please retry"},
    ]


def test_openai_canonicalizes_malformed_arguments_after_session_json_round_trip() -> None:
    """Reloaded bad tool-call arguments must not make the next vLLM request 400."""

    poisoned_assistant = Message(
        "assistant",
        [
            Content.from_text("I'll search."),
            Content.from_function_call(call_id="call_bad", name="glob", arguments="{"),
        ],
    )
    tool_error = Message(
        "tool",
        [Content.from_function_result(call_id="call_bad", result="Error: Argument parsing failed.")],
    )

    persisted = [
        Message("user", ["find files"]).to_dict(),
        poisoned_assistant.to_dict(),
        tool_error.to_dict(),
    ]
    assert persisted[1] == {
        "type": "message",
        "role": "assistant",
        "contents": [
            {"type": "text", "text": "I'll search.", "additional_properties": {}},
            {
                "type": "function_call",
                "call_id": "call_bad",
                "name": "glob",
                "arguments": "{",
                "additional_properties": {},
            },
        ],
        "additional_properties": {},
    }
    reloaded = [Message.from_dict(d) for d in persisted]

    history_for_replay = [*reloaded, Message("user", ["please continue"])]
    prepared = encode_messages(history_for_replay, variant=OPENAI)

    assert prepared == [
        {"role": "user", "content": "find files"},
        {
            "role": "assistant",
            "content": "I'll search.",
            "tool_calls": [
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_bad", "content": "Error: Argument parsing failed."},
        {"role": "user", "content": "please continue"},
    ]


def test_openai_repairs_malformed_arguments_with_deepseek_client_cls() -> None:
    """DeepSeek's own message assembler repairs malformed arguments after its per-message prep."""
    from chrys.service.llm.chat_completions import DeepSeekChatCompletionsClient

    chat_client = make_chat_client(chat_client_cls=DeepSeekChatCompletionsClient)

    prepared = _encode(
        chat_client,
        Message(
            "assistant",
            [
                Content.from_text("I'll search."),
                Content.from_function_call(call_id="call_bad", name="glob", arguments="{"),
                Content.from_text_reasoning(
                    text=None,
                    protected_data='"reasoning"',
                    additional_properties={"openai_reasoning_format": "reasoning_content"},
                ),
            ],
        ),
    )

    assert prepared == [
        {
            "role": "assistant",
            "content": "I'll search.",
            "tool_calls": [
                {
                    "id": "call_bad",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
            "reasoning_content": "reasoning",
        }
    ]
