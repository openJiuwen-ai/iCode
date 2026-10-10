# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Which response header carries each provider's own request id, as data.

Request tracking records the id from the first header in
``PROVIDER_REQUEST_ID_HEADERS`` that a response carries as its
``provider_request_id``, so the table's order is the precedence. Gateway ids
come first: the gateway is the service the client called, and one-api copies
its upstream's headers onto its replies. Request-id names come next, then trace
and log ids, which Z.ai, Tencent Hunyuan and MiniMax send beside a request id.

Each header lists the senders whose id is read from it and how that was
confirmed; a sender that also sends an earlier header is listed under that one
only. To support another provider, add it as a sender of the header its id is
read from, or add its header as a row at its place in the precedence, and add
its response headers to the provider cases in this module's test.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import httpx

_MAX_REQUEST_ID_CHARS: Final = 256


class Evidence(StrEnum):
    """How a sender's header was confirmed. Responses were observed in October 2026."""

    RESPONSE = "response"
    """Seen on a successful model response; the reference is the endpoint host."""
    ERROR_RESPONSE = "error_response"
    """Seen on an error response, such as a 401 without a key; the reference is the endpoint host."""
    DOCS = "docs"
    """Named in the sender's documentation; the reference is the page."""
    SOURCE = "source"
    """Set by the sender's source code; the reference is the file."""
    CLIENT_SOURCE = "client_source"
    """Read from the sender's responses by a client's source code; the reference is the file."""


@dataclass(frozen=True, slots=True)
class RequestIdSender:
    """A provider, inference server or gateway whose request id one header carries."""

    name: str
    evidence: Evidence
    reference: str
    """Endpoint host for an observed response, otherwise the page or file URL."""
    note: str = ""


@dataclass(frozen=True, slots=True)
class RequestIdHeader:
    """A response header that carries a request id, and who sends it."""

    name: str
    """Lowercase header name."""
    senders: tuple[RequestIdSender, ...]


PROVIDER_REQUEST_ID_HEADERS: Final[tuple[RequestIdHeader, ...]] = (
    # Gateway ids.
    RequestIdHeader(
        "x-generation-id",
        (
            RequestIdSender(
                "OpenRouter",
                Evidence.DOCS,
                "https://openrouter.ai/docs/guides/features/router-metadata",
                note="on error responses; a successful response's id is its body's gen- id",
            ),
        ),
    ),
    RequestIdHeader(
        "x-oneapi-request-id",
        (
            RequestIdSender(
                "one-api",
                Evidence.SOURCE,
                "https://github.com/songquanpeng/one-api/blob/8df4a2670b98266bd287c698243fff327d9748cf/middleware/request-id.go",
            ),
            RequestIdSender(
                "new-api",
                Evidence.SOURCE,
                "https://github.com/QuantumNous/new-api/blob/c6741c36a9a1a729554ad94ee99f21602a214a23/middleware/request-id.go",
                note="also seen on a successful response from a hosted new-api gateway",
            ),
        ),
    ),
    RequestIdHeader(
        "x-litellm-call-id",
        (RequestIdSender("LiteLLM proxy", Evidence.DOCS, "https://docs.litellm.ai/docs/proxy/response_headers"),),
    ),
    # Request ids.
    RequestIdHeader(
        "x-request-id",
        (
            RequestIdSender("OpenAI", Evidence.RESPONSE, "api.openai.com"),
            RequestIdSender("Groq", Evidence.ERROR_RESPONSE, "api.groq.com"),
            RequestIdSender("Together AI", Evidence.ERROR_RESPONSE, "api.together.xyz", note="also sends request-id"),
            RequestIdSender("Fireworks AI", Evidence.ERROR_RESPONSE, "api.fireworks.ai"),
            RequestIdSender(
                "Amazon Bedrock",
                Evidence.ERROR_RESPONSE,
                "bedrock-runtime.us-east-1.amazonaws.com",
                note="OpenAI-compatible endpoint; also sends x-amzn-requestid",
            ),
            RequestIdSender("Alibaba Cloud DashScope", Evidence.ERROR_RESPONSE, "dashscope.aliyuncs.com"),
            RequestIdSender("Volcengine Ark", Evidence.ERROR_RESPONSE, "ark.cn-beijing.volces.com"),
            RequestIdSender("Baidu Qianfan", Evidence.ERROR_RESPONSE, "qianfan.baidubce.com"),
            RequestIdSender(
                "Tencent Hunyuan",
                Evidence.ERROR_RESPONSE,
                "api.hunyuan.cloud.tencent.com",
                note="also sends x-trace-id",
            ),
            RequestIdSender("Z.ai", Evidence.ERROR_RESPONSE, "api.z.ai", note="also sends x-log-id"),
            RequestIdSender(
                "vLLM",
                Evidence.SOURCE,
                "https://github.com/vllm-project/vllm/blob/c41b2639e29c3bc01add1d34bef3032a6d9d8aca/vllm/entrypoints/serve/middleware/x_request_id.py",
                note="only when started with --enable-request-id-headers",
            ),
            RequestIdSender(
                "SGLang model gateway",
                Evidence.SOURCE,
                "https://github.com/sgl-project/sglang/blob/6fc8d9da3288e9710a6f9a1f59503cacf8a984f0/sgl-model-gateway/src/middleware.rs",
                note="an SGLang server without the gateway sends none",
            ),
        ),
    ),
    RequestIdHeader(
        "request-id",
        (RequestIdSender("Anthropic", Evidence.RESPONSE, "api.anthropic.com"),),
    ),
    RequestIdHeader(
        "x-oai-request-id",
        (
            RequestIdSender(
                "OpenAI Codex backend",
                Evidence.CLIENT_SOURCE,
                "https://github.com/openai/codex/blob/806d9732c974bc8a51b8317c1bd8985544fe627c/codex-rs/response-debug-context/src/lib.rs",
                note="the Codex CLI reads it when x-request-id is absent",
            ),
        ),
    ),
    RequestIdHeader(
        "msh-request-id",
        (
            RequestIdSender("Moonshot AI (China)", Evidence.ERROR_RESPONSE, "api.moonshot.cn"),
            RequestIdSender(
                "Moonshot AI (international)",
                Evidence.ERROR_RESPONSE,
                "api.moonshot.ai",
                note="also sends x-msh-trace-id",
            ),
        ),
    ),
    RequestIdHeader(
        "minimax-request-id",
        (
            RequestIdSender(
                "MiniMax (international)",
                Evidence.RESPONSE,
                "api.minimax.io",
                note="also sends trace-id and x-mm-request-id",
            ),
        ),
    ),
    RequestIdHeader(
        "mistral-correlation-id",
        (
            RequestIdSender(
                "Mistral AI", Evidence.ERROR_RESPONSE, "api.mistral.ai", note="x-kong-request-id carries the same value"
            ),
        ),
    ),
    # Trace and log ids.
    RequestIdHeader(
        "x-ds-trace-id",
        (RequestIdSender("DeepSeek", Evidence.RESPONSE, "api.deepseek.com"),),
    ),
    RequestIdHeader(
        "x-trace-id",
        (RequestIdSender("Kimi", Evidence.RESPONSE, "api.kimi.com", note="both its OpenAI and Anthropic endpoints"),),
    ),
    RequestIdHeader(
        "x-log-id",
        (RequestIdSender("Zhipu AI", Evidence.RESPONSE, "open.bigmodel.cn"),),
    ),
    RequestIdHeader(
        "trace-id",
        (RequestIdSender("MiniMax (China)", Evidence.ERROR_RESPONSE, "api.minimaxi.com"),),
    ),
)

_HEADER_NAMES: Final = tuple(header.name for header in PROVIDER_REQUEST_ID_HEADERS)


def read_provider_request_id(headers: httpx.Headers) -> str | None:
    """Return the first non-empty request id in table order, printable and bounded."""
    for name in _HEADER_NAMES:
        value = headers.get(name)
        if value:
            cleaned = "".join(ch for ch in value if ch.isprintable())[:_MAX_REQUEST_ID_CHARS]
            if cleaned:
                return cleaned
    return None
