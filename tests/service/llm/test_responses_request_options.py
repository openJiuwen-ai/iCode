# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Responses request-options pins: native instructions, response-format name
sanitizing at the ``text`` choke point, and cache-write usage extraction.

``instructions`` rides ``run_options`` as the first-class Responses request
param on every create — never as a prepended system message — so instruction
changes stay effective on continuation turns. Every name-producing
response-format form lands in ``text.format`` before the wire, where the
schema-name sanitizer (``[A-Za-z0-9_-]``, ≤64, fallback ``"response"``)
runs once.
"""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
from typing import Any

import pytest
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails, ResponseUsage
from pydantic import BaseModel

from chrys.kernel import Message
from chrys.service.llm.openai_responses import ResponsesApiClient
from chrys.service.llm.openai_responses.client import OPENAI_RESPONSES
from chrys.service.llm.openai_responses.decode import decode_usage
from tests.service.llm._non_strict_schemas import non_strict_schema_cases
from tests.service.llm._responses_wire import Script, blocking, respond


class _FakeAsyncOpenAI:
    base_url = "https://api.test"


def _client() -> ResponsesApiClient:
    return ResponsesApiClient(model="gpt-test", sdk_client=_FakeAsyncOpenAI())


def _user(text: str = "hi") -> Message:
    return Message(role="user", contents=[text])


def _prepare(options: dict[str, Any]) -> dict[str, Any]:
    return _client()._build_request([_user()], options)


def _input_roles(run_options: dict[str, Any]) -> list[Any]:
    return [item.get("role") for item in run_options["input"] if isinstance(item, dict)]


# ───────────────────────── native instructions ─────────────────────────


@pytest.mark.parametrize(
    "continuation",
    [{}, {"conversation_id": "resp_123"}, {"conversation_id": "conv_123"}, {"previous_response_id": "resp_456"}],
)
def test_instructions_ride_natively_without_system_prepend(continuation: dict[str, Any]) -> None:
    run_options = _prepare({"instructions": "be brief", **continuation})
    assert run_options["instructions"] == "be brief"
    assert "system" not in _input_roles(run_options)


def test_store_false_full_replay_keeps_instructions_native() -> None:
    run_options = _prepare({"instructions": "be brief", "store": False})
    assert run_options["instructions"] == "be brief"
    assert run_options["store"] is False
    assert "system" not in _input_roles(run_options)


def test_updated_instructions_reach_the_continuation_request() -> None:
    client = _client()
    first = client._build_request([_user()], {"instructions": "v1", "store": True})
    assert first["instructions"] == "v1"
    # Turn 2 continues a stored conversation with changed instructions: the
    # native param must carry the new value (a prepended system message would
    # have been suppressed here and the change silently dropped).
    second = client._build_request(
        [_user()],
        {"instructions": "v2", "store": True, "conversation_id": "resp_1"},
    )
    assert second["instructions"] == "v2"
    assert second["previous_response_id"] == "resp_1"
    assert "system" not in _input_roles(second)


@pytest.mark.parametrize(
    ("conversation_id", "handle", "other"),
    [("conv_1", "conversation", "previous_response_id"), ("resp_1", "previous_response_id", "conversation")],
)
def test_conversation_id_continues_a_conversation_or_a_response(conversation_id: str, handle: str, other: str) -> None:
    request = _prepare({"conversation_id": conversation_id})

    assert request[handle] == conversation_id
    assert other not in request
    assert "conversation_id" not in request


# ───────────────────────── response-format name sanitizing ─────────────────────────


def test_envelope_explicit_name_sanitized() -> None:
    envelope = {
        "type": "json_schema",
        "json_schema": {"name": "Weather Digest", "schema": {"type": "object"}},
    }
    run_options = _prepare({"response_format": envelope})
    assert run_options["text"]["format"]["name"] == "WeatherDigest"
    assert envelope["json_schema"]["name"] == "Weather Digest", "input not mutated"


def test_nested_format_mapping_name_sanitized() -> None:
    nested = {"format": {"type": "json_schema", "name": "bad name!", "schema": {"type": "object"}}}
    run_options = _prepare({"response_format": nested})
    assert run_options["text"]["format"]["name"] == "badname"
    assert nested["format"]["name"] == "bad name!", "input not mutated"


def test_raw_schema_cjk_title_falls_back_to_response() -> None:
    run_options = _prepare({"response_format": {"type": "object", "properties": {}, "title": "天气摘要"}})
    format_config = run_options["text"]["format"]
    assert format_config["type"] == "json_schema"
    assert format_config["name"] == "response"
    assert format_config["schema"]["additionalProperties"] is False


def test_raw_schema_long_title_truncates_to_64() -> None:
    run_options = _prepare({"response_format": {"type": "object", "properties": {}, "title": "T" * 80}})
    assert run_options["text"]["format"]["name"] == "T" * 64


def test_raw_schema_strictified_recursively() -> None:
    raw = {
        "type": "object",
        "properties": {
            "digest": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
            }
        },
    }
    format_config = _prepare({"response_format": raw})["text"]["format"]
    assert format_config["strict"] is True
    assert format_config["schema"]["required"] == ["digest"]
    nested = format_config["schema"]["properties"]["digest"]
    assert nested["additionalProperties"] is False
    assert nested["required"] == ["answer"]
    assert "additionalProperties" not in raw["properties"]["digest"], "input dict must not be mutated"


def test_unstrictifiable_raw_schema_sent_non_strict_verbatim() -> None:
    # Boolean subschemas are valid JSON Schema but the SDK strictifier
    # rejects them; forcing strict anyway would 400 on the API side, so the
    # schema goes to the wire non-strict exactly as the caller wrote it.
    raw = {
        "type": "object",
        "properties": {"anything": True},
        "title": "Loose",
    }
    format_config = _prepare({"response_format": raw})["text"]["format"]
    assert format_config["name"] == "Loose"
    assert "strict" not in format_config
    assert format_config["schema"] == {"type": "object", "properties": {"anything": True}}


@non_strict_schema_cases()
def test_non_strict_schema_sent_verbatim(title: str, raw: dict[str, Any]) -> None:
    format_config = _prepare({"response_format": {**deepcopy(raw), "title": title}})["text"]["format"]
    assert format_config["name"] == title
    assert "strict" not in format_config
    assert format_config["schema"] == raw


def test_mapping_proxy_response_format_supported() -> None:
    # The response_format contract is Mapping, which admits read-only views
    # that neither deep-copy nor pop; they must still convert.
    raw = MappingProxyType({"type": "object", "properties": {"answer": {"type": "string"}}, "title": "Prox"})
    format_config = _prepare({"response_format": raw})["text"]["format"]
    assert format_config["name"] == "Prox"
    assert format_config["strict"] is True
    assert format_config["schema"]["required"] == ["answer"]
    assert "title" in raw, "read-only input must stay untouched"


def test_nested_mapping_proxy_response_format_supported() -> None:
    # Read-only views can sit at any depth, not just the root — deepcopy
    # falls back to pickling them and raises; the materializing copy must
    # convert the whole tree to plain containers.
    raw = MappingProxyType(
        {
            "type": "object",
            "properties": MappingProxyType({"answer": MappingProxyType({"type": "string", "enum": ("a", "b")})}),
            "title": "DeepProx",
        }
    )
    format_config = _prepare({"response_format": raw})["text"]["format"]
    assert format_config["name"] == "DeepProx"
    assert format_config["strict"] is True
    assert isinstance(format_config["schema"]["properties"], dict)
    assert format_config["schema"]["properties"]["answer"]["enum"] == ["a", "b"]
    assert format_config["schema"]["required"] == ["answer"]


_FT_MODEL = "ft:gpt-4o-mini:acme::abc123"

_CONSTRAINED_RAW_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string", "minLength": 2}},
}


def test_fine_tuned_model_constraint_keywords_sent_non_strict() -> None:
    # Structured Outputs rejects constraint keywords on fine-tuned models
    # only — strict:true would 400 there, so the schema goes to the wire
    # non-strict, verbatim. Mirrors the Chat Completions client's gate.
    run_options = _prepare({"model": _FT_MODEL, "response_format": dict(_CONSTRAINED_RAW_SCHEMA)})
    format_config = run_options["text"]["format"]
    assert "strict" not in format_config
    assert format_config["schema"] == _CONSTRAINED_RAW_SCHEMA


def test_base_model_keeps_strict_with_constraint_keywords() -> None:
    # Base models support the constraint keywords under strict mode; the
    # fine-tune gate must not cost them the strict guarantee.
    run_options = _prepare({"model": "gpt-4o", "response_format": dict(_CONSTRAINED_RAW_SCHEMA)})
    format_config = run_options["text"]["format"]
    assert format_config["strict"] is True
    assert format_config["schema"]["properties"]["code"]["minLength"] == 2


def test_fine_tuned_model_without_constraint_keywords_stays_strict() -> None:
    raw = {"type": "object", "properties": {"code": {"type": "string"}}}
    run_options = _prepare({"model": _FT_MODEL, "response_format": raw})
    assert run_options["text"]["format"]["strict"] is True


@pytest.mark.parametrize(
    "raw",
    [
        {"type": ["object", "null"], "properties": {"a": {"type": "string"}}},
        {"enum": ["red", "green"]},
    ],
)
def test_unusual_raw_schema_shapes_wrapped_non_strict(raw: dict[str, Any]) -> None:
    # JSON Schema admits array-valued and absent ``type`` (enum-only), so
    # both classify as raw schemas — not a crash, a passthrough, or a
    # rejection. Neither satisfies the strict root contract, so they wrap
    # non-strict verbatim.
    format_config = _prepare({"response_format": {**raw, "title": "Odd"}})["text"]["format"]
    assert format_config["name"] == "Odd"
    assert "strict" not in format_config
    assert format_config["schema"] == raw


def test_empty_raw_schema_wrapped_non_strict() -> None:
    # An empty mapping is a valid type-less JSON Schema and wraps through
    # the non-strict raw path; pinned for parity with the CC client.
    format_config = _prepare({"response_format": {}})["text"]["format"]
    assert format_config["name"] == "response"
    assert "strict" not in format_config
    assert format_config["schema"] == {}


def test_top_level_text_config_name_sanitized() -> None:
    text = {"format": {"type": "json_schema", "name": "bad name", "schema": {"type": "object"}}}
    run_options = _prepare({"text": text})
    assert run_options["text"]["format"]["name"] == "badname"
    assert text["format"]["name"] == "bad name", "input not mutated"


def test_valid_name_passes_unchanged() -> None:
    envelope = {
        "type": "json_schema",
        "json_schema": {"name": "Good_Name-1", "schema": {"type": "object"}},
    }
    run_options = _prepare({"response_format": envelope})
    assert run_options["text"]["format"]["name"] == "Good_Name-1"


def test_non_json_schema_text_mapping_passes_byte_identical() -> None:
    text = {"format": {"type": "text"}}
    run_options = _prepare({"text": text})
    assert run_options["text"] is text


def test_combined_text_and_response_format_never_mutates_the_caller_mapping() -> None:
    # A reused profile ``text`` config combined with a per-turn
    # ``response_format`` must get the converted format on a copy: writing
    # into the caller's mapping would leak one turn's format into every later
    # request and misreport a format conflict on the next turn.
    text_profile = {"verbosity": "low"}
    first = _prepare({"text": text_profile, "response_format": {"type": "object", "properties": {}, "title": "First"}})
    assert first["text"]["verbosity"] == "low"
    assert first["text"]["format"]["name"] == "First"
    assert "format" not in text_profile, "caller's reused mapping must stay clean"

    second = _prepare(
        {"text": text_profile, "response_format": {"type": "object", "properties": {}, "title": "Second"}}
    )
    assert second["text"]["format"]["name"] == "Second"
    assert "format" not in text_profile


def test_model_class_text_format_invalid_name_renamed_keeping_parse_target() -> None:
    # The SDK derives the wire schema name from the class __name__ unchanged,
    # so a legal-Python Unicode class name must be replaced before the wire —
    # via a renamed subclass, so parsed outputs are still instances of the
    # caller's model.
    weather = type("天气摘要", (BaseModel,), {"__annotations__": {"answer": str}})
    run_options = _prepare({"response_format": weather})
    text_format = run_options["text_format"]
    assert text_format.__name__ == "response"
    assert text_format is not weather
    assert issubclass(text_format, weather), "original model stays the parse target"


def test_model_class_text_format_valid_name_passes_identical() -> None:
    class Good_Digest1(BaseModel):
        answer: str

    run_options = _prepare({"response_format": Good_Digest1})
    assert run_options["text_format"] is Good_Digest1


# ───────────────────────── cache_write_tokens extraction ─────────────────────────


def _usage(**detail_kwargs: Any) -> ResponseUsage:
    # Built without validation: the SDK requires cache_write_tokens, but an
    # OpenAI-compatible service may leave it out.
    return ResponseUsage(
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        input_tokens_details=InputTokensDetails.model_construct(cached_tokens=3, **detail_kwargs),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=1),
    )


def test_cache_write_tokens_extracted() -> None:
    details = decode_usage(_usage(cache_write_tokens=7), variant=OPENAI_RESPONSES)
    assert details is not None
    assert details["openai.cache_write_tokens"] == 7
    assert details["cache_creation_input_token_count"] == 7


def test_cache_write_tokens_zero_preserved() -> None:
    details = decode_usage(_usage(cache_write_tokens=0), variant=OPENAI_RESPONSES)
    assert details is not None
    assert details["openai.cache_write_tokens"] == 0
    assert details["cache_creation_input_token_count"] == 0


def test_cache_write_tokens_absent_omits_both_keys() -> None:
    details = decode_usage(_usage(), variant=OPENAI_RESPONSES)
    assert details is not None
    assert "openai.cache_write_tokens" not in details
    assert "cache_creation_input_token_count" not in details
    assert details["cache_read_input_token_count"] == 3


_ANSWER = {
    "type": "message",
    "id": "msg_1",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Sunny.", "annotations": []}],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_a_service_that_omits_cache_write_tokens_keeps_its_cache_reads(stream: bool) -> None:
    """The SDK's own response parsing tolerates the missing field, and a zero cache read survives."""
    reply = Script().started().text(0, "msg_1", "Sunny.").finished(_ANSWER).reply() if stream else blocking(_ANSWER)

    response, _ = await respond(reply, stream=stream)

    usage = response.usage_details
    assert usage is not None
    assert usage["cache_read_input_token_count"] == 0
    assert "cache_creation_input_token_count" not in usage
    assert "openai.cache_write_tokens" not in usage
