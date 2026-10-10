# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Each provider's response headers yield that provider's own request id."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest

from chrys.service.llm.provider_request_ids import (
    PROVIDER_REQUEST_ID_HEADERS,
    Evidence,
    read_provider_request_id,
)

# The id-like headers each provider's response carries, and the one its request id is read from.
_PROVIDER_RESPONSES: dict[str, tuple[dict[str, str], str | None]] = {
    "OpenAI": ({"x-request-id": "openai", "cf-ray": "ray"}, "x-request-id"),
    "Together AI": ({"x-request-id": "together", "request-id": "together-dup"}, "x-request-id"),
    "Amazon Bedrock": ({"x-amzn-requestid": "amzn", "x-request-id": "bedrock"}, "x-request-id"),
    "Tencent Hunyuan": ({"x-request-id": "hunyuan", "x-trace-id": "trace"}, "x-request-id"),
    "Z.ai": ({"ga-traceid": "ga", "x-log-id": "log", "x-request-id": "zai"}, "x-request-id"),
    "vLLM": ({"X-Request-Id": "vllm"}, "x-request-id"),
    "Anthropic": ({"request-id": "anthropic", "traceresponse": "00-trace", "cf-ray": "ray"}, "request-id"),
    "OpenAI Codex backend": ({"x-oai-request-id": "codex", "cf-ray": "ray"}, "x-oai-request-id"),
    "Moonshot AI": ({"msh-request-id": "msh", "x-msh-trace-id": "trace", "msh-trace-mode": "on"}, "msh-request-id"),
    "MiniMax (international)": (
        {"trace-id": "trace", "minimax-request-id": "minimax", "x-mm-request-id": "mm", "alb_request_id": "alb"},
        "minimax-request-id",
    ),
    "Mistral AI": ({"mistral-correlation-id": "mistral", "x-kong-request-id": "kong"}, "mistral-correlation-id"),
    "OpenRouter error": ({"x-generation-id": "gen-openrouter", "cf-ray": "ray"}, "x-generation-id"),
    "new-api gateway": ({"x-oneapi-request-id": "oneapi", "x-new-api-version": "v"}, "x-oneapi-request-id"),
    # one-api copies its upstream's headers onto non-streamed replies.
    "one-api over OpenAI": ({"x-oneapi-request-id": "oneapi", "x-request-id": "upstream"}, "x-oneapi-request-id"),
    "one-api over DeepSeek": ({"x-oneapi-request-id": "oneapi", "x-ds-trace-id": "upstream"}, "x-oneapi-request-id"),
    "LiteLLM proxy": ({"x-litellm-call-id": "litellm", "x-litellm-model-id": "model"}, "x-litellm-call-id"),
    "DeepSeek": ({"x-ds-trace-id": "deepseek", "eo-log-uuid": "edge"}, "x-ds-trace-id"),
    "Kimi": ({"x-trace-id": "kimi", "x-internal-adhoc-canary": "1"}, "x-trace-id"),
    "Zhipu AI": ({"ga-traceid": "ga", "x-log-id": "zhipu"}, "x-log-id"),
    "MiniMax (China)": ({"trace-id": "minimaxi", "alb_request_id": "alb"}, "trace-id"),
    "xAI error": ({"cf-ray": "ray"}, None),
    "SiliconFlow error": ({}, None),
}


@pytest.mark.parametrize("provider", list(_PROVIDER_RESPONSES))
def test_each_provider_response_yields_its_request_id(provider: str) -> None:
    headers, source = _PROVIDER_RESPONSES[provider]
    expected = None if source is None else httpx.Headers(headers)[source]
    assert read_provider_request_id(httpx.Headers(headers)) == expected


def test_every_table_header_is_some_providers_request_id() -> None:
    table = [header.name for header in PROVIDER_REQUEST_ID_HEADERS]
    assert len(table) == len(set(table))
    assert set(table) == {source for _, source in _PROVIDER_RESPONSES.values() if source is not None}


def test_every_sender_names_where_its_header_was_confirmed() -> None:
    for header in PROVIDER_REQUEST_ID_HEADERS:
        assert header.name == header.name.lower()
        assert header.senders, header.name
        for sender in header.senders:
            if sender.evidence in {Evidence.DOCS, Evidence.SOURCE, Evidence.CLIENT_SOURCE}:
                url = urlsplit(sender.reference)
                assert (url.scheme, bool(url.netloc), bool(url.path)) == ("https", True, True), sender
            else:
                assert "." in sender.reference, sender
                assert not {"/", ":", " "} & set(sender.reference), sender


def test_request_id_is_printable_and_bounded() -> None:
    assert read_provider_request_id(httpx.Headers({"x-request-id": "a\tb\x7fc" + "x" * 300})) == "abc" + "x" * 253


def test_an_unprintable_request_id_falls_through_to_the_next_header() -> None:
    headers = httpx.Headers({"x-request-id": "\t\x7f", "x-ds-trace-id": "deepseek"})
    assert read_provider_request_id(headers) == "deepseek"
