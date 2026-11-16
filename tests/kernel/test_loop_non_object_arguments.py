# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool-call arguments that are not a JSON object never run the tool.

``Content.parse_arguments`` wraps such a payload as ``{"raw": ...}``; a tool
whose schema has no required field or accepts extra keys would otherwise run
on arguments the model never gave it. The loop answers with an
``argument_parsing`` error before the middleware pipeline instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY, TOOL_FAILED_METADATA_KEY
from chrys.kernel import FunctionTool
from chrys.kernel.types import ChatResponse, ChatResponseUpdate, Content, Message
from tests.kernel._fakes import (
    _final_response,
    _ProbeFunction,
    _result_contents,
    _stack,
    _text_response,
    _text_update,
    _user,
)
from tests.support.oversized_int import oversized_integer_json

_PERMISSIVE_SCHEMAS = {
    "no_required": {
        "type": "object",
        "properties": {"state": {"type": "string"}, "limit": {"type": "integer"}},
    },
    "additional_properties": {
        "type": "object",
        "properties": {"state": {"type": "string"}},
        "additionalProperties": True,
    },
}

_NON_OBJECT_TEXT = {
    "word": "oops",
    "number": "5",
    "array": "[1]",
    "null": "null",
    "true": "true",
    "whitespace": "   ",
    "json_string": '"oops"',
    "json_empty_string": '""',
}

_OBJECT_MESSAGE = "arguments must be a valid JSON object"


class _ArgumentsDict(dict[str, Any]):
    """A mapping subclass, as a provider adapter might hand over parsed arguments."""


def _probe_tool(schema: dict[str, Any], received: list[dict[str, Any]]) -> FunctionTool:
    async def probe(**kwargs: Any) -> str:
        received.append(kwargs)
        return "ran"

    return FunctionTool(name="probe", description="Lenient tool.", func=probe, input_model=schema)


def _turns(call: Content, *, stream: bool) -> list[Any]:
    if stream:
        return [[ChatResponseUpdate(contents=[call], role="assistant")], [_text_update("done")]]
    return [ChatResponse(messages=[Message("assistant", [call])]), _text_response()]


def _calls(response: ChatResponse) -> list[Content]:
    return [item for message in response.messages for item in message.contents if item.type == "function_call"]


async def _run(arguments: Any, *, schema: dict[str, Any], stream: bool) -> tuple[ChatResponse, list[Any], int]:
    """Answer one call to a lenient tool; return the response, what the tool received and the pipeline entries."""
    received: list[dict[str, Any]] = []
    probe = _ProbeFunction()
    call = Content.from_function_call("c1", "probe", arguments=arguments)
    layer, _wire = _stack(_turns(call, stream=stream))
    response = await _final_response(
        layer, [_user()], stream=stream, options={"tools": [_probe_tool(schema, received)]}, middleware=[probe]
    )
    return response, received, len(probe.contexts)


def _assert_object_error(response: ChatResponse) -> None:
    (result,) = _result_contents(response)
    assert result.additional_properties[TOOL_FAILED_METADATA_KEY] is True
    assert result.additional_properties[TOOL_ERROR_KIND_METADATA_KEY] == "argument_parsing"
    assert str(result.result).startswith("Error: Invalid arguments for 'probe':")
    assert _OBJECT_MESSAGE in str(result.result)


@pytest.mark.parametrize("schema_name", sorted(_PERMISSIVE_SCHEMAS))
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize("text_name", sorted(_NON_OBJECT_TEXT))
async def test_non_object_argument_text_is_an_argument_error(schema_name: str, stream: bool, text_name: str) -> None:
    raw = _NON_OBJECT_TEXT[text_name]

    response, received, pipeline_entries = await _run(raw, schema=_PERMISSIVE_SCHEMAS[schema_name], stream=stream)

    assert received == []
    assert pipeline_entries == 0
    _assert_object_error(response)
    assert [call.arguments for call in _calls(response)] == [raw]


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_oversized_integer_literal_is_an_argument_error(stream: bool) -> None:
    """An integer past Python's conversion limit is unreadable, not a failed turn."""
    arguments = oversized_integer_json()

    response, received, pipeline_entries = await _run(
        arguments, schema=_PERMISSIVE_SCHEMAS["no_required"], stream=stream
    )

    assert received == []
    assert pipeline_entries == 0
    (result,) = _result_contents(response)
    assert result.additional_properties[TOOL_FAILED_METADATA_KEY] is True
    assert result.additional_properties[TOOL_ERROR_KIND_METADATA_KEY] == "argument_parsing"
    assert "integer-string conversion limit" in str(result.result)
    assert [call.arguments for call in _calls(response)] == [arguments]
    assert Content.from_function_call("c1", "probe", arguments=arguments).parse_arguments() == {"raw": arguments}


@pytest.mark.parametrize(
    "arguments",
    [[], [["state", "open"]], 0, False, 5, ("state", "open")],
    ids=["empty_list", "pair_list", "zero", "false", "int", "tuple"],
)
async def test_parsed_non_mapping_arguments_are_an_argument_error(arguments: Any) -> None:
    response, received, pipeline_entries = await _run(
        arguments, schema=_PERMISSIVE_SCHEMAS["no_required"], stream=False
    )

    assert received == []
    assert pipeline_entries == 0
    _assert_object_error(response)


_RUNNABLE = [
    ("none", None, {}),
    ("empty_string", "", {}),
    ("empty_object", "{}", {}),
    ("object_text", '{"state": "open"}', {"state": "open"}),
]
_RUNNABLE_PARSED = [
    ("dict", {"state": "open"}, {"state": "open"}),
    ("dict_subclass", _ArgumentsDict(state="open"), {"state": "open"}),
]


@pytest.mark.parametrize(
    ("stream", "arguments", "expected"),
    [pytest.param(False, arguments, expected, id=f"blocking-{name}") for name, arguments, expected in _RUNNABLE]
    + [pytest.param(True, arguments, expected, id=f"streaming-{name}") for name, arguments, expected in _RUNNABLE]
    + [
        pytest.param(False, arguments, expected, id=f"blocking-{name}")
        for name, arguments, expected in _RUNNABLE_PARSED
    ],
)
async def test_object_and_absent_arguments_still_run(stream: bool, arguments: Any, expected: dict[str, Any]) -> None:
    response, received, pipeline_entries = await _run(
        arguments, schema=_PERMISSIVE_SCHEMAS["no_required"], stream=stream
    )

    assert received == [expected]
    assert pipeline_entries == 1
    (result,) = _result_contents(response)
    assert result.exception is None
    assert str(result.result) == "ran"


async def test_a_raw_key_the_schema_declares_still_runs() -> None:
    """``{"raw": ...}`` is an ordinary object; only the parser's wrapper of a non-object is refused."""
    schema = {"type": "object", "properties": {"raw": {"type": "string"}}, "required": ["raw"]}

    response, received, pipeline_entries = await _run('{"raw": "value"}', schema=schema, stream=False)

    assert received == [{"raw": "value"}]
    assert pipeline_entries == 1
    assert str(_result_contents(response)[0].result) == "ran"
