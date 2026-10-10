# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for OpenAI-compatible Chat Completions stream assembly."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta as ChunkChoiceDelta

from chrys.kernel import ChatResponse, Content, FunctionTool, Message, ResponseStream
from chrys.kernel.exceptions import ChatClientInvalidResponseException
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.chat_completions.client import OPENAI
from chrys.service.llm.chat_completions.history import encode_messages
from chrys.service.llm.observer import intermediate_text_signal
from tests.service.llm._wire_stacks import assemble_openai_stack
from tests.support.openai_chat_wire import scripted_openai
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT

_MISSING = object()


def _tool_delta(
    *,
    index: object = 0,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> ChoiceDeltaToolCall:
    values: dict[str, Any] = {
        "id": call_id,
        "type": "function",
        "function": ChoiceDeltaToolCallFunction.model_construct(name=name, arguments=arguments),
    }
    if index is not _MISSING:
        values["index"] = index
    return ChoiceDeltaToolCall.model_construct(**values)


def _chunk(
    delta: ChunkChoiceDelta | None,
    *,
    finish_reason: str | None = None,
    chunk_id: str = "chunk-1",
    choice_index: int = 0,
) -> ChatCompletionChunk:
    choice = ChunkChoice.model_construct(
        index=choice_index,
        delta=delta,
        finish_reason=finish_reason,
    )
    return ChatCompletionChunk.model_construct(
        id=chunk_id,
        object="chat.completion.chunk",
        created=1_717_171_717,
        model="glm-5.2",
        choices=[choice],
        usage=None,
    )


def _tool_chunk(
    *tool_calls: ChoiceDeltaToolCall,
    reasoning_content: str | None = None,
    reasoning: str | None = None,
    chunk_id: str = "chunk-1",
) -> ChatCompletionChunk:
    return _chunk(
        ChunkChoiceDelta.model_construct(
            role="assistant",
            tool_calls=list(tool_calls),
            reasoning_content=reasoning_content,
            reasoning=reasoning,
        ),
        chunk_id=chunk_id,
    )


async def _raw_stream_response(
    chunks: Sequence[ChatCompletionChunk],
) -> tuple[list[Any], ChatResponse]:
    async with scripted_openai([chunks]) as wire:
        client = ChatCompletionsClient(model="glm-5.2", sdk_client=wire.client)
        stream = client._inner_get_response(
            messages=[Message("user", ["test"])],
            options={},
            stream=True,
        )
        assert isinstance(stream, ResponseStream)
        updates = [update async for update in stream]
        return updates, await stream.get_final_response()


async def _updates_until_failure(chunks: Sequence[ChatCompletionChunk], *, match: str) -> list[Any]:
    emitted: list[Any] = []
    async with scripted_openai([chunks]) as wire:
        client = ChatCompletionsClient(model="glm-5.2", sdk_client=wire.client)
        stream = client._inner_get_response(
            messages=[Message("user", ["test"])],
            options={},
            stream=True,
        )
        with pytest.raises(ChatClientInvalidResponseException, match=match):
            async for update in stream:
                emitted.append(update)
    return emitted


def _function_calls(response: ChatResponse) -> list[Content]:
    return [content for message in response.messages for content in message.contents if content.type == "function_call"]


@pytest.mark.parametrize("reasoning_field", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize(("first_text", "last_text"), [("基线\n", "核对完毕"), ("回", "退\n把代码核对完毕")])
async def test_mixed_reasoning_tail_and_text_matches_sequential_chunks(
    reasoning_field: str, first_text: str, last_text: str
) -> None:
    prefix = _chunk(ChunkChoiceDelta.model_construct(role="assistant", **{reasoning_field: "read lines 810"}))
    tail = {reasoning_field: "-830."}
    suffix = [
        _chunk(ChunkChoiceDelta.model_construct(content=last_text)),
        _tool_chunk(_tool_delta(call_id="call_1", name="read_file", arguments="{}")),
        _chunk(ChunkChoiceDelta.model_construct(), finish_reason="tool_calls"),
    ]

    _, mixed = await _raw_stream_response(
        [prefix, _chunk(ChunkChoiceDelta.model_construct(content=first_text, **tail)), *suffix]
    )
    _, sequential = await _raw_stream_response(
        [
            prefix,
            _chunk(ChunkChoiceDelta.model_construct(**tail)),
            _chunk(ChunkChoiceDelta.model_construct(content=first_text)),
            *suffix,
        ]
    )

    for response in (mixed, sequential):
        restored = ChatResponse.from_dict(response.to_dict())
        contents = restored.messages[0].contents
        assert [(content.type, content.text) for content in contents[:2]] == [
            ("text_reasoning", "read lines 810-830."),
            ("text", first_text + last_text),
        ]
        assert len(contents) == 3
        assert contents[0].additional_properties["openai_reasoning_format"] == reasoning_field
        assert contents[2].type == "function_call"
        assert contents[2].call_id == "call_1"
        assert intermediate_text_signal(restored) == first_text + last_text


@pytest.mark.asyncio
async def test_glm_stream_through_the_client_stack_assembles_tool_and_preserves_markdown() -> None:
    """Exercise the production wire client, tool loop, replay, and final text stream."""
    argument_fragments = [
        "{",
        '"command": ',
        '"ls"',
        ", ",
        '"reason": ',
        '"List files',
        " in the",
        " current directory",
        '"}',
    ]
    tool_chunks = [
        _tool_chunk(
            _tool_delta(
                index=0,
                call_id="call-glm",
                name="zsh",
                arguments=argument_fragments[0],
            ),
            reasoning_content="",
        ),
        *[
            _tool_chunk(
                _tool_delta(index=0, arguments=fragment),
                reasoning_content="",
            )
            for fragment in argument_fragments[1:]
        ],
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]
    markdown_fragments = ["Contents:\n\n", "```\n", "AGENTS.md\n", "src\n", "```"]
    final_chunks = [
        _chunk(
            ChunkChoiceDelta.model_construct(
                role="assistant",
                content=fragment,
                reasoning_content="",
            ),
            chunk_id="chunk-2",
        )
        for fragment in markdown_fragments
    ]
    final_chunks.append(
        _chunk(
            ChunkChoiceDelta.model_construct(role="assistant"),
            finish_reason="stop",
            chunk_id="chunk-2",
        )
    )
    executions: list[tuple[str, str]] = []

    def zsh(command: str, reason: str) -> str:
        executions.append((command, reason))
        return "AGENTS.md\nsrc"

    async with scripted_openai([tool_chunks, final_chunks]) as wire:
        client = assemble_openai_stack(ChatCompletionsClient, wire.client, model_id="glm-5.2")
        tool = FunctionTool(name="zsh", description="Run a command", func=zsh)
        stream = client.get_response(
            [Message("user", ["list files"])],
            stream=True,
            options={"tools": [tool], "extra_body": {"tool_stream": True}},
        )
        updates = [update async for update in stream]
        response = await stream.get_final_response()

    assert executions == [("ls", "List files in the current directory")]
    assert response.messages[-1].text == "".join(markdown_fragments)
    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        (
            "call-glm",
            "zsh",
            {"command": "ls", "reason": "List files in the current directory"},
        )
    ]
    assert len(wire.requests) == 2
    replayed_call_message = next(message for message in wire.requests[1]["messages"] if message.get("tool_calls"))
    assert replayed_call_message["reasoning_content"] == ""
    assert replayed_call_message["tool_calls"] == [
        {
            "id": "call-glm",
            "type": "function",
            "function": {
                "name": "zsh",
                "arguments": '{"command": "ls", "reason": "List files in the current directory"}',
            },
        }
    ]
    final_message = response.messages[-1]
    assert [content.type for content in final_message.contents] == ["text_reasoning", "text"]
    assert final_message.contents[0].text == ""
    assert final_message.contents[1].text == "".join(markdown_fragments)
    call_update_index = next(
        index
        for index, update in enumerate(updates)
        if any(content.type == "function_call" for content in update.contents)
    )
    tool_finish_index = next(index for index, update in enumerate(updates) if update.finish_reason == "tool_calls")
    assert call_update_index < tool_finish_index


@pytest.mark.asyncio
async def test_nonempty_call_id_wins_when_gateway_reuses_tool_index() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", name="alpha", arguments='{"value":1}'),
            _tool_delta(index=0, call_id="call-b", name="beta", arguments='{"value":2}'),
        ),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "alpha", {"value": 1}),
        ("call-b", "beta", {"value": 2}),
    ]


@pytest.mark.asyncio
async def test_distinct_whole_calls_to_one_function_reusing_tool_index_stay_apart() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="read_file", arguments='{"path":"a"}')),
        _tool_chunk(_tool_delta(index=0, call_id="call-b", name="read_file", arguments='{"path":"b"}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "read_file", {"path": "a"}),
        ("call-b", "read_file", {"path": "b"}),
    ]


@pytest.mark.asyncio
async def test_call_id_changing_mid_call_continues_the_call_under_its_first_id() -> None:
    # A Kimi-style stream: every fragment of one call under a new id, the name only on the first.
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="functions.read:0", name="read", arguments="")),
        _tool_chunk(_tool_delta(index=0, call_id="chatcmpl-tool-a", arguments='{"path":"README')),
        _chunk(
            ChunkChoiceDelta.model_construct(
                role="assistant", tool_calls=[_tool_delta(index=0, call_id="chatcmpl-tool-b", arguments='.md"}')]
            ),
            finish_reason="tool_calls",
        ),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("functions.read:0", "read", {"path": "README.md"})
    ]


@pytest.mark.asyncio
async def test_name_sent_under_a_changed_call_id_after_a_nameless_first_fragment_names_the_call() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", arguments="")),
        _tool_chunk(_tool_delta(index=0, call_id="call-b", name="read_file", arguments='{"path":')),
        _tool_chunk(_tool_delta(index=0, call_id="call-c", arguments='"a"}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "read_file", {"path": "a"})
    ]


@pytest.mark.asyncio
async def test_named_whole_call_reusing_the_index_of_a_nameless_whole_call_stays_apart() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", arguments='{"path":"a"}'),
            _tool_delta(index=0, call_id="call-b", name="read_file", arguments='{"path":"b"}'),
        ),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "", {"path": "a"}),
        ("call-b", "read_file", {"path": "b"}),
    ]


@pytest.mark.asyncio
async def test_trailing_empty_fragment_under_a_new_call_id_adds_no_call() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="read_file", arguments='{"path":"x"}')),
        _tool_chunk(_tool_delta(index=0, call_id="call-b", arguments="")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "read_file", {"path": "x"})
    ]


@pytest.mark.asyncio
async def test_parallel_calls_with_changing_ids_and_indexless_calls_assemble_independently() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="tc_read_initial", name="read", arguments='{"path":"README'),
            _tool_delta(index=1, call_id="tc_grep_initial", name="grep", arguments='{"pattern":"TODO'),
            _tool_delta(index=_MISSING, call_id="tc_list_no_index", name="list", arguments='{"path":"packages'),
            _tool_delta(index=_MISSING, call_id="tc_write_no_index", name="write", arguments='{"path":"out'),
        ),
        _tool_chunk(
            _tool_delta(index=1, call_id="tc_grep_changed", arguments='","path":"src'),
            _tool_delta(index=_MISSING, call_id="tc_write_no_index", arguments='.txt","content":"ok"}'),
            _tool_delta(index=_MISSING, call_id="tc_list_no_index", arguments='/ai"}'),
        ),
        _chunk(
            ChunkChoiceDelta.model_construct(
                role="assistant",
                tool_calls=[
                    _tool_delta(index=0, call_id="tc_read_changed", arguments='.md"}'),
                    _tool_delta(index=1, arguments='"}'),
                ],
            ),
            finish_reason="tool_calls",
        ),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("tc_read_initial", "read", {"path": "README.md"}),
        ("tc_grep_initial", "grep", {"pattern": "TODO", "path": "src"}),
        ("tc_list_no_index", "list", {"path": "packages/ai"}),
        ("tc_write_no_index", "write", {"path": "out.txt", "content": "ok"}),
    ]


@pytest.mark.asyncio
async def test_changed_call_id_finds_its_call_again_without_an_index() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", name="alpha", arguments="{"),
            _tool_delta(index=1, call_id="call-b", name="beta", arguments="{"),
        ),
        _tool_chunk(_tool_delta(index=0, call_id="call-a2", arguments='"x":')),
        _tool_chunk(_tool_delta(index=_MISSING, call_id="call-a2", arguments="1}")),
        _tool_chunk(_tool_delta(index=1, arguments='"y":2}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "alpha", {"x": 1}),
        ("call-b", "beta", {"y": 2}),
    ]


@pytest.mark.asyncio
async def test_known_call_id_wins_over_the_index_of_another_call() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", name="alpha", arguments='{"x":1}'),
            _tool_delta(index=1, call_id="call-b", name="beta", arguments="{"),
        ),
        _tool_chunk(_tool_delta(index=0, call_id="call-b", arguments='"y":2}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "alpha", {"x": 1}),
        ("call-b", "beta", {"y": 2}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opening_index", "late_id_index"),
    [(0, 0), (_MISSING, _MISSING)],
    ids=["indexed", "indexless"],
)
async def test_late_call_id_binds_to_existing_sole_call(
    opening_index: object,
    late_id_index: object,
) -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=opening_index, name="alpha", arguments="{")),
        _tool_chunk(_tool_delta(index=late_id_index, call_id="call-a", arguments='"value":1}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "alpha", {"value": 1})
    ]


@pytest.mark.asyncio
async def test_idless_explicit_new_indices_remain_distinct_parallel_calls() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, name="read_file", arguments='{"path":"a"}')),
        _tool_chunk(_tool_delta(index=1, name="read_file", arguments='{"path":"b"}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    calls = _function_calls(response)
    assert [(call.name, call.parse_arguments()) for call in calls] == [
        ("read_file", {"path": "a"}),
        ("read_file", {"path": "b"}),
    ]
    assert all(call.call_id.startswith("call_chrys_0_") for call in calls)
    assert len({call.call_id for call in calls}) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("names"), [("read_file", "read_file"), ("read_file", "write_file")])
async def test_literal_null_call_ids_remain_distinct_parallel_calls(names: tuple[str, str]) -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="null", name=names[0], arguments='{"path":"a"}'),
            _tool_delta(index=1, call_id="null", name=names[1], arguments='{"path":"b"}'),
        ),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    calls = _function_calls(response)
    assert [(call.name, call.parse_arguments()) for call in calls] == [
        (names[0], {"path": "a"}),
        (names[1], {"path": "b"}),
    ]
    assert all(call.call_id.startswith("call_chrys_0_") for call in calls)
    assert len({call.call_id for call in calls}) == 2


@pytest.mark.asyncio
async def test_single_literal_null_call_id_is_treated_as_absent() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="null", name="read_file", arguments='{"path":"a"}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    (call,) = _function_calls(response)
    assert call.call_id != "null"


@pytest.mark.asyncio
async def test_function_named_null_is_still_accepted() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="null", arguments="{}")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    (call,) = _function_calls(response)
    assert (call.call_id, call.name, call.parse_arguments()) == ("call-a", "null", {})


@pytest.mark.asyncio
async def test_standard_parallel_tool_call_fragments_interleave_by_index() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="alpha", arguments="{")),
        _tool_chunk(_tool_delta(index=1, call_id="call-b", name="beta", arguments="{")),
        _tool_chunk(_tool_delta(index=0, arguments='"x":1}')),
        _tool_chunk(_tool_delta(index=1, arguments='"y":2}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "alpha", {"x": 1}),
        ("call-b", "beta", {"y": 2}),
    ]


@pytest.mark.asyncio
async def test_conflicting_complete_function_names_fail_before_emission() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, name="read_file", arguments='{"path":"a"}')),
        _tool_chunk(_tool_delta(index=0, name="write_file", arguments='{"path":"b"}')),
    ]
    emitted = await _updates_until_failure(chunks, match="Conflicting streamed tool-call names")

    assert not any(content.type == "function_call" for update in emitted for content in update.contents)


@pytest.mark.asyncio
async def test_repeated_complete_function_name_is_ignored() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="read_file", arguments="{")),
        _tool_chunk(_tool_delta(index=0, name="read_file", arguments='"path":"a"}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    (call,) = _function_calls(response)
    assert (call.call_id, call.name, call.parse_arguments()) == ("call-a", "read_file", {"path": "a"})


@pytest.mark.asyncio
async def test_ambiguous_idless_fragment_fails_before_function_call_emission() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", name="alpha", arguments="{"),
            _tool_delta(index=1, call_id="call-b", name="beta", arguments="{"),
        ),
        _tool_chunk(_tool_delta(index=_MISSING, arguments='"value":1}')),
    ]
    emitted = await _updates_until_failure(chunks, match="Ambiguous streamed tool-call fragment")

    assert not any(content.type == "function_call" for update in emitted for content in update.contents)


@pytest.mark.asyncio
async def test_null_terminal_delta_drains_complete_call_before_finish() -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="alpha", arguments="{")),
        _tool_chunk(_tool_delta(index=0, arguments='"value":1')),
        _tool_chunk(_tool_delta(index=0, arguments="}")),
        _chunk(None, finish_reason="tool_calls"),
    ]

    updates, response = await _raw_stream_response(chunks)

    call_update_index = next(
        index
        for index, update in enumerate(updates)
        if any(content.type == "function_call" for content in update.contents)
    )
    finish_update_index = next(index for index, update in enumerate(updates) if update.finish_reason == "tool_calls")
    assert call_update_index < finish_update_index
    assert _function_calls(response)[0].parse_arguments() == {"value": 1}


@pytest.mark.asyncio
async def test_tool_delta_after_finish_is_ignored_without_duplicate_emission(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="alpha", arguments='{"value":1}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
        _tool_chunk(_tool_delta(index=0, call_id="call-a", name="alpha", arguments='{"value":1}')),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "alpha", {"value": 1})
    ]
    assert "Ignoring streamed tool-call fragment received after the terminal update" in caplog.text


@pytest.mark.asyncio
async def test_late_tool_delta_after_null_terminal_first_is_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chunks = [
        _chunk(None, finish_reason="stop"),
        _tool_chunk(_tool_delta(index=0, call_id="call-late", name="alpha", arguments='{"value":1}')),
    ]

    _, response = await _raw_stream_response(chunks)

    assert response.finish_reason == "stop"
    assert _function_calls(response) == []
    assert "Ignoring streamed tool-call fragment received after the terminal update" in caplog.text


@pytest.mark.asyncio
async def test_nonempty_reasoning_suppresses_later_empty_presence_marker() -> None:
    chunks = [
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", reasoning_content="think ")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", reasoning_content="hard")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", content="answer", reasoning_content="")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="stop"),
    ]

    _, response = await _raw_stream_response(chunks)

    reasoning = [content for content in response.messages[0].contents if content.type == "text_reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0].text == "think hard"
    assert response.raw_text == "answer"


@pytest.mark.asyncio
async def test_vllm_reasoning_stream_suppresses_later_empty_and_replays_exact_field() -> None:
    chunks = [
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", reasoning="think ")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", reasoning="hard")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", content="answer", reasoning="")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="stop"),
    ]

    _, response = await _raw_stream_response(chunks)

    reasoning = [content for content in response.messages[0].contents if content.type == "text_reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0].text == "think hard"
    assert reasoning[0].additional_properties["openai_reasoning_format"] == "reasoning"
    assert response.raw_text == "answer"

    replayed = encode_messages(response.messages, variant=OPENAI)
    assert replayed == [{"role": "assistant", "content": "answer", "reasoning": "think hard"}]


@pytest.mark.asyncio
async def test_vllm_qwen36_reported_sse_payload_assembles_reasoning() -> None:
    """Assemble a verbatim Qwen3.6 SSE stream captured from vLLM 0.19.2.

    Real wire shape, not a constructed one: an empty ``content: ""`` role
    chunk first, bare ``delta.reasoning`` fragments, a field-less
    ``delta: {}`` stop chunk, then a ``choices: []`` usage-only chunk. In
    this capture the server had thinking disabled yet still routed the
    answer through ``reasoning`` (an upstream parser fault); Chrys must
    faithfully keep what the wire said rather than second-guess it, so
    the assembled reasoning is ``"12"`` and no text content exists.
    """
    payloads = [
        {
            "id": "chatcmpl-890423d350192a92",
            "object": "chat.completion.chunk",
            "created": 1_777_044_630,
            "model": "qwen3.6-35b-nvfp4",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-890423d350192a92",
            "object": "chat.completion.chunk",
            "created": 1_777_044_630,
            "model": "qwen3.6-35b-nvfp4",
            "choices": [{"index": 0, "delta": {"reasoning": "1"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-890423d350192a92",
            "object": "chat.completion.chunk",
            "created": 1_777_044_630,
            "model": "qwen3.6-35b-nvfp4",
            "choices": [{"index": 0, "delta": {"reasoning": "2"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-890423d350192a92",
            "object": "chat.completion.chunk",
            "created": 1_777_044_630,
            "model": "qwen3.6-35b-nvfp4",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl-890423d350192a92",
            "object": "chat.completion.chunk",
            "created": 1_777_044_630,
            "model": "qwen3.6-35b-nvfp4",
            "choices": [],
            "usage": {"prompt_tokens": 35, "total_tokens": 38, "completion_tokens": 3},
        },
    ]
    chunks = [ChatCompletionChunk.model_validate(payload) for payload in payloads]

    _, response = await _raw_stream_response(chunks)

    reasoning = [content for content in response.messages[0].contents if content.type == "text_reasoning"]
    assert len(reasoning) == 1
    assert reasoning[0].text == "12"
    assert reasoning[0].additional_properties["openai_reasoning_format"] == "reasoning"
    assert response.raw_text == ""
    assert response.usage_details is not None
    assert response.usage_details["total_token_count"] == 38


@pytest.mark.asyncio
async def test_vllm_empty_reasoning_presence_marker_precedes_tool_call_and_replays() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-qwen", name="lookup", arguments="{}"),
            reasoning="",
        ),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [content.type for content in response.messages[0].contents] == ["text_reasoning", "function_call"]
    reasoning = response.messages[0].contents[0]
    assert reasoning.text == ""
    assert reasoning.additional_properties["openai_reasoning_format"] == "reasoning"

    replayed = encode_messages(response.messages, variant=OPENAI)
    assert replayed[0]["reasoning"] == ""
    assert "reasoning_content" not in replayed[0]
    assert replayed[0]["tool_calls"][0]["id"] == "call-qwen"


@pytest.mark.asyncio
async def test_nonempty_reasoning_before_glm_tool_fragments_replays_real_reasoning() -> None:
    chunks = [
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", reasoning_content="think ")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", reasoning_content="hard")),
        _tool_chunk(
            _tool_delta(index=0, call_id="call-zsh", name="zsh", arguments="{"),
            reasoning_content="",
        ),
        _tool_chunk(
            _tool_delta(index=0, arguments='"command":"ls"}'),
            reasoning_content="",
        ),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]
    _, response = await _raw_stream_response(chunks)

    assert [content.type for content in response.messages[0].contents] == ["text_reasoning", "function_call"]
    assert response.messages[0].contents[0].text == "think hard"
    replayed = encode_messages(response.messages, variant=OPENAI)
    assert replayed == [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "think hard",
            "tool_calls": [
                {
                    "id": "call-zsh",
                    "type": "function",
                    "function": {"name": "zsh", "arguments": '{"command":"ls"}'},
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_parallel_glm_tool_fragments_with_empty_reasoning_do_not_cross_contaminate() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", name="alpha", arguments="{"),
            reasoning_content="",
        ),
        _tool_chunk(
            _tool_delta(index=1, call_id="call-b", name="beta", arguments="{"),
            reasoning_content="",
        ),
        _tool_chunk(_tool_delta(index=0, arguments='"x":1}'), reasoning_content=""),
        _tool_chunk(_tool_delta(index=1, arguments='"y":2}'), reasoning_content=""),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [content.type for content in response.messages[0].contents] == [
        "text_reasoning",
        "function_call",
        "function_call",
    ]
    assert response.messages[0].contents[0].text == ""
    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "alpha", {"x": 1}),
        ("call-b", "beta", {"y": 2}),
    ]


@pytest.mark.asyncio
async def test_plain_openai_text_stream_adds_no_reasoning_content() -> None:
    chunks = [
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", content="hello ")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant", content="world")),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="stop"),
    ]

    updates, response = await _raw_stream_response(chunks)

    assert [update.text for update in updates] == ["hello ", "world", ""]
    assert [content.type for content in response.messages[0].contents] == ["text"]
    assert response.raw_text == "hello world"


@pytest.mark.asyncio
async def test_idless_single_tool_call_preserves_legacy_behavior(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG", logger="chrys.service.llm.chat_completions.stream")
    chunks = [
        _tool_chunk(_tool_delta(index=0, name="alpha", arguments='{"value":1}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    (call,) = _function_calls(response)
    assert (call.call_id, call.name, call.parse_arguments()) == ("", "alpha", {"value": 1})
    assert "preserving legacy id-less behavior" in caplog.text


@pytest.mark.asyncio
async def test_length_truncated_call_remains_final_content() -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", name="alpha", arguments="{"),
            reasoning_content="",
        ),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="length"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert response.finish_reason == "length"
    assert [content.type for content in response.messages[0].contents] == ["text_reasoning", "function_call"]
    assert response.messages[0].contents[-1].arguments == "{"


@pytest.mark.asyncio
async def test_nameless_call_is_handed_over_for_response_validation(caplog: pytest.LogCaptureFixture) -> None:
    # Response validation rejects it and sends the request again.
    chunks = [
        _tool_chunk(_tool_delta(index=0, call_id="call-a", arguments='{"path":"x"}')),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="tool_calls"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response)] == [
        ("call-a", "", {"path": "x"})
    ]
    assert "Streamed tool call ended without a function name for choice 0, index 0" in caplog.text


@pytest.mark.asyncio
async def test_length_truncated_nameless_call_is_discarded(caplog: pytest.LogCaptureFixture) -> None:
    chunks = [
        _tool_chunk(
            _tool_delta(index=0, call_id="call-a", arguments="{"),
            reasoning_content="",
        ),
        _chunk(ChunkChoiceDelta.model_construct(role="assistant"), finish_reason="length"),
    ]

    _, response = await _raw_stream_response(chunks)

    assert response.finish_reason == "length"
    assert _function_calls(response) == []
    assert "Discarding truncated streamed tool call without a name" in caplog.text


@pytest.mark.asyncio
async def test_stream_assembly_state_is_isolated_between_concurrent_requests() -> None:
    def reply(body: dict[str, Any]) -> list[ChatCompletionChunk]:
        suffix = "a" if body["messages"][0]["content"] == "request-a" else "b"
        return [
            _tool_chunk(
                _tool_delta(index=0, call_id=f"call-{suffix}", name=f"tool_{suffix}", arguments="{"),
                chunk_id=f"chunk-{suffix}",
            ),
            _tool_chunk(
                _tool_delta(index=0, arguments=f'"value":"{suffix}"}}'),
                chunk_id=f"chunk-{suffix}",
            ),
            _chunk(
                ChunkChoiceDelta.model_construct(role="assistant"),
                finish_reason="tool_calls",
                chunk_id=f"chunk-{suffix}",
            ),
        ]

    # Lockstep events: each stream's next event waits for the other's.
    async with scripted_openai(reply, pace=asyncio.Barrier(2).wait) as wire:
        client = ChatCompletionsClient(model="glm-5.2", sdk_client=wire.client)

        async def _run(prompt: str) -> ChatResponse:
            stream = client._inner_get_response(
                messages=[Message("user", [prompt])],
                options={},
                stream=True,
            )
            assert isinstance(stream, ResponseStream)
            return await stream.get_final_response()

        # A stream that stops reading early would leave the other at the barrier.
        async with asyncio.timeout(DEFAULT_WAIT_TIMEOUT):
            response_a, response_b = await asyncio.gather(_run("request-a"), _run("request-b"))

    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response_a)] == [
        ("call-a", "tool_a", {"value": "a"})
    ]
    assert [(call.call_id, call.name, call.parse_arguments()) for call in _function_calls(response_b)] == [
        ("call-b", "tool_b", {"value": "b"})
    ]
