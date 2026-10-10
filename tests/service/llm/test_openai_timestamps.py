# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for OpenAI-compatible timestamp normalization, and answers whose timestamp is missing or unusable."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, ChoiceDelta
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from chrys.kernel import Message, ResponseStream
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.openai_timestamps import (
    normalize_openai_created_payload,
    normalize_openai_created_timestamp,
    openai_created_at_iso,
)
from tests.service.llm._responses_wire import Script, respond, snapshot
from tests.support.openai_chat_wire import ChatReply, scripted_openai, wire_payload
from tests.support.wire_cases._kit import json_reply


def test_normalize_openai_created_timestamp_leaves_standard_seconds_unchanged() -> None:
    created = 1_717_171_717

    assert normalize_openai_created_timestamp(created) == created


def test_openai_created_at_iso_converts_thirteen_digit_milliseconds() -> None:
    created_ms = 1_717_171_717_123

    assert openai_created_at_iso(created_ms) == datetime.fromtimestamp(
        1_717_171_717.123,
        tz=UTC,
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_normalize_openai_created_payload_copies_openai_sdk_models() -> None:
    created_ms = 1_717_171_717_123
    chunk = ChatCompletionChunk(
        id="chunk-1",
        object="chat.completion.chunk",
        created=created_ms,
        model="gpt-test",
        choices=[],
    )

    normalized = normalize_openai_created_payload(chunk)

    assert normalized is not chunk
    assert normalized.created == 1_717_171_717.123
    assert chunk.created == created_ms


def test_normalize_openai_created_payload_leaves_standard_seconds_object_unchanged() -> None:
    payload = SimpleNamespace(created=1_717_171_717)

    assert normalize_openai_created_payload(payload) is payload


@pytest.mark.parametrize(
    "created",
    [None, True, False, "1717171717", math.nan, math.inf, -math.inf, 10**20, -(10**20), 10**400],
    ids=["none", "true", "false", "string", "nan", "inf", "-inf", "huge", "-huge", "beyond-float"],
)
def test_openai_created_at_iso_is_none_for_values_that_are_no_unix_time(created: object) -> None:
    assert openai_created_at_iso(created) is None


def test_openai_created_at_iso_formats_float_seconds() -> None:
    assert openai_created_at_iso(1_717_171_717.5) == "2024-05-31T16:08:37.500000Z"


def test_openai_created_at_iso_converts_thirteen_digit_milliseconds_held_as_a_float() -> None:
    assert openai_created_at_iso(1_717_171_717_123.0) == "2024-05-31T16:08:37.123000Z"


def _chat_completion(**created: Any) -> ChatCompletion:
    message = ChatCompletionMessage.model_construct(role="assistant", content="hi")
    choice = Choice.model_construct(index=0, message=message, finish_reason="stop")
    return ChatCompletion.model_construct(
        id="completion-1", object="chat.completion", model="test", choices=[choice], usage=None, **created
    )


def _chat_chunk(**created: Any) -> ChatCompletionChunk:
    delta = ChoiceDelta.model_construct(role="assistant", content="hi")
    choice = ChunkChoice.model_construct(index=0, delta=delta, finish_reason="stop")
    return ChatCompletionChunk.model_construct(
        id="chunk-1", object="chat.completion.chunk", model="test", choices=[choice], usage=None, **created
    )


# Absent, null, and out of range for the platform's time_t.  (The Responses SDK
# model turns a boolean into 0.0 or 1.0 before Chrys reads it.)
_ODD_CREATED: list[dict[str, Any]] = [{}, {"created": None}, {"created": 10**20}]
_ODD_CREATED_IDS = ["absent", "null", "out-of-range"]


@pytest.mark.parametrize(
    "created",
    [*_ODD_CREATED, {"created": 10**400}],
    ids=[*_ODD_CREATED_IDS, "beyond-float"],
)
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_chat_completions_answer_without_a_usable_created_still_decodes(
    created: dict[str, Any], stream: bool
) -> None:
    reply: ChatReply = [_chat_chunk(**created)] if stream else _chat_completion(**created)
    async with scripted_openai([reply]) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        result = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=stream)
        if isinstance(result, ResponseStream):
            updates = [update async for update in result]
            response = await result.get_final_response()
        else:
            updates = []
            response = await result

    assert response.text == "hi"
    assert response.finish_reason == "stop"
    assert response.created_at is None
    assert all(update.created_at is None for update in updates)


async def test_chat_completions_answer_with_a_boolean_created_still_decodes() -> None:
    # Raw event data: the SDK model keeps the boolean, which a model dump writes as 1.
    event = {**wire_payload(_chat_chunk()), "created": True}
    async with scripted_openai([[json.dumps(event)]]) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        result = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        assert isinstance(result, ResponseStream)
        response = await result.get_final_response()

    assert response.text == "hi"
    assert response.created_at is None


def _with_created_at(response: dict[str, Any], created: dict[str, Any]) -> dict[str, Any]:
    """*response* with ``created_at`` left out, or set to the value *created* gives."""
    response = {key: value for key, value in response.items() if key != "created_at"}
    if "created" in created:
        response["created_at"] = created["created"]
    return response


@pytest.mark.parametrize("created", _ODD_CREATED, ids=_ODD_CREATED_IDS)
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_responses_answer_without_a_usable_created_at_still_decodes(
    created: dict[str, Any], stream: bool
) -> None:
    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "hi", "annotations": []}],
    }
    if stream:
        script = Script().started().text(0, "msg_1", "hi").finished(item)
        script.events = [
            (name, {**data, "response": _with_created_at(data["response"], created)} if "response" in data else data)
            for name, data in script.events
        ]
        reply = script.reply()
    else:
        reply = json_reply(_with_created_at(snapshot(item), created))

    response, updates = await respond(reply, stream=stream)

    assert response.text == "hi"
    assert response.created_at is None
    assert all(update.created_at is None for update in updates)


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_responses_answer_converts_a_thirteen_digit_millisecond_created_at(stream: bool) -> None:
    item = {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "hi", "annotations": []}],
    }
    created = {"created": 1_717_171_717_123}
    if stream:
        script = Script().started().text(0, "msg_1", "hi").finished(item)
        script.events = [
            (name, {**data, "response": _with_created_at(data["response"], created)} if "response" in data else data)
            for name, data in script.events
        ]
        reply = script.reply()
    else:
        reply = json_reply(_with_created_at(snapshot(item), created))

    response, _ = await respond(reply, stream=stream)

    assert response.created_at == "2024-05-31T16:08:37.123000Z"
