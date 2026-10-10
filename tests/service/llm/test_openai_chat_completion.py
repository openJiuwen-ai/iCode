# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat Completions wire-prep pins: author-name sanitizing, response-format
normalization, and cache-write usage extraction.

The ``name`` field on wire messages accepts CJK and hyphens but rejects
whitespace and ``< | \\ / >`` (the API's documented charset); the
response-format schema ``name`` is stricter (``[A-Za-z0-9_-]``, ≤64). Both
sanitizers run at the wire boundary so persisted or caller-supplied values
can never 400 a request Chrys itself constructs.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from types import MappingProxyType, SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, ChoiceDelta
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage, PromptTokensDetails
from pydantic import BaseModel

from chrys.foundation.errors import ErrorKind
from chrys.foundation.errors.display import describe_error
from chrys.kernel import ChatResponse, Content, Message
from chrys.kernel.exceptions import (
    ChatClientException,
    ChatClientInvalidRequestException,
    ChatClientInvalidResponseException,
)
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.chat_completions.client import DEEPSEEK, GLM, OPENAI, ChatCompletionsVariant
from chrys.service.llm.chat_completions.decode import decode_completion, decode_usage, ensure_choices, text_contents
from chrys.service.llm.chat_completions.history import encode_message, sanitize_author_name
from chrys.service.llm.chat_completions.validation import (
    _INVALID_STREAM_BODY_CAPTURE_LIMIT,
    _strip_leading_bom_from_decoded_bytes,
    bounded_body_preview,
    validate_stream_response,
    zero_event_message,
)
from tests.service.llm._wire_stacks import assemble_openai_stack
from tests.support.openai_chat_wire import parse_stream_chunks


class _UnusedCompletions:
    async def create(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("wire-prep tests should not call the SDK")


class _UnusedChat:
    def __init__(self) -> None:
        self.completions = _UnusedCompletions()


class _UnusedAsyncOpenAI:
    base_url = "https://api.test"

    def __init__(self) -> None:
        self.chat = _UnusedChat()


def _client() -> ChatCompletionsClient:
    return ChatCompletionsClient(model="glm-5.2", sdk_client=_UnusedAsyncOpenAI())


def _user(text: str = "hi", **kwargs: Any) -> Message:
    return Message(role="user", contents=[text], **kwargs)


def test_response_body_preview_marks_empty_and_has_a_hard_limit() -> None:
    assert bounded_body_preview("") == "<empty>"

    preview = bounded_body_preview("x" * 3000)

    assert len(preview) == 2000
    assert preview.endswith("...[truncated]")

    assert bounded_body_preview("abc", truncated=True) == "'abc'...[truncated]"
    assert len(bounded_body_preview("x" * 3000, truncated=True)) == 2000


class _ChunkedByteStream(httpx.AsyncByteStream):
    """Deliver a body in fixed chunks so the sniff window spans several reads."""

    def __init__(self, items: list[bytes | BaseException]) -> None:
        self._items = items
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for item in self._items:
            if isinstance(item, BaseException):
                raise item
            yield item

    async def aclose(self) -> None:
        self.closed = True


class _RawResponseStandIn:
    """The subset of the SDK's raw response that the stream validator touches."""

    def __init__(self, http_response: httpx.Response) -> None:
        self.http_response = http_response

    @property
    def status_code(self) -> int:
        return self.http_response.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self.http_response.headers

    @property
    def text(self) -> str:
        return self.http_response.text


class _EndlessByteStream(httpx.AsyncByteStream):
    """A gateway that never stops sending a non-SSE body."""

    def __init__(self, chunk: bytes) -> None:
        self._chunk = chunk
        self.yielded = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            self.yielded += 1
            yield self._chunk

    async def aclose(self) -> None:
        self.closed = True


def _streaming_raw_response(
    items: list[bytes | BaseException],
    *,
    content_type: str | None = "text/event-stream",
    content_encoding: str | None = None,
) -> tuple[_RawResponseStandIn, _ChunkedByteStream]:
    stream = _ChunkedByteStream(items)
    headers = {} if content_type is None else {"content-type": content_type}
    if content_encoding is not None:
        headers["content-encoding"] = content_encoding
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    return _RawResponseStandIn(httpx.Response(200, headers=headers, stream=stream, request=request)), stream


async def test_stream_sniff_replays_padded_prefix_and_delegates_close() -> None:
    chunks = [b"\r\n", b"\xef\xbb\xbf", b": keepalive\n\ndata: [DONE]\n\n"]
    raw, stream = _streaming_raw_response(chunks, content_type="application/json")

    await validate_stream_response(raw)

    replayed = b"".join([chunk async for chunk in raw.http_response.aiter_raw()])
    assert replayed == b"".join(chunks)
    assert stream.closed


async def test_stream_sniff_leaves_padding_beyond_window_to_the_decoder() -> None:
    raw, stream = _streaming_raw_response([b"\n" * 600, b'{"error":"late"}'])

    await validate_stream_response(raw)

    assert not stream.closed


async def test_stream_sniff_rejects_blank_body_and_releases_the_connection() -> None:
    raw, stream = _streaming_raw_response([b"\r\n", b"  "], content_type=None)

    with pytest.raises(ChatClientException) as exc_info:
        await validate_stream_response(raw)

    message = str(exc_info.value)
    assert "Content-Type '<missing>'" in message
    assert "streaming response body is blank" in message
    assert stream.closed


async def test_stream_sniff_rejects_json_first_chunk_with_full_body() -> None:
    # No error envelope (``test_chat_completions_error_envelopes.py``): just a JSON document.
    raw, stream = _streaming_raw_response([b"{", b'"status":"gateway broke"}'])

    with pytest.raises(ChatClientException) as exc_info:
        await validate_stream_response(raw)

    # The outer wrapper carries (message, inner_exception) as args, so its str()
    # is a repr-escaped tuple; the user-facing text is the cause's own message.
    detail = str(exc_info.value.__cause__)
    assert "starts with '{'; expected an SSE event stream" in detail
    assert 'Response body: \'{"status":"gateway broke"}\'' in detail
    assert stream.closed


async def test_stream_sniff_caps_capture_of_an_endless_non_sse_body() -> None:
    chunk = b"<html>" + b"x" * 1024
    stream = _EndlessByteStream(chunk)
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    raw = _RawResponseStandIn(
        httpx.Response(200, headers={"content-type": "text/html"}, stream=stream, request=request)
    )

    with pytest.raises(ChatClientException) as exc_info:
        await validate_stream_response(raw)

    detail = str(exc_info.value.__cause__)
    assert "starts with '<'; expected an SSE event stream" in detail
    assert detail.endswith("...[truncated]")
    assert stream.yielded <= _INVALID_STREAM_BODY_CAPTURE_LIMIT // len(chunk) + 2
    assert stream.closed


async def test_stream_sniff_keeps_verdict_when_capture_read_fails() -> None:
    raw, stream = _streaming_raw_response([b"{", httpx.ReadTimeout("stalled")])

    with pytest.raises(ChatClientException) as exc_info:
        await validate_stream_response(raw)

    detail = str(exc_info.value.__cause__)
    assert "starts with '{'; expected an SSE event stream" in detail
    assert "Response body: '{'...[truncated]" in detail
    assert stream.closed


async def test_stream_sniff_classifies_decoded_bytes_under_content_encoding() -> None:
    document = gzip.compress(b'{"detail":{"message":"gateway broke"}}')
    raw, stream = _streaming_raw_response([document], content_type="application/json", content_encoding="gzip")

    with pytest.raises(ChatClientException) as exc_info:
        await validate_stream_response(raw)

    detail = str(exc_info.value.__cause__)
    assert "starts with '{'; expected an SSE event stream" in detail
    assert 'Response body: \'{"detail":{"message":"gateway broke"}}\'' in detail
    assert stream.closed

    sse = b"data: [DONE]\n\n"
    raw, _ = _streaming_raw_response([gzip.compress(sse)], content_encoding="gzip")

    await validate_stream_response(raw)

    # The replay carries raw bytes; httpx still decodes them for the SDK.
    assert b"".join([chunk async for chunk in raw.http_response.aiter_bytes()]) == sse


@pytest.mark.parametrize(
    "body",
    [b"upstream connect error", b"null", b'"a string"', b"42", b"dat", b"x" * 600],
    ids=["plain-text", "null", "string", "number", "cut-off-field-name", "colon-less-text-beyond-the-window"],
)
async def test_stream_sniff_rejects_bodies_without_sse_framing(body: bytes) -> None:
    raw, stream = _streaming_raw_response([body])

    with pytest.raises(ChatClientException) as exc_info:
        await validate_stream_response(raw)

    assert "expected an SSE event stream" in str(exc_info.value.__cause__)
    assert stream.closed


@pytest.mark.parametrize(
    "items",
    [
        [b"data: [DONE]\n\n"],
        [b"event: ping\n\n"],
        [b"id: 1\n\n"],
        [b"retry: 5\n\n"],
        [b"da", b"ta: x\n\n"],
        [b"x-proxy: ignored\n", b"data: [DONE]\n\n"],
        [b"x.proxy/v1 (odd): ignored\n", b"data: [DONE]\n\n"],
        [b"x-proxy\n", b"data: [DONE]\n\n"],
        [b"Error: boom\n"],
    ],
    ids=[
        "data",
        "event",
        "id",
        "retry",
        "field-name-split-across-chunks",
        "unknown-field-left-to-the-sdk",
        "unknown-field-with-any-name-bytes",
        "colon-less-unknown-field-before-data",
        "colon-shaped-text-left-to-the-zero-event-guard",
    ],
)
async def test_stream_sniff_accepts_sse_framing(items: list[bytes]) -> None:
    raw, _ = _streaming_raw_response(list(items))

    await validate_stream_response(raw)

    assert b"".join([chunk async for chunk in raw.http_response.aiter_raw()]) == b"".join(items)


@pytest.mark.parametrize("content_encoding", [None, "gzip"], ids=["identity", "gzip"])
async def test_stream_sniff_strips_a_leading_bom_from_the_decoded_stream(content_encoding: str | None) -> None:
    sse = b"\xef\xbb\xbfdata: [DONE]\n\n"
    body = sse if content_encoding is None else gzip.compress(sse)
    raw, _ = _streaming_raw_response([body], content_encoding=content_encoding)

    await validate_stream_response(raw)

    # The SDK reads decoded bytes; that is where the BOM must be gone.
    assert b"".join([chunk async for chunk in raw.http_response.aiter_bytes()]) == b"data: [DONE]\n\n"


async def test_decoded_bom_strip_tolerates_a_split_first_chunk() -> None:
    class _Decoded:
        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            for chunk in (b"", b"\xef", b"\xbb", b"\xbfdata: [DONE]\n\n", b"data: x\n\n"):
                yield chunk

    response = _Decoded()
    _strip_leading_bom_from_decoded_bytes(response)

    assert [chunk async for chunk in response.aiter_bytes()] == [b"data: [DONE]\n\n", b"data: x\n\n"]


async def test_stream_sniff_falls_back_to_utf8_for_an_unknown_charset() -> None:
    raw, stream = _streaming_raw_response([b"<html>bad gateway</html>"], content_type="text/html; charset=madeup")

    with pytest.raises(ChatClientException) as exc_info:
        await validate_stream_response(raw)

    assert str(exc_info.value.__cause__).endswith("Response body: '<html>bad gateway</html>'")
    assert stream.closed


async def test_stream_replay_records_what_the_decoder_saw_for_the_zero_event_guard() -> None:
    raw, _ = _streaming_raw_response([b": keep", b"alive\n\n", b": still nothing\n\n"])

    replay = await validate_stream_response(raw)

    # Before the decoder drains the body only the sniffed prefix is known.
    assert zero_event_message(replay).endswith("Response body: ': keep'...[truncated]")

    async for _ in raw.http_response.aiter_bytes():
        pass

    assert zero_event_message(replay) == (
        "Chat Completions API returned an invalid response (HTTP 200, Content-Type 'text/event-stream'): "
        "stream ended without any events. Response body: ': keepalive\\n\\n: still nothing\\n\\n'"
    )


async def test_stream_sniff_propagates_transport_failure_before_a_verdict() -> None:
    raw, stream = _streaming_raw_response([httpx.ReadTimeout("stalled")])

    with pytest.raises(httpx.ReadTimeout):
        await validate_stream_response(raw)

    assert stream.closed


# ───────────────────────── author_name sanitizing ─────────────────────────


def test_sanitize_author_name_value_rules() -> None:
    assert sanitize_author_name("my agent") == "myagent"
    assert sanitize_author_name("小助手") == "小助手", "CJK is valid for the wire name field"
    assert sanitize_author_name("my-agent_2") == "my-agent_2"
    assert sanitize_author_name("a<b|c\\d/e>f") == "abcdef"
    assert sanitize_author_name("< | / >") is None, "empty after sanitize omits the key"
    assert sanitize_author_name(None) is None
    assert sanitize_author_name("") is None
    truncated = sanitize_author_name("a b" + "c" * 80)
    assert truncated is not None
    assert len(truncated) == 64
    assert truncated == ("ab" + "c" * 80)[:64]


def test_system_message_author_name_sanitized() -> None:
    message = Message(role="system", contents=["be nice"], author_name="sys helper")
    (prepared,) = encode_message(message, variant=OPENAI)
    assert prepared["name"] == "syshelper"


def test_user_message_author_name_sanitized() -> None:
    (prepared,) = encode_message(_user(author_name="user one"), variant=OPENAI)
    assert prepared["name"] == "userone"


def test_unsanitizable_author_name_omits_the_key() -> None:
    (prepared,) = encode_message(_user(author_name="< / >"), variant=OPENAI)
    assert "name" not in prepared


def test_tool_result_message_never_carries_a_name() -> None:
    message = Message(
        role="tool",
        contents=[Content.from_function_result(call_id="c1", result="ok")],
        author_name="tool helper",
    )
    (prepared,) = encode_message(message, variant=OPENAI)
    assert "name" not in prepared


@pytest.mark.parametrize(
    ("uri", "media_type"),
    [
        ("data:video/mp4;base64,AAAA", "video/mp4"),
        ("data:audio/ogg;base64,AAAA", "audio/ogg"),
        ("https://example.com/speech.wav", "audio/wav"),
        ("https://example.com/report.pdf", "application/pdf"),
    ],
    ids=["video", "unknown-audio-format", "audio-link", "application-link"],
)
def test_media_without_a_wire_part_goes_out_as_placeholder_text(
    uri: str, media_type: str, caplog: pytest.LogCaptureFixture
) -> None:
    message = Message(role="user", contents=[Content.from_uri(uri=uri, media_type=media_type)])
    with caplog.at_level("DEBUG", logger="chrys.service.llm.chat_completions.history"):
        (prepared,) = encode_message(message, variant=OPENAI)
    assert prepared == {
        "role": "user",
        "content": f"[{media_type} content omitted: the Chat Completions API does not accept it.]",
    }
    assert [record.getMessage() for record in caplog.records] == [
        f"Chat Completions has no content part for {media_type}; a placeholder text goes out instead."
    ]


def test_reasoning_coalescer_aggregate_sanitizes_name_and_keeps_reasoning() -> None:
    # The reasoning-bearing encoder builds its own aggregate message; the
    # sanitize must touch only ``name``, never the reasoning keys.
    reasoning = Content.from_text_reasoning(
        text="thinking",
        additional_properties={"openai_reasoning_format": "reasoning_content"},
    )
    message = Message(
        role="assistant",
        contents=[reasoning, Content.from_text("answer")],
        author_name="agent one",
    )
    (aggregate,) = encode_message(message, variant=OPENAI)
    assert aggregate["name"] == "agentone"
    assert aggregate["reasoning_content"] == "thinking"
    assert aggregate["content"] == "answer"


def test_reasoning_coalescer_standalone_non_text_content_sanitizes_name() -> None:
    # A non-text fragment (e.g. an image) keeps its standalone emission ahead
    # of the run's aggregate; that standalone message carries the sanitized
    # name too.
    reasoning = Content.from_text_reasoning(
        text="thinking",
        additional_properties={"openai_reasoning_format": "reasoning_content"},
    )
    message = Message(
        role="assistant",
        contents=[
            reasoning,
            Content.from_text("answer"),
            Content.from_uri(uri="https://example.com/img.png", media_type="image/png"),
        ],
        author_name="agent one",
    )
    standalone, aggregate = encode_message(message, variant=OPENAI)
    assert standalone["content"][0]["type"] == "image_url"
    assert standalone["name"] == "agentone"
    assert aggregate["name"] == "agentone"
    assert aggregate["reasoning_content"] == "thinking"


def test_reasoning_props_carrier_message_sanitizes_name() -> None:
    # Message-level reasoning props with no wire carrier synthesize an
    # empty-content assistant message; it carries the sanitized name.
    message = Message(
        role="assistant",
        contents=[],
        additional_properties={"reasoning_content": "props chain", "openai_reasoning_format": "reasoning_content"},
        author_name="agent one",
    )
    (carrier,) = encode_message(message, variant=OPENAI)
    assert carrier == {
        "role": "assistant",
        "content": "",
        "reasoning_content": "props chain",
        "name": "agentone",
    }


def test_restored_history_author_name_sanitized_on_wire() -> None:
    # A deserialized message carrying an invalid persisted author_name must
    # still serialize to the wire sanitized.
    restored = Message.from_dict(_user(author_name="restored user").to_dict())
    assert restored.author_name == "restored user"
    (prepared,) = encode_message(restored, variant=OPENAI)
    assert prepared["name"] == "restoreduser"


def test_deepseek_sites_sanitize_author_name() -> None:
    (sys_prepared,) = encode_message(Message(role="system", contents=["s"], author_name="sys helper"), variant=DEEPSEEK)
    assert sys_prepared["name"] == "syshelper"
    (user_prepared,) = encode_message(_user(author_name="user one"), variant=DEEPSEEK)
    assert user_prepared["name"] == "userone"


# ───────────────────────── response_format normalization ─────────────────────────


def _prepared_response_format(response_format: Any) -> Any:
    run_options = _client()._build_request([_user()], {"response_format": response_format})
    return run_options["response_format"]


def test_raw_object_schema_is_wrapped() -> None:
    raw = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "title": "Weather Digest",
    }
    original = {**raw, "properties": dict(raw["properties"])}
    wrapped = _prepared_response_format(raw)
    assert wrapped["type"] == "json_schema"
    envelope = wrapped["json_schema"]
    assert envelope["name"] == "WeatherDigest"
    assert envelope["strict"] is True
    assert envelope["schema"]["additionalProperties"] is False
    # Strict mode rejects schemas whose objects omit ``required``.
    assert envelope["schema"]["required"] == ["answer"]
    assert "title" not in envelope["schema"]
    assert raw == original, "input dict must not be mutated"


def test_keyword_only_schema_is_wrapped() -> None:
    # A type-less root violates the strict-mode root contract (the root must
    # be an object schema), so the wrap goes out non-strict, verbatim.
    wrapped = _prepared_response_format({"properties": {"a": {"type": "integer"}}})
    assert wrapped["type"] == "json_schema"
    envelope = wrapped["json_schema"]
    assert envelope["name"] == "response"
    assert "strict" not in envelope
    assert envelope["schema"] == {"properties": {"a": {"type": "integer"}}}


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
    envelope = _prepared_response_format(raw)["json_schema"]
    assert envelope["strict"] is True
    nested = envelope["schema"]["properties"]["digest"]
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
    wrapped = _prepared_response_format(raw)
    envelope = wrapped["json_schema"]
    assert envelope["name"] == "Loose"
    assert "strict" not in envelope
    assert envelope["schema"] == {"type": "object", "properties": {"anything": True}}


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
        {
            "type": "object",
            "properties": {"inner": {"type": "object", "properties": {}, "additionalProperties": True}},
        },
        {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}},
    ],
)
def test_explicitly_open_schema_sent_non_strict_verbatim(raw: dict[str, Any]) -> None:
    # The strictifier preserves an explicit non-false additionalProperties
    # anywhere in the tree; strict mode rejects exactly that, so the schema
    # must go to the wire non-strict, verbatim.
    envelope = _prepared_response_format({**raw, "title": "Open"})["json_schema"]
    assert envelope["name"] == "Open"
    assert "strict" not in envelope
    assert envelope["schema"] == raw


@pytest.mark.parametrize(
    "raw",
    [
        {
            "type": "object",
            "properties": {"choice": {"oneOf": [{"type": "object", "properties": {"value": {"type": "string"}}}]}},
        },
        {
            "type": "object",
            "properties": {
                "x": {"allOf": [{"type": "object", "properties": {}}, {"type": "object", "properties": {}}]}
            },
        },
        {"type": "object", "properties": {}, "patternProperties": {"^x": {"type": "object", "properties": {}}}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "dependentRequired": {"a": ["b"]}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "dependentSchemas": {"a": {"type": "string"}}},
        {
            "type": "object",
            "properties": {},
            "patternProperties": {
                "^x": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "additionalProperties": False,
                }
            },
        },
        {
            "type": "object",
            "properties": {},
            "patternProperties": {
                "^x": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "additionalProperties": False,
                    "required": ["value", "value"],
                }
            },
        },
        {
            "type": "object",
            "properties": {},
            "patternProperties": {
                "^x": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "additionalProperties": False,
                    "required": [["value"]],
                }
            },
        },
    ],
)
def test_unstrictified_branches_sent_non_strict_verbatim(raw: dict[str, Any]) -> None:
    # The strictifier does not traverse oneOf or patternProperties (their
    # objects stay open) and leaves multi-entry allOf and the dependent*
    # keywords in place; strict mode rejects all of them, so none may go out
    # strict. The dependent* values hold property names or primitive
    # subschemas, so only the keyword gate can catch them. The last three
    # shapes exercise the required-completeness leg on a pre-closed object:
    # missing required, duplicate names that a bare set comparison would
    # collapse, and an unhashable entry that must fall back instead of
    # raising from set().
    envelope = _prepared_response_format({**raw, "title": "Branch"})["json_schema"]
    assert envelope["name"] == "Branch"
    assert "strict" not in envelope
    assert envelope["schema"] == raw


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "string"},
        {"type": "object", "properties": {}, "anyOf": [{"type": "object", "properties": {}}]},
        {"anyOf": [{"type": "object", "properties": {}}, {"type": "object", "properties": {}}]},
    ],
)
def test_strict_root_contract_violations_sent_non_strict_verbatim(raw: dict[str, Any]) -> None:
    # Structured Outputs requires the root to be an object schema and
    # forbids a root-level anyOf; the strictifier passes such roots through
    # unchanged, so they must go to the wire non-strict, verbatim.
    envelope = _prepared_response_format({**raw, "title": "Root"})["json_schema"]
    assert envelope["name"] == "Root"
    assert "strict" not in envelope
    assert envelope["schema"] == raw


def test_mapping_proxy_response_format_supported() -> None:
    # The response_format contract is Mapping, which admits read-only views;
    # a dict-only gate would misroute them to the SDK's model-class helper.
    raw = MappingProxyType({"type": "object", "properties": {"answer": {"type": "string"}}, "title": "Prox"})
    envelope = _prepared_response_format(raw)["json_schema"]
    assert envelope["name"] == "Prox"
    assert envelope["strict"] is True
    assert envelope["schema"]["required"] == ["answer"]
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
    envelope = _prepared_response_format(raw)["json_schema"]
    assert envelope["name"] == "DeepProx"
    assert envelope["strict"] is True
    assert isinstance(envelope["schema"]["properties"], dict)
    assert envelope["schema"]["properties"]["answer"]["enum"] == ["a", "b"]
    assert envelope["schema"]["required"] == ["answer"]


_FT_MODEL = "ft:gpt-4o-mini:acme::abc123"

_CONSTRAINED_RAW_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string", "minLength": 2}},
}


def _prepared_response_format_for_model(model: str, response_format: Any) -> Any:
    run_options = _client()._build_request([_user()], {"model": model, "response_format": response_format})
    return run_options["response_format"]


@pytest.mark.parametrize(
    "constrained_property",
    [
        {"type": "string", "minLength": 2},
        {"type": "integer", "maximum": 10},
        {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    ],
    ids=["string-bound", "number-bound", "array-bound"],
)
def test_fine_tuned_model_constraint_keywords_sent_non_strict(constrained_property: dict[str, Any]) -> None:
    # Structured Outputs rejects constraint keywords on fine-tuned models
    # only — strict:true would 400 there, so the schema goes to the wire
    # non-strict, verbatim.
    raw = {"type": "object", "properties": {"code": constrained_property}, "title": "Constrained"}
    envelope = _prepared_response_format_for_model(_FT_MODEL, raw)["json_schema"]
    assert envelope["name"] == "Constrained"
    assert "strict" not in envelope
    assert envelope["schema"] == {"type": "object", "properties": {"code": constrained_property}}


def test_base_model_keeps_strict_with_constraint_keywords() -> None:
    # Base models support the constraint keywords under strict mode; the
    # fine-tune gate must not cost them the strict guarantee.
    envelope = _prepared_response_format_for_model("gpt-4o", _CONSTRAINED_RAW_SCHEMA)["json_schema"]
    assert envelope["strict"] is True
    assert envelope["schema"]["properties"]["code"]["minLength"] == 2


def test_fine_tuned_model_without_constraint_keywords_stays_strict() -> None:
    raw = {"type": "object", "properties": {"code": {"type": "string"}}}
    envelope = _prepared_response_format_for_model(_FT_MODEL, raw)["json_schema"]
    assert envelope["strict"] is True


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
    envelope = _prepared_response_format({**raw, "title": "Odd"})["json_schema"]
    assert envelope["name"] == "Odd"
    assert "strict" not in envelope
    assert envelope["schema"] == raw


def test_empty_raw_schema_wrapped_non_strict() -> None:
    # An empty mapping is a valid type-less JSON Schema; a truthiness gate
    # would leave it riding the seeded options copy unwrapped.
    envelope = _prepared_response_format({})["json_schema"]
    assert envelope["name"] == "response"
    assert "strict" not in envelope
    assert envelope["schema"] == {}


def test_json_object_and_text_pass_through_unchanged() -> None:
    for passthrough in ({"type": "json_object"}, {"type": "text"}):
        assert _prepared_response_format(passthrough) is passthrough


def test_cjk_title_falls_back_to_response() -> None:
    wrapped = _prepared_response_format({"type": "object", "properties": {}, "title": "天气摘要"})
    assert wrapped["json_schema"]["name"] == "response"


def test_long_title_truncates_to_64() -> None:
    wrapped = _prepared_response_format({"type": "object", "properties": {}, "title": "T" * 80})
    assert wrapped["json_schema"]["name"] == "T" * 64


def test_enveloped_dict_with_invalid_name_sanitized_copy_on_write() -> None:
    envelope = {
        "type": "json_schema",
        "json_schema": {"name": "bad name!", "schema": {"type": "object"}},
    }
    prepared = _prepared_response_format(envelope)
    assert prepared["json_schema"]["name"] == "badname"
    assert envelope["json_schema"]["name"] == "bad name!", "input not mutated"


def test_enveloped_dict_with_valid_name_passes_identically() -> None:
    envelope = {
        "type": "json_schema",
        "json_schema": {"name": "Good_Name-1", "schema": {"type": "object"}},
    }
    assert _prepared_response_format(envelope) is envelope


def test_model_class_name_is_sanitized() -> None:
    # The SDK helper copies the class __name__ into json_schema.name
    # unchecked; a PEP 3131 identifier would 400 without the sanitize.
    class _Digest(BaseModel):
        answer: str

    _Digest.__name__ = "天气 Digest"
    prepared = _prepared_response_format(_Digest)
    assert prepared["json_schema"]["name"] == "Digest"
    assert prepared["json_schema"]["strict"] is True
    assert prepared["json_schema"]["schema"]["additionalProperties"] is False
    assert prepared["json_schema"]["schema"]["required"] == ["answer"]


# ───────────────────────── cache_write_tokens extraction ─────────────────────────


def _usage(**detail_kwargs: Any) -> CompletionUsage:
    return CompletionUsage(
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        prompt_tokens_details=PromptTokensDetails(cached_tokens=3, **detail_kwargs),
    )


def test_cache_write_tokens_extracted() -> None:
    details = decode_usage(_usage(cache_write_tokens=7), variant=OPENAI)
    assert details["prompt/cache_write_tokens"] == 7
    assert details["cache_creation_input_token_count"] == 7


def test_cache_write_tokens_zero_preserved() -> None:
    details = decode_usage(_usage(cache_write_tokens=0), variant=OPENAI)
    assert details["prompt/cache_write_tokens"] == 0
    assert details["cache_creation_input_token_count"] == 0


def test_cache_write_tokens_absent_omits_both_keys() -> None:
    details = decode_usage(_usage(), variant=OPENAI)
    assert "prompt/cache_write_tokens" not in details
    assert "cache_creation_input_token_count" not in details
    assert details["cache_read_input_token_count"] == 3


# ─────────────── Kimi: choice-level stream usage, top-level cached_tokens ───────────────


def _kimi_usage(**extra: Any) -> dict[str, Any]:
    return {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, **extra}


def _kimi_chunk(*, choice_usage: Any = None, chunk_usage: Any = None) -> ChatCompletionChunk:
    choice: dict[str, Any] = {"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}
    if choice_usage is not None:
        choice["usage"] = choice_usage
    return ChatCompletionChunk.model_validate(
        {
            "id": "chunk-kimi",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "kimi-k2",
            "choices": [choice],
            "usage": chunk_usage,
        }
    )


def _usage_contents(update: Any) -> list[Content]:
    return [content for content in update.contents if content.type == "usage"]


def test_choice_level_stream_usage_is_reported() -> None:
    (update,) = parse_stream_chunks(_client(), _kimi_chunk(choice_usage=_kimi_usage(cached_tokens=64)))

    [usage] = _usage_contents(update)
    assert usage.usage_details == {
        "input_token_count": 100,
        "output_token_count": 20,
        "total_token_count": 120,
        "prompt/cached_tokens": 64,
        "cache_read_input_token_count": 64,
    }
    assert update.text == "done"


def test_chunk_level_stream_usage_wins_over_the_choice_level_copy() -> None:
    chunk = _kimi_chunk(
        choice_usage=_kimi_usage(),
        chunk_usage={"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
    )

    [usage] = _usage_contents(parse_stream_chunks(_client(), chunk)[0])

    assert usage.usage_details is not None
    assert usage.usage_details["input_token_count"] == 7


@pytest.mark.parametrize("choice_usage", ["n/a", {"prompt_tokens": "many"}, {"prompt_tokens": 1}])
def test_malformed_choice_level_usage_is_ignored(choice_usage: Any) -> None:
    (update,) = parse_stream_chunks(_client(), _kimi_chunk(choice_usage=choice_usage))

    assert _usage_contents(update) == []
    assert update.text == "done"


@pytest.mark.parametrize("cached_tokens", [64, 0])
def test_top_level_cached_tokens_is_the_cache_read_count(cached_tokens: int) -> None:
    usage = CompletionUsage.model_validate(_kimi_usage(cached_tokens=cached_tokens))

    details = decode_usage(usage, variant=OPENAI)

    assert details["prompt/cached_tokens"] == cached_tokens
    assert details["cache_read_input_token_count"] == cached_tokens


def test_prompt_tokens_details_cached_tokens_win_over_the_top_level_field() -> None:
    usage = CompletionUsage.model_validate(_kimi_usage(cached_tokens=64, prompt_tokens_details={"cached_tokens": 3}))

    details = decode_usage(usage, variant=OPENAI)

    assert details["prompt/cached_tokens"] == 3
    assert details["cache_read_input_token_count"] == 3


@pytest.mark.parametrize("cached_tokens", ["64", True, None])
def test_non_integer_top_level_cached_tokens_is_not_reported(cached_tokens: Any) -> None:
    usage = CompletionUsage.model_validate(_kimi_usage(cached_tokens=cached_tokens))

    details = decode_usage(usage, variant=OPENAI)

    assert "prompt/cached_tokens" not in details
    assert "cache_read_input_token_count" not in details


def test_deepseek_cache_usage_is_unchanged() -> None:
    usage = CompletionUsage.model_validate(
        _kimi_usage(
            prompt_cache_hit_tokens=40,
            prompt_cache_miss_tokens=60,
            prompt_tokens_details={"cached_tokens": 40},
        )
    )

    assert decode_usage(usage, variant=DEEPSEEK) == {
        "input_token_count": 100,
        "output_token_count": 20,
        "total_token_count": 120,
        "prompt/cached_tokens": 40,
        "cache_read_input_token_count": 40,
        "deepseek.prompt_cache_hit_tokens": 40,
    }


@pytest.mark.parametrize("variant", [OPENAI, GLM], ids=["openai", "glm"])
def test_only_deepseek_reports_prompt_cache_hits(variant: ChatCompletionsVariant) -> None:
    usage = CompletionUsage.model_validate(_kimi_usage(prompt_cache_hit_tokens=40))

    assert "deepseek.prompt_cache_hit_tokens" not in decode_usage(usage, variant=variant)


@pytest.mark.parametrize("group", ["prompt_tokens_details", "completion_tokens_details"])
async def test_a_usage_breakdown_of_the_wrong_shape_fails_the_completion(group: str) -> None:
    """The SDK keeps a non-object breakdown as is; decoding it fails the call, never drops it silently."""
    body = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12, group: "unavailable"},
    }

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(body).encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http_client:
        client = ChatCompletionsClient(
            model="gpt-test",
            sdk_client=AsyncOpenAI(api_key="sk-test", base_url="https://api.test/v1", http_client=http_client),
        )
        with pytest.raises(ChatClientException) as exc_info:
            await client.get_response([_user()], options={})

    assert isinstance(exc_info.value.__cause__, AttributeError)


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
def test_a_request_that_cannot_be_built_fails_the_call_itself(stream: bool) -> None:
    # Raised by the call, before anything is awaited or iterated.
    with pytest.raises(ChatClientInvalidRequestException, match="only n=1"):
        _client().get_response([_user()], stream=stream, options={"n": 2})


@pytest.mark.parametrize(
    "trailing_frames",
    [
        pytest.param([], id="choice-level-only"),
        # Kimi K3 (api.kimi.com) also honors ``include_usage``: after the finish
        # chunk it sends a ``choices: []`` chunk repeating the same usage.
        pytest.param([{"choices": [], "usage": _kimi_usage(cached_tokens=64)}], id="then-top-level-repeat"),
    ],
)
async def test_kimi_stream_usage_reaches_the_final_response(trailing_frames: list[dict[str, Any]]) -> None:
    """The SDK keeps the choice-level ``usage`` in ``model_extra``; the client stack reports it once."""
    frames = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop", "usage": _kimi_usage(cached_tokens=64)}]},
        *trailing_frames,
    ]
    body = "".join(
        f"data: {json.dumps({'id': 'kimi-1', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'kimi-k2', **frame})}\n\n"
        for frame in frames
    )

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=(body + "data: [DONE]\n\n").encode()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http_client:
        client: Any = assemble_openai_stack(
            ChatCompletionsClient,
            AsyncOpenAI(api_key="sk-test", base_url="https://kimi.test/v1", http_client=http_client),
            model_id="kimi-k2",
        )
        stream = client.get_response([Message("user", ["hi"])], stream=True, options={})
        updates = [update async for update in stream]
        response = await stream.get_final_response()

    # Each usage-carrying chunk is streamed as its own snapshot of the one call.
    assert sum(len(_usage_contents(update)) for update in updates) == 1 + len(trailing_frames)
    assert response.text == "Hi"
    assert response.usage_details is not None
    assert response.usage_details["input_token_count"] == 100
    assert response.usage_details["output_token_count"] == 20
    assert response.usage_details["total_token_count"] == 120
    assert response.usage_details["cache_read_input_token_count"] == 64


def _completion_choice(*, content: Any, refusal: Any = None) -> Choice:
    return Choice.model_construct(
        index=0,
        finish_reason="stop",
        logprobs=None,
        message=ChatCompletionMessage.model_construct(role="assistant", content=content, refusal=refusal),
    )


def _chunk_choice(*, content: Any = None, reasoning_content: Any = None, refusal: Any = None) -> ChunkChoice:
    return ChunkChoice.model_construct(
        index=0,
        finish_reason=None,
        logprobs=None,
        delta=ChoiceDelta.model_construct(
            role="assistant",
            content=content,
            refusal=refusal,
            reasoning_content=reasoning_content,
        ),
    )


def test_non_string_blocking_content_is_not_stored_as_text() -> None:
    response = ChatCompletion.model_construct(
        id="resp-1",
        choices=[_completion_choice(content=[{"type": "text", "text": "hi"}])],
        created=1,
        model="gateway-model",
        object="chat.completion",
    )

    parsed = decode_completion(response, {}, variant=OPENAI)

    assert parsed.messages[0].contents == []
    assert parsed.messages[0].text == ""


def test_non_string_stream_content_is_not_stored_as_text() -> None:
    chunk = ChatCompletionChunk.model_construct(
        id="chunk-1",
        choices=[_chunk_choice(content=[{"type": "text", "text": "hi"}])],
        created=1,
        model="gateway-model",
        object="chat.completion.chunk",
        usage=None,
    )

    (parsed,) = parse_stream_chunks(_client(), chunk)

    assert parsed.contents == []
    assert parsed.text == ""


def test_non_string_stream_content_does_not_hide_string_refusal() -> None:
    choice = _chunk_choice(content=[{"type": "thinking", "text": "private"}], refusal="blocked")

    assert [content.text for content in text_contents(choice)] == ["blocked"]


def test_non_string_stream_reasoning_delta_is_not_stored_as_reasoning_text() -> None:
    chunk = ChatCompletionChunk.model_construct(
        id="chunk-1",
        choices=[_chunk_choice(reasoning_content=[{"type": "thinking", "text": "private"}])],
        created=1,
        model="gateway-model",
        object="chat.completion.chunk",
        usage=None,
    )

    (update,) = parse_stream_chunks(_client(), chunk)
    response = ChatResponse.from_updates([update])

    assert update.contents == []
    assert response.raw_text == ""


def test_anthropic_redacted_reasoning_is_not_replayed_as_chat_completions_reasoning() -> None:
    redacted = Content.from_text_reasoning(
        protected_data='{"private":"opaque"}',
        additional_properties={"anthropic_redacted_thinking": True},
    )

    prepared = encode_message(Message("assistant", [redacted, Content.from_text("answer")]), variant=OPENAI)

    assert prepared == [{"role": "assistant", "content": "answer"}]


# ── a decoded completion without a choices array ──


def test_ensure_choices_passes_with_populated_choices() -> None:
    resp = SimpleNamespace(choices=[SimpleNamespace()])
    ensure_choices(resp)


def test_ensure_choices_passes_with_empty_list() -> None:
    """Empty choices is a valid (no-completion) response and must not raise."""
    resp = SimpleNamespace(choices=[])
    ensure_choices(resp)


def _invalid_response_text(exc_info: pytest.ExceptionInfo[ChatClientException]) -> str:
    """The raw boundary's shape: the typed invalid-response error is the cause, and carries the text."""
    cause = exc_info.value.__cause__
    assert isinstance(cause, ChatClientInvalidResponseException)
    return str(cause)


def test_ensure_choices_raises_when_none_and_includes_body() -> None:
    body = '{"error": "rate limit exceeded"}'
    resp = SimpleNamespace(choices=None, model_dump_json=lambda: body)
    with pytest.raises(ChatClientException) as exc_info:
        ensure_choices(resp)
    msg = _invalid_response_text(exc_info)
    assert "missing the required 'choices' array" in msg
    assert "rate limit exceeded" in msg


def test_ensure_choices_raises_when_not_a_list() -> None:
    """Non-strict SDK construction can leave a str/dict in ``choices``; reject it too."""
    body = '{"choices": "oops"}'
    resp = SimpleNamespace(choices="oops", model_dump_json=lambda: body)
    with pytest.raises(ChatClientException) as exc_info:
        ensure_choices(resp)
    msg = _invalid_response_text(exc_info)
    assert "'choices' is str; expected an array" in msg
    assert body in msg


def test_ensure_choices_truncates_long_body() -> None:
    big = '{"error": "' + ("x" * 5000) + '"}'
    resp = SimpleNamespace(choices=None, model_dump_json=lambda: big)
    with pytest.raises(ChatClientException) as exc_info:
        ensure_choices(resp)
    assert "[truncated]" in _invalid_response_text(exc_info)


def test_ensure_choices_failure_speaks_for_the_model_service() -> None:
    """No route reaches this failure: the typed cause alone says the model service answered."""
    body = '{"error": {"message": "This model\'s maximum context length is 128000 tokens."}}'
    resp = SimpleNamespace(choices=None, model_dump_json=lambda: body)
    with pytest.raises(ChatClientException) as exc_info:
        ensure_choices(resp)

    description = describe_error(exc_info.value, route_probe=lambda: None)

    assert description is not None
    assert description.kind is ErrorKind.CONTEXT_OVERFLOW


def test_ensure_choices_falls_back_to_repr_on_dump_failure() -> None:
    def _bad_dump() -> str:
        raise RuntimeError("pydantic broken")

    resp = SimpleNamespace(choices=None, model_dump_json=_bad_dump)
    with pytest.raises(ChatClientException):
        ensure_choices(resp)
