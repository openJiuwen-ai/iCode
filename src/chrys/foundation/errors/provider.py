# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Provider signals: what the model service itself said about a failure.

A signal is read from ONE explicit node — the first SDK-shaped exception in
walk order — so its status, code, type and message never combine evidence
from different nodes.  An SDK-shaped node is a :class:`ProviderResponseError`,
a node with an integer ``status_code``, or a node with both a ``body`` and a
``code`` attribute (the bare ``openai.APIError`` an in-band stream error
raises).  ``code`` alone doesn't count: ``SystemExit.code`` would match.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ._provider_payload import (
    CONTENT_FILTER_CODES,
    GATEWAY_QUOTA_CODES,
    ProviderErrorPayload,
    ProviderVerdict,
    gateway_verdict,
)
from ._text import _clean_exception_text
from .kinds import ErrorKind

# Account quota or billing exhaustion: retrying the same request cannot
# succeed until the account changes.  Broad types such as
# ``invalid_request_error`` are deliberately absent.
NON_RETRYABLE_PROVIDER_ERROR_CODES = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "exceeded_current_quota_error",
        "usage_not_included",
        # OpenAI: prepaid credits spent, or a spend or usage limit an
        # organization or project set; ChatGPT plan usage windows.
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "usage_limit_reached",
        "subscription_sharing_usage_limit_exceeded",
        # LiteLLM proxy: the key's, user's or team's budget is spent.
        "budget_exceeded",
        *GATEWAY_QUOTA_CODES,
    }
)
# Types some services also send as the error's code: under an error status
# such a code ranks as the type does, below the status.
_BROAD_TYPE_CODES = frozenset({"invalid_request_error"})
# Zhipu GLM's balance-exhausted code; it means quota only on a 429.
_GLM_QUOTA_CODE = "1113"
_PAYMENT_REQUIRED = 402
# Anthropic answers spent prepaid credit with a plain 400
# ``invalid_request_error``: only its text tells it apart.
_CREDIT_EXHAUSTED_PHRASE = "credit balance is too low"

_PROVIDER_CODE_KINDS: Mapping[str, ErrorKind] = {
    "rate_limit_exceeded": ErrorKind.RATE_LIMITED,
    "rate_limit_error": ErrorKind.RATE_LIMITED,
    "overloaded_error": ErrorKind.OVERLOADED,
    "server_is_overloaded": ErrorKind.OVERLOADED,
    "slow_down": ErrorKind.OVERLOADED,
    "api_error": ErrorKind.SERVER_ERROR,
    "server_error": ErrorKind.SERVER_ERROR,
    "request_too_large": ErrorKind.PAYLOAD_TOO_LARGE,
    "authentication_error": ErrorKind.AUTH_FAILED,
    "permission_error": ErrorKind.AUTH_FAILED,
    "invalid_request_error": ErrorKind.REQUEST_REJECTED,
    "not_found_error": ErrorKind.REQUEST_REJECTED,
    **dict.fromkeys(CONTENT_FILTER_CODES, ErrorKind.CONTENT_FILTERED),
    "billing_error": ErrorKind.QUOTA_EXHAUSTED,
    "stream_truncated": ErrorKind.STREAM_TRUNCATED,
}
# Codes only a response's own failure reports (``ProviderResponseError``, or
# an error event inside its stream): an HTTP error that carries one keeps the
# kind its status gives it.
_IN_BAND_CODE_KINDS: Mapping[str, ErrorKind] = {
    **_PROVIDER_CODE_KINDS,
    # Finish reasons a Chat Completions service (GLM, DeepSeek) ends a
    # completion with when it failed to finish it.
    "network_error": ErrorKind.STREAM_TRUNCATED,
    "insufficient_system_resource": ErrorKind.OVERLOADED,
    # Codes a failed Responses API response reports.
    "invalid_prompt": ErrorKind.REQUEST_REJECTED,
    # A gateway's error body under HTTP 200 may name a bad key by its code alone.
    "invalid_api_key": ErrorKind.AUTH_FAILED,
    "cyber_policy": ErrorKind.CONTENT_FILTERED,
    "bio_policy": ErrorKind.CONTENT_FILTERED,
    "misalignment_policy_violation": ErrorKind.CONTENT_FILTERED,
    "image_content_policy_violation": ErrorKind.CONTENT_FILTERED,
    **dict.fromkeys(
        (
            "invalid_image",
            "invalid_image_format",
            "invalid_base64_image",
            "invalid_image_url",
            "image_too_large",
            "image_too_small",
            "image_parse_error",
            "invalid_image_mode",
            "image_file_too_large",
            "unsupported_image_media_type",
            "empty_image_file",
            "failed_to_download_image",
            "image_file_not_found",
        ),
        ErrorKind.REQUEST_REJECTED,
    ),
}
# Kinds of a failure a response reported in-band that the same request meets
# again when sent anew.
_FINAL_IN_BAND_KINDS = frozenset(
    {
        ErrorKind.QUOTA_EXHAUSTED,
        ErrorKind.CONTEXT_OVERFLOW,
        ErrorKind.PAYLOAD_TOO_LARGE,
        ErrorKind.AUTH_FAILED,
        ErrorKind.REQUEST_REJECTED,
        ErrorKind.CONTENT_FILTERED,
    }
)

_CONTEXT_OVERFLOW_CODES = frozenset(
    {
        "context_length_exceeded",
        "model_context_window_exceeded",
        "model_max_prompt_tokens_exceeded",  # GitHub Copilot
    }
)
# Only phrasings that name the context window or the model's token limit.
_CONTEXT_OVERFLOW_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"context_length_exceeded",
        r"context window",  # OpenAI "exceeds the context window", MiniMax
        r"maximum context length",  # OpenAI, DeepSeek, OpenRouter
        r"prompt (?:is )?too long",  # Anthropic, z.ai (code 1261), Ollama
        r"prompt exceeds max length",  # z.ai China endpoint (code 1261)
        r"input is too long",  # Amazon Bedrock
        r"too many tokens",
        r"input token count.*exceeds the maximum",  # Google Gemini
        r"maximum prompt length is \d+",  # xAI
        r"reduce the length of the messages",  # Groq
        r"exceeds the available context size",  # llama.cpp
        r"greater than the context length",  # LM Studio
        r"exceeded model token limit",  # Kimi
        r"context_window_exceeded",  # z.ai's finish reason, relayed as error text
        r"maximum allowed input length",  # OpenRouter (Poolside)
        r"longer than the model'?s context length",  # Together AI
        r"context length is only \d+ tokens",  # vLLM
        r"prompt token count of [\d,]+ exceeds the limit of \d+",  # GitHub Copilot
        r"configured context size is [\d,]+ tokens",  # DS4 server
        r"range of input length should be",  # Alibaba DashScope (Qwen)
        r"tokens in request more than max tokens allowed",  # from opencode's list; service unnamed
    )
)
# The window limit, in tokens, that an overflow phrasing above names: a
# plain decimal, optionally with thousands commas; never a "128k" shorthand.
_LIMIT = r"(\d{1,3}(?:,\d{3})+|\d+)(?!\w|[.,]\d)"
_CONTEXT_LIMIT_PATTERNS = tuple(
    re.compile(pattern.replace("{limit}", _LIMIT), re.IGNORECASE)
    for pattern in (
        r"maximum context length is {limit} tokens",  # OpenAI, vLLM, DeepSeek, OpenRouter
        r"maximum context length of {limit} tokens",  # LiteLLM
        r"maximum context length \({limit}\)",  # OpenAI-compatible servers
        r"too large for model with {limit} maximum context length",  # Mistral
        r"maximum allowed input length of {limit} tokens",  # OpenRouter (Poolside)
        r"model'?s context length \({limit} tokens\)",  # Together AI
        r"context length is only {limit} tokens",  # vLLM
        r"prompt token count of [\d,]+ exceeds the limit of {limit}",  # GitHub Copilot
        r"configured context size is {limit} tokens",  # DS4 server
        r"range of input length should be \[\d+, ?{limit}\]",  # Alibaba DashScope (Qwen)
        r"prompt is too long: [\d,]+ tokens > {limit} maximum",  # Anthropic
        r"maximum number of tokens allowed \({limit}\)",  # Google Gemini
        r"maximum prompt length is {limit}",  # xAI
        r"available context size \({limit} tokens\)",  # llama.cpp
        r"n_ctx: {limit}",  # LM Studio
        r"exceeded model token limit: {limit}",  # Kimi
    )
)
# Throttling that happens to mention tokens ("Too many tokens, please wait",
# "too many tokens per minute", a daily token quota) is a rate limit, not an
# oversized request.  Without a provider signal the text is the only
# evidence, so any rate-window wording vetoes it.
_NON_OVERFLOW_PHRASES = (
    "throttl",
    "rate limit",
    "rate_limit",
    "too many requests",
    "please wait",
    "per sec",
    "per min",
    "per hour",
    "per day",
    "quota",
)
# 429 is throttling.  413 / ``request_too_large`` is an oversized payload (an
# image, an attachment), which compaction cannot fix.  Every 5xx is vetoed too
# (:func:`_is_server_error`): the server failed, and overflow wording there may
# name a temporary limit, so it stays a retry.
_NON_OVERFLOW_STATUS_CODES = frozenset({413, 429})
_NON_OVERFLOW_CODES = frozenset({"request_too_large"})

# Error types an Anthropic stream reports after its 200 that a retry of the
# same request cannot fix; every other type (overloaded, api, rate limit,
# timeout, unknown) is transient.
_NON_RETRYABLE_STREAM_ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "request_too_large",
        "billing_error",
    }
)


def _code_kind(code: str) -> ErrorKind:
    if code in _CONTEXT_OVERFLOW_CODES:
        return ErrorKind.CONTEXT_OVERFLOW
    if code in NON_RETRYABLE_PROVIDER_ERROR_CODES:
        return ErrorKind.QUOTA_EXHAUSTED
    return _IN_BAND_CODE_KINDS.get(code, ErrorKind.UNKNOWN)


def in_band_failure_retryable(code: str) -> bool:
    """Whether a request whose response failed with *code* may succeed when sent again.

    Adapters raising :class:`ProviderResponseError` for a failure the
    response itself reported decide its retry by this, and the classifier
    applies it to an error an SDK raised from inside a stream; an unknown
    code may pass.
    """
    return _code_kind(code) not in _FINAL_IN_BAND_KINDS


class ContinuationVerdictError(Exception):
    """A failure that states what it judged of the response it ended.

    ``invalidates_continuation_token`` true means a live continuation token
    now names a completed, immutable response: a retry must issue a fresh
    request, never re-poll it.  ``blank_response`` true means that response
    showed nothing — no answer text, no call — so sampling the same request
    again may answer (:func:`is_blank_response`).  Raisers in higher tiers
    subclass this so the kernel and the classifier read typed fields.
    """

    invalidates_continuation_token: bool = False
    blank_response: bool = False


class ProviderResponseError(ContinuationVerdictError):
    """A provider failure an adapter found in a response the SDK accepted.

    Adapters raise it for failures the SDK does not raise itself — a stream
    that ended without its terminal event, an error-typed finish reason — and
    state the retry decision explicitly.

    ``observed_contents`` holds what the failed response showed that the
    adapter never yielded, such as provider-hosted tool work: the retry gates
    count it as executed. It is opaque here; the layer that reads it knows
    its type. ``usage_details`` holds the token usage the failed response
    reported, so the tokens it consumed are still counted.
    """

    def __init__(
        self,
        code: str,
        provider_message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        invalidates_continuation_token: bool = False,
        kind: ErrorKind | None = None,
        observed_contents: tuple[object, ...] = (),
        usage_details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {provider_message}")
        self.code = code
        self.provider_message = provider_message
        self.retryable = retryable
        self.retry_after = retry_after
        self.invalidates_continuation_token = invalidates_continuation_token
        self.kind = kind if kind is not None else _code_kind(code)
        self.observed_contents = observed_contents
        self.usage_details = usage_details

    @classmethod
    def from_body(
        cls,
        body: object,
        *,
        fallback_code: str,
        fallback_message: str,
        retryable: bool | None = None,
        invalidates_continuation_token: bool = False,
        observed_contents: tuple[object, ...] = (),
        usage_details: Mapping[str, Any] | None = None,
    ) -> ProviderResponseError:
        """An in-band failure using the same envelope rules as HTTP/SDK errors.

        A caller's lifecycle decision (such as cancellation) can veto retry;
        protocol-specific lossy codes never discard a precise gateway verdict.
        """
        payload = ProviderErrorPayload.read(body)
        code = payload.code or payload.error_type or fallback_code
        verdict = gateway_verdict(payload, None)
        kind = verdict.kind if verdict is not None else _code_kind(code)
        may_retry = verdict.retryable if verdict is not None else in_band_failure_retryable(code)
        return cls(
            code,
            payload.message or fallback_message,
            kind=kind,
            retryable=may_retry if retryable is None else retryable,
            invalidates_continuation_token=invalidates_continuation_token,
            observed_contents=observed_contents,
            usage_details=usage_details,
        )


@dataclass(frozen=True, slots=True)
class ProviderSignal:
    """The provider's own statement about a failure, read from one node."""

    # Every field below was read from this one exception.
    source: BaseException
    # None for a bare ``APIError`` raised from an in-band stream event.
    status_code: int | None
    payload: ProviderErrorPayload
    # Seconds from the response's ``retry-after`` header.
    retry_after: float | None
    # Set only by :class:`ProviderResponseError`.
    explicit_retryable: bool | None = None
    verdict: ProviderVerdict | None = None

    @property
    def code(self) -> str | None:
        return self.payload.code

    @property
    def error_type(self) -> str | None:
        return self.payload.error_type

    @property
    def message(self) -> str | None:
        return self.payload.message


def _retry_after(node: BaseException) -> float | None:
    response = getattr(node, "response", None)
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    raw = headers.get("retry-after")
    try:
        seconds = float(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


def _status(node: BaseException) -> int | None:
    status = getattr(node, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _sdk_payload(node: BaseException, body: object) -> ProviderErrorPayload:
    payload = ProviderErrorPayload.read(
        body,
        code=getattr(node, "code", None),
        error_type=getattr(node, "type", None),
        message=getattr(node, "message", None),
    )
    if payload.canonical_code is None:
        # OpenAI unwraps body.error before constructing its status exception,
        # losing Responses' sibling error_type. Recover only that field from
        # this same response's already-buffered body; never read a live stream.
        response = getattr(node, "response", None)
        try:
            original_body = response.content if response is not None else None
        except AttributeError, RuntimeError:
            original_body = None
        original = ProviderErrorPayload.read(original_body)
        if original.canonical_code is not None:
            payload = replace(payload, canonical_code=original.canonical_code)
    return payload


def provider_signal(explicit: Iterable[BaseException]) -> ProviderSignal | None:
    """Return the signal of the first SDK-shaped node in *explicit*, if any."""
    for node in explicit:
        if isinstance(node, ProviderResponseError):
            return ProviderSignal(
                source=node,
                status_code=None,
                payload=ProviderErrorPayload(code=node.code, message=node.provider_message),
                retry_after=node.retry_after,
                explicit_retryable=node.retryable,
            )
        status = _status(node)
        body = getattr(node, "body", None)
        if status is None and (body is None or not hasattr(node, "code")):
            continue
        payload = _sdk_payload(node, body)
        retry_after = _retry_after(node)
        return ProviderSignal(
            source=node,
            status_code=status,
            payload=payload,
            retry_after=retry_after,
            verdict=gateway_verdict(payload, status, retry_after),
        )
    return None


def _is_server_error(status: int | None) -> bool:
    return status is not None and 500 <= status < 600


def status_kind(status: int) -> ErrorKind:
    """Map an HTTP error status to its coarse kind."""
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (401, 403):
        return ErrorKind.AUTH_FAILED
    if status == 413:
        return ErrorKind.PAYLOAD_TOO_LARGE
    if status == _PAYMENT_REQUIRED:
        return ErrorKind.QUOTA_EXHAUSTED
    if status == 529:
        return ErrorKind.OVERLOADED
    if _is_server_error(status):
        return ErrorKind.SERVER_ERROR
    if 400 <= status < 500:
        return ErrorKind.REQUEST_REJECTED
    return ErrorKind.UNKNOWN


def _is_quota(signal: ProviderSignal) -> bool:
    if signal.code in NON_RETRYABLE_PROVIDER_ERROR_CODES or signal.error_type in NON_RETRYABLE_PROVIDER_ERROR_CODES:
        return True
    if signal.code == _GLM_QUOTA_CODE and signal.status_code == 429:
        return True
    if _CREDIT_EXHAUSTED_PHRASE in (signal.message or "").casefold():
        return True
    return signal.status_code == _PAYMENT_REQUIRED


def signal_kind(signal: ProviderSignal) -> tuple[ErrorKind, str]:
    """Return the kind *signal* names and the evidence for it."""
    source = signal.source
    if isinstance(source, ProviderResponseError):
        return source.kind, f"provider {source.code}"
    evidence = f"{type(source).__name__} http {signal.status_code}"
    if signal.verdict is not None:
        return signal.verdict.kind, f"{evidence} {signal.verdict.evidence}"
    if _is_quota(signal):
        return ErrorKind.QUOTA_EXHAUSTED, f"{evidence} {signal.code or signal.error_type or ''}".rstrip()
    # An error a response reports in-band, with no status of its own or under
    # its 2xx status, is how that response failed: the codes only a response
    # reports name it too.
    in_band = signal.status_code is None or is_2xx(signal.status_code)
    codes = _IN_BAND_CODE_KINDS if in_band else _PROVIDER_CODE_KINDS
    # A code that only repeats a broad type says no more than the type does
    # (DeepSeek answers every error status with code ``invalid_request_error``,
    # a bad key's 401 included), so it ranks as the type below.
    code = signal.code if in_band or signal.code not in _BROAD_TYPE_CODES else None
    if code is not None and (kind := codes.get(code)) is not None:
        return kind, f"{evidence} {code}"
    # An error status outranks the error type, which can be broad
    # (OpenAI answers a bad key with 401 ``invalid_request_error``); the type
    # decides only for errors reported without one, e.g. inside a 2xx stream.
    if signal.status_code is not None and (kind := status_kind(signal.status_code)) is not ErrorKind.UNKNOWN:
        return kind, evidence
    for name in (signal.error_type, signal.code):
        if name is not None and (kind := codes.get(name)) is not None:
            return kind, f"{evidence} {name}"
    return ErrorKind.UNKNOWN, evidence


def _names_overflow(texts: Iterable[str]) -> bool:
    texts = [text.casefold() for text in texts if text]
    if any(phrase in text for text in texts for phrase in _NON_OVERFLOW_PHRASES):
        return False
    return any(pattern.search(text) for text in texts for pattern in _CONTEXT_OVERFLOW_PATTERNS)


def names_context_overflow(signal: ProviderSignal | None, unstructured_texts: Iterable[str]) -> bool:
    """Return whether explicit evidence says the request overflowed the context window.

    With a provider signal only its own status, code and text count.  Without
    one (a plain exception carrying the provider's text), *unstructured_texts*
    — the explicit nodes' texts, minus nodes Chrys's own policies raised — are
    read instead.
    """
    if signal is None:
        return _names_overflow(unstructured_texts)
    source = signal.source
    if isinstance(source, ProviderResponseError):
        return source.kind is ErrorKind.CONTEXT_OVERFLOW
    if signal.verdict is not None:
        return signal.verdict.kind is ErrorKind.CONTEXT_OVERFLOW
    status = signal.status_code
    if status in _NON_OVERFLOW_STATUS_CODES or _is_server_error(status) or signal.code in _NON_OVERFLOW_CODES:
        return False
    if signal.code in _CONTEXT_OVERFLOW_CODES:
        return True
    return _names_overflow((signal.message or "", _clean_exception_text(source)))


def named_context_limit(texts: Iterable[str]) -> int | None:
    """Return the one positive window limit, in tokens, that an overflow's *texts* name; None for none or several."""
    limits = {
        int(match.group(1).replace(",", ""))
        for text in texts
        for pattern in _CONTEXT_LIMIT_PATTERNS
        for match in pattern.finditer(text)
    }
    limits.discard(0)
    return limits.pop() if len(limits) == 1 else None


def names_server_error_overflow(signal: ProviderSignal) -> bool:
    """Return whether a 5xx names the context window, which :func:`names_context_overflow` vetoes.

    The veto stands for retry policy: a 5xx may name a temporary limit ("maximum
    context length is temporarily reduced").  A gateway may also wrap a real
    overflow in one, though.  Rate-window wording still vetoes it, and a
    :class:`ProviderResponseError` names its own kind.
    """
    if not _is_server_error(signal.status_code) or isinstance(signal.source, ProviderResponseError):
        return False
    if signal.code in _NON_OVERFLOW_CODES:
        return False
    if signal.code in _CONTEXT_OVERFLOW_CODES:
        return True
    return _names_overflow((signal.message or "", _clean_exception_text(signal.source)))


def stream_error_retryable(signal: ProviderSignal, kind: ErrorKind) -> bool:
    """Retry decision for an error a response reported under its 2xx status.

    *kind* is what its code or type names. The error failed that response, so
    a kind a retry meets again is final, as for any failure a response
    reports (:func:`in_band_failure_retryable`); so is a type Anthropic
    reports for one.
    """
    return kind not in _FINAL_IN_BAND_KINDS and signal.error_type not in _NON_RETRYABLE_STREAM_ERROR_TYPES


def is_2xx(status: int | None) -> bool:
    return status is not None and 200 <= status < 300
