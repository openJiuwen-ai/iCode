# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Context-window overflow: explicit provider evidence only."""

from __future__ import annotations

from typing import NoReturn

import httpx
import pytest

from chrys.foundation.errors import (
    ErrorKind,
    classify_error,
    context_overflow_limit,
    is_context_overflow,
    is_thinking_binding_rejection,
    may_be_context_overflow,
)
from chrys.service.agent_middleware.response_validation import TerminalResponseValidationError
from chrys.service.agent_middleware.validators import OUTPUT_TRUNCATED_REASON
from tests.support.provider_errors import anthropic_thinking_binding_rejection, openai_status, raised_while_handling

# Provider phrasings, after pi-mono's and opencode's overflow patterns.
_OVERFLOW_MESSAGES = [
    "prompt is too long: 213462 tokens > 200000 maximum",
    "input is too long for requested model",
    "This model's maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens.",
    "Your input exceeds the context window of this model. Please adjust your input and try again.",
    "The input token count (1196265) exceeds the maximum number of tokens allowed (1048575).",
    "This model's maximum prompt length is 131072 but the request contains 537812 tokens.",
    "Please reduce the length of the messages or completion.",
    "the request exceeds the available context size, try increasing it",
    "The number of tokens to keep from the initial prompt is greater than the context length",
    "invalid params, context window exceeds limit",
    "Your request exceeded model token limit: 262144 (requested: 291351)",
    "Prompt too long",
    "Prompt exceeds max length",
    "prompt too long; exceeded max context length by 100 tokens",
    "model_context_window_exceeded",
    "Input length 140000 exceeds the maximum allowed input length of 131072 tokens.",
    "The input (9000 tokens) is longer than the model's context length (8192 tokens).",
    "This model's context length is only 8192 tokens, but 9534 tokens were requested.",
    "prompt token count of 135211 exceeds the limit of 128000",
    "Prompt has 5,958,968 tokens, but the configured context size is 256,000 tokens",
    "<400> InternalError.Algo.InvalidParameter: Range of input length should be [1, 129024]",
    "tokens in request more than max tokens allowed",
]

_NOT_OVERFLOW = [
    (400, "Too many tokens, please wait before trying again."),
    (429, "Too many tokens, please wait before trying again."),
    (429, "Rate limit reached for gpt-4o on tokens per min (TPM): Limit 30000, Requested 50000."),
    (400, "Throttling: too many tokens in flight"),
    (400, "Invalid 'messages[0].content': string too long."),
    (503, "maximum context length is temporarily reduced"),
    (429, "Too many tokens per minute. Retry shortly."),
    (400, "Too many tokens: this key's daily quota is used up."),
    # Only the token count of a prompt exceeding a limit names the window.
    (400, "The number of images exceeds the limit of 20."),
    (400, "Token limit exceeded for the output."),
]
# Text a plain exception carries with no provider signal: the text alone must veto overflow.
_NOT_OVERFLOW_TEXT = [
    "Too many tokens, please wait before trying again.",
    "Throttling: too many tokens in flight",
    "Too many tokens per minute. Retry shortly.",
    "Too many tokens: this key's daily quota is used up.",
    "Rate limit reached: context window tokens per day exhausted.",
]
# (status, code, message, may_be_context_overflow): none is an overflow for retry policy.
_SERVER_ERRORS = [
    (
        500,
        None,
        "This model's maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens.",
        True,
    ),
    (503, None, "maximum context length is temporarily reduced", True),
    # Every 5xx, not only the statuses the legacy retry table lists.
    (507, None, "maximum context length is temporarily reduced", True),
    (520, None, "maximum context length is temporarily reduced", True),
    (524, None, "maximum context length is temporarily reduced", True),
    (502, None, "Upstream error: prompt is too long: 213462 tokens > 200000 maximum", True),
    (500, None, "Too many tokens, please wait before trying again.", False),
    (500, None, "Internal server error.", False),
    # The code outranks wording that alone reads as throttling.
    (500, "context_length_exceeded", "Upstream busy, please wait.", True),
    (500, "request_too_large", "This model's maximum context length is 128000 tokens.", False),
    (429, None, "This model's maximum context length is 128000 tokens.", False),
    (413, None, "This model's maximum context length is 128000 tokens.", False),
]


# (message, the window limit it names): the limit, never the request's size.
_NAMED_LIMITS = [
    ("prompt is too long: 213462 tokens > 200000 maximum", 200000),
    (
        (
            "This model's maximum context length is 131072 tokens. However, you requested 131074 tokens "
            "(99074 in the messages, 32000 in the completion)."
        ),
        131072,
    ),
    ("The input token count (1196265) exceeds the maximum number of tokens allowed (1048575).", 1048575),
    ("This model's maximum prompt length is 131072 but the request contains 537812 tokens.", 131072),
    ("request (5000 tokens) exceeds the available context size (4096 tokens), try increasing it", 4096),
    ("the number of tokens to keep from the initial prompt is greater than the context length (n_ctx: 4096)", 4096),
    ("Your request exceeded model token limit: 262144 (requested: 291351)", 262144),
    ("Requested token count exceeds the model's maximum context length of 131072 tokens", 131072),
    ("Input length (265330) exceeds model's maximum context length (262144).", 262144),
    (
        "Prompt contains 140000 tokens and 0 draft tokens, too large for model with 131072 maximum context length",
        131072,
    ),
    ("Input length 140000 exceeds the maximum allowed input length of 131,072 tokens.", 131072),
    ("The input (9000 tokens) is longer than the model's context length (8192 tokens).", 8192),
    ("This model's context length is only 8192 tokens, but 9534 tokens were requested.", 8192),
    ("prompt token count of 135211 exceeds the limit of 128000", 128000),
    ("Prompt has 5,958,968 tokens, but the configured context size is 256,000 tokens", 256000),
    ("<400> InternalError.Algo.InvalidParameter: Range of input length should be [1, 129024]", 129024),
    ("prompt too long; exceeded max context length by 100 tokens", None),
    ("This model's maximum context length is 128,000 tokens.", 128000),
    ("This model's maximum context length is 128k tokens.", None),
    ("This model's maximum context length is 0 tokens.", None),
    ("Your input exceeds the context window of this model.", None),
    ("maximum context length is 128000 tokens; maximum context length is 64000 tokens", None),
]


def _error_body(message: str, code: str | None = None) -> dict[str, object]:
    return {"error": {"type": "invalid_request_error", "code": code, "message": message}}


@pytest.mark.parametrize("message", _OVERFLOW_MESSAGES)
async def test_provider_overflow_phrasings_are_overflow(message: str) -> None:
    exc = await openai_status(400, _error_body(message))

    assert is_context_overflow(exc) is True
    assert may_be_context_overflow(exc) is True
    assert classify_error(exc).retryable is False


@pytest.mark.parametrize("message", _OVERFLOW_MESSAGES)
def test_plain_exception_carrying_provider_text_is_overflow(message: str) -> None:
    assert is_context_overflow(RuntimeError(f"400: {message}")) is True


@pytest.mark.parametrize("message", _NOT_OVERFLOW_TEXT)
def test_plain_exception_carrying_throttling_text_is_not_overflow(message: str) -> None:
    assert is_context_overflow(RuntimeError(f"503: {message}")) is False
    assert may_be_context_overflow(RuntimeError(f"503: {message}")) is False


@pytest.mark.parametrize(("status", "message"), _NOT_OVERFLOW)
async def test_throttling_and_unrelated_rejections_are_not_overflow(status: int, message: str) -> None:
    exc = await openai_status(status, _error_body(message))

    assert is_context_overflow(exc) is False


@pytest.mark.parametrize(("status", "code", "message", "may_be"), _SERVER_ERRORS)
async def test_only_a_caller_that_shrinks_reads_a_server_error_naming_the_context_window(
    status: int, code: str | None, message: str, may_be: bool
) -> None:
    exc = await openai_status(status, _error_body(message, code))

    assert is_context_overflow(exc) is False
    # An oversized payload is final; every other one stays a retry for the main turn.
    assert classify_error(exc).retryable is (status != 413 and code != "request_too_large")
    assert may_be_context_overflow(exc) is may_be


@pytest.mark.parametrize("code", ["context_length_exceeded", "model_max_prompt_tokens_exceeded"])
async def test_overflow_code_counts_without_a_known_phrase(code: str) -> None:
    exc = await openai_status(400, _error_body("Request rejected.", code=code))

    assert is_context_overflow(exc) is True


async def test_request_too_large_is_a_payload_problem_not_overflow() -> None:
    exc = await openai_status(
        413, _error_body("Request too large: maximum context length is 32 MB", "request_too_large")
    )

    result = classify_error(exc)
    assert is_context_overflow(exc) is False
    assert result.kind is ErrorKind.PAYLOAD_TOO_LARGE
    assert result.retryable is False


async def test_overflow_text_only_in_implicit_context_is_not_overflow() -> None:
    stale = await openai_status(400, _error_body(_OVERFLOW_MESSAGES[0]))

    def raise_fresh() -> NoReturn:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    fresh = raised_while_handling(stale, raise_fresh)

    assert is_context_overflow(fresh) is False
    assert classify_error(fresh).retryable is True


def test_chrys_validation_verdict_mentioning_the_context_window_is_not_overflow() -> None:
    exc = TerminalResponseValidationError(OUTPUT_TRUNCATED_REASON)

    assert "context window" in str(exc)
    assert is_context_overflow(exc) is False
    assert classify_error(exc).kind is ErrorKind.INVALID_RESPONSE


@pytest.mark.parametrize(("message", "limit"), _NAMED_LIMITS)
async def test_an_overflow_names_the_servers_window_limit(message: str, limit: int | None) -> None:
    exc = await openai_status(400, _error_body(message))

    assert is_context_overflow(exc) is True
    assert context_overflow_limit(exc) == limit


async def test_only_an_overflow_names_a_limit() -> None:
    server_error = await openai_status(500, _error_body("This model's maximum context length is 128000 tokens."))

    assert context_overflow_limit(server_error) is None
    assert context_overflow_limit(RuntimeError("400: This model's maximum context length is 128000 tokens.")) == 128000


async def test_a_thinking_binding_refusal_naming_the_context_window_is_both() -> None:
    """The text alone makes it an overflow; the loop checks the binding refusal before acting on that."""
    exc = await anthropic_thinking_binding_rejection(names_the_window=True)

    assert is_context_overflow(exc) is True
    assert is_thinking_binding_rejection(exc) is True
