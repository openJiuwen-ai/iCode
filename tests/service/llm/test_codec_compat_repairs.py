# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The wire codecs themselves carry their provider-compatibility repairs.

Every client stack wraps one of these loop-free codec classes, so a repair
pinned here holds for the main agent, judges, sub-agents and workflow nodes
alike.
"""

from __future__ import annotations

import re

import pytest
from openai.types.chat.chat_completion import ChatCompletion

from chrys.kernel import ChatClientException, Content, Message
from chrys.service.llm.anthropic_messages.history import encode_messages
from chrys.service.llm.chat_completions import ChatCompletionsClient, DeepSeekChatCompletionsClient
from chrys.service.llm.chat_completions import history as chat_history
from chrys.service.llm.chat_completions.client import OPENAI
from chrys.service.llm.chat_completions.decode import decode_completion


@pytest.mark.parametrize("cls", [ChatCompletionsClient, DeepSeekChatCompletionsClient])
def test_chat_completions_sends_text_and_a_repaired_tool_call_as_one_message(
    cls: type[ChatCompletionsClient],
) -> None:
    wire = chat_history.encode_messages(
        [
            Message(
                "assistant",
                [
                    Content.from_text("I'll read it."),
                    Content.from_function_call(call_id="call_1", name="read_file", arguments="{"),
                ],
            )
        ],
        variant=cls.VARIANT,
    )

    assert len(wire) == 1
    assert wire[0]["content"] == "I'll read it."
    assert wire[0]["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    ]


def test_chat_completions_rejects_a_completion_without_choices() -> None:
    gateway_error = ChatCompletion.model_construct(
        id="r",
        choices=None,
        created=0,
        model="gpt-test",
        object="chat.completion",
        error={"message": "gateway oops"},
    )

    with pytest.raises(ChatClientException, match="gateway oops"):
        decode_completion(gateway_error, {}, variant=OPENAI)


def test_anthropic_drops_unsigned_thinking_and_the_messages_it_empties() -> None:
    wire = encode_messages(
        [
            Message("user", ["before"]),
            Message(
                "assistant",
                [
                    Content.from_text_reasoning(text="unsigned"),
                    Content.from_text_reasoning(text="signed", protected_data="sig-1"),
                ],
            ),
            Message(
                "assistant",
                [Content.from_text_reasoning(text="unsigned only"), Content.from_text_reasoning(protected_data="")],
            ),
            Message(
                "user",
                ["after", Content.from_text_reasoning(text="unsigned"), Content.from_text_reasoning(protected_data="")],
            ),
        ]
    )

    assert wire == [
        {"role": "user", "content": [{"type": "text", "text": "before"}]},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "signed", "signature": "sig-1"}]},
        {"role": "user", "content": [{"type": "text", "text": "after"}]},
    ]


def test_anthropic_replays_thinking_streamed_without_text_with_its_own_signature() -> None:
    """Thinking streamed without its text assembles with no text: it is an empty block the signature after it signs."""
    wire = encode_messages(
        [
            Message("user", ["go"]),
            Message(
                "assistant",
                [
                    Content.from_text_reasoning(text="visible", protected_data=""),
                    Content.from_text_reasoning(protected_data="sig-1"),
                    Content.from_text_reasoning(protected_data=""),
                    Content.from_text_reasoning(protected_data="sig-2"),
                ],
            ),
        ]
    )

    assert wire[1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "visible", "signature": "sig-1"},
            {"type": "thinking", "thinking": "", "signature": "sig-2"},
        ],
    }


def test_anthropic_sends_another_providers_tool_call_ids_as_ids_it_accepts() -> None:
    """Kimi-style ids map by value, the same for the call and its result; native ids go as they are."""
    ids = ["functions.read_file:0", "functions.read_file:1", "toolu_01-native_ID"]
    history = [
        Message("user", ["read both"]),
        Message("assistant", [Content.from_function_call(call_id=i, name="read_file", arguments="{}") for i in ids]),
        Message("tool", [Content.from_function_result(call_id=i, result="ok") for i in ids]),
    ]

    wire = encode_messages(history)
    _, calls, results = wire

    sent = [block["id"] for block in calls["content"]]
    assert [block["tool_use_id"] for block in results["content"]] == sent
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", call_id) for call_id in sent)
    assert len(set(sent)) == 3
    assert sent[2] == ids[2]
    assert encode_messages(history) == wire
    assert [content.call_id for content in history[1].contents] == ids


def test_anthropic_drops_blank_text_the_api_rejects() -> None:
    wire = encode_messages(
        [
            Message("user", ["read"]),
            Message(
                "assistant",
                [Content.from_text(" \n"), Content.from_function_call(call_id="toolu_1", name="read", arguments="{}")],
            ),
            Message(
                "tool",
                [
                    Content.from_function_result(call_id="toolu_1", result=" \n"),
                    Content.from_function_result(
                        call_id="toolu_2", result=[Content.from_text(" "), Content.from_text("kept")]
                    ),
                ],
            ),
        ]
    )

    assert wire[1:] == [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}]},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "", "is_error": False},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_2",
                    "content": [{"type": "text", "text": "kept"}],
                    "is_error": False,
                },
            ],
        },
    ]
