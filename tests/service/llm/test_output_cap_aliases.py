# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The three output-cap spellings, sent under the wire name each protocol line uses."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.resources.messages import AsyncMessages
from openai.resources.chat.completions import AsyncCompletions
from openai.resources.responses import AsyncResponses

from chrys.kernel import CompactionCallContext, Message
from chrys.kernel.client import OUTPUT_CAP_OPTION_ALIASES
from chrys.service.llm.anthropic_messages import AnthropicMessagesClient
from chrys.service.llm.chat_completions import (
    ChatCompletionsClient,
    DeepSeekChatCompletionsClient,
    GlmChatCompletionsClient,
)
from chrys.service.llm.openai_responses import DeepSeekResponsesApiClient, ResponsesApiClient

# (line id, client type, the wire name of its output cap, its SDK ``create``).
# The SDK class is what a built request must bind to: an unknown keyword there
# is the ``TypeError`` a spelling that was never renamed used to cause.
_LINES = (
    ("openai-chat", ChatCompletionsClient, "max_completion_tokens", AsyncCompletions),
    ("deepseek-chat", DeepSeekChatCompletionsClient, "max_tokens", AsyncCompletions),
    ("glm-chat", GlmChatCompletionsClient, "max_tokens", AsyncCompletions),
    ("openai-responses", ResponsesApiClient, "max_output_tokens", AsyncResponses),
    ("deepseek-responses", DeepSeekResponsesApiClient, "max_output_tokens", AsyncResponses),
    ("anthropic-messages", AnthropicMessagesClient, "max_tokens", AsyncMessages),
)


def _messages() -> list[Message]:
    return [Message("user", ["hi"])]


def _sdk() -> SimpleNamespace:
    """The SDK client attributes a request build reads; no call is ever made."""
    return SimpleNamespace(base_url="https://api.test", default_headers={})


def _build(client_type: Any, options: dict[str, Any]) -> dict[str, Any]:
    client = client_type(model="model-test", sdk_client=_sdk())
    if client_type is AnthropicMessagesClient:
        return client._build_request(_messages(), options, {})
    return client._build_request(_messages(), options)


def _offsets(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key in OUTPUT_CAP_OPTION_ALIASES}


@pytest.mark.parametrize("spelling", OUTPUT_CAP_OPTION_ALIASES)
@pytest.mark.parametrize(("line", "client_type", "wire_name", "sdk_resource"), _LINES, ids=[row[0] for row in _LINES])
def test_every_output_cap_spelling_reaches_the_lines_wire_name(
    line: str, client_type: Any, wire_name: str, sdk_resource: Any, spelling: str
) -> None:
    request = _build(client_type, {spelling: 4096})

    assert _offsets(request) == {wire_name: 4096}
    # The only keys allowed are the SDK's own: binding refuses an unknown one.
    inspect.signature(sdk_resource.create).bind(None, **request)


def test_the_canonical_spelling_wins_when_several_are_supplied() -> None:
    options = {"max_tokens": 111, "max_output_tokens": 222, "max_completion_tokens": 333}

    for line, client_type, wire_name, _sdk_resource in _LINES:
        request = _build(client_type, options)
        assert _offsets(request) == {wire_name: 111}, line


@pytest.mark.parametrize("spelling", ("max_output_tokens", "max_completion_tokens"))
def test_anthropic_uses_a_non_canonical_cap_instead_of_its_fallback(spelling: str) -> None:
    request = _build(AnthropicMessagesClient, {spelling: 9000})

    assert request["max_tokens"] == 9000


class _AdmissionStrategy:
    def __init__(self, *, last_included_tokens: int, max_context_tokens: int = 100) -> None:
        self.last_included_tokens = last_included_tokens
        self.max_context_tokens = max_context_tokens
        self.system_overhead_tokens = 0
        self.calibration_ratio = 1.0

    async def __call__(self, _messages: list[Message], _context: CompactionCallContext | None = None) -> bool:
        return False


@pytest.mark.asyncio
async def test_an_admitted_non_canonical_cap_still_reaches_the_wire_name() -> None:
    client = ChatCompletionsClient(model="gpt-test", sdk_client=_sdk())

    _prepared, admitted = await client._prepare_wire_call(
        _messages(),
        call_context=CompactionCallContext(),
        options_snapshot={"max_output_tokens": 4096},
        sanitized_client_kwargs={},
        compaction_overrides={"compaction_strategy": _AdmissionStrategy(last_included_tokens=99)},
    )

    assert _offsets(client._build_request(_messages(), admitted)) == {"max_completion_tokens": 1}
