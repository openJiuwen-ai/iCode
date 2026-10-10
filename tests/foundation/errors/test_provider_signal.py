# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Provider signals: read from one node, never from stale context."""

from __future__ import annotations

from typing import Any, NoReturn

import httpx
import pytest
from openai import APIError, APIStatusError

from chrys.foundation.errors import (
    ContinuationVerdictError,
    ErrorKind,
    ProviderResponseError,
    classify_error,
    in_band_failure_retryable,
    invalidates_continuation_token,
)
from chrys.foundation.errors import classify as classify_module
from chrys.service.context.compaction.last_words import LastWordsGenerationError
from tests.foundation.errors._corpus import STALE_PROVIDER_ERRORS
from tests.support.provider_errors import openai_status, raised_from, raised_while_handling


class _BareStreamError(Exception):
    """The shape of a bare ``openai.APIError``: ``body`` and ``code``, no status."""

    def __init__(self, message: str, body: Any, code: str | None) -> None:
        super().__init__(message)
        self.body = body
        self.code = code


class _StatusError(Exception):
    def __init__(self, message: str, status_code: int, response: httpx.Response | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


async def test_stale_context_quota_error_does_not_classify_new_read_timeout() -> None:
    stale = await STALE_PROVIDER_ERRORS["429-insufficient-quota"]()

    def raise_fresh() -> NoReturn:
        raise httpx.ReadTimeout("timed out")

    result = classify_error(raised_while_handling(stale, raise_fresh))

    assert result.signal is None
    assert result.kind is ErrorKind.READ_TIMEOUT
    assert result.retryable is True


def test_signal_fields_come_from_one_node() -> None:
    inner = _StatusError("Error code: 429", 429)
    outer = raised_from(_BareStreamError("stream failed", {"message": "stream failed"}, None), inner)

    signal = classify_error(outer).signal

    assert signal is not None
    assert signal.source is outer
    assert (signal.status_code, signal.code, signal.message) == (None, None, "stream failed")


async def test_signal_reads_code_type_message_and_retry_after_from_the_sdk_error() -> None:
    exc = await openai_status(
        429, {"error": {"type": "insufficient_quota", "code": "insufficient_quota", "message": "Quota exceeded"}}
    )

    signal = classify_error(exc).signal

    assert signal is not None
    assert signal.source is exc
    assert (signal.status_code, signal.code, signal.error_type, signal.message) == (
        429,
        "insufficient_quota",
        "insufficient_quota",
        "Quota exceeded",
    )
    assert signal.retry_after is None


def test_retry_after_header_is_read_in_seconds() -> None:
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(429, headers={"retry-after": "7"}, request=request)
    exc = _StatusError("Error code: 429", 429, response)

    signal = classify_error(exc).signal

    assert signal is not None
    assert signal.retry_after == 7.0


def test_system_exit_code_is_not_a_provider_signal() -> None:
    assert classify_error(SystemExit(2)).signal is None
    assert classify_error(_BareStreamError("no body", None, "insufficient_quota")).signal is None


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        (
            ProviderResponseError("stream_truncated", "no message_stop", retryable=True),
            ErrorKind.STREAM_TRUNCATED,
            True,
        ),
        (
            ProviderResponseError("overloaded_error", "busy", retryable=False, kind=ErrorKind.OVERLOADED),
            ErrorKind.OVERLOADED,
            False,
        ),
        (ProviderResponseError("vendor_specific", "?", retryable=True), ErrorKind.UNKNOWN, True),
        (ProviderResponseError("network_error", "?", retryable=True), ErrorKind.STREAM_TRUNCATED, True),
        (ProviderResponseError("insufficient_system_resource", "?", retryable=True), ErrorKind.OVERLOADED, True),
    ],
    ids=["truncated-retryable", "explicit-kind-not-retryable", "unknown-code", "network-error", "no-resources"],
)
def test_provider_response_error_states_kind_and_retry(
    error: ProviderResponseError, kind: ErrorKind, retryable: bool
) -> None:
    result = classify_error(error)

    assert (result.kind, result.retryable) == (kind, retryable)
    assert str(error) == f"{error.code}: {error.provider_message}"


@pytest.mark.parametrize(
    ("code", "kind", "retryable"),
    [
        ("server_error", ErrorKind.SERVER_ERROR, True),
        ("rate_limit_exceeded", ErrorKind.RATE_LIMITED, True),
        ("vector_store_timeout", ErrorKind.UNKNOWN, True),
        ("vendor_specific", ErrorKind.UNKNOWN, True),
        ("context_length_exceeded", ErrorKind.CONTEXT_OVERFLOW, False),
        ("insufficient_quota", ErrorKind.QUOTA_EXHAUSTED, False),
        ("invalid_prompt", ErrorKind.REQUEST_REJECTED, False),
        ("invalid_image_url", ErrorKind.REQUEST_REJECTED, False),
        ("cyber_policy", ErrorKind.CONTENT_FILTERED, False),
        ("image_content_policy_violation", ErrorKind.CONTENT_FILTERED, False),
    ],
)
def test_a_failure_a_response_reports_retries_only_when_its_code_may_pass(
    code: str, kind: ErrorKind, retryable: bool
) -> None:
    error = ProviderResponseError(code, "?", retryable=in_band_failure_retryable(code))

    assert (classify_error(error).kind, classify_error(error).retryable) == (kind, retryable)


@pytest.mark.parametrize(
    "code",
    [
        "network_error",
        "insufficient_system_resource",
        "invalid_prompt",
        "cyber_policy",
        "misalignment_policy_violation",
        "image_content_policy_violation",
        "invalid_image_url",
    ],
)
@pytest.mark.parametrize("status", [400, 502])
async def test_an_http_error_carrying_a_code_only_responses_report_is_read_by_its_status(
    status: int, code: str
) -> None:
    # These codes name how a response failed, not what an HTTP error means.
    carrying = classify_error(await openai_status(status, {"error": {"code": code, "message": "It broke."}}))
    plain = classify_error(await openai_status(status, {"error": {"code": "vendor_specific", "message": "It broke."}}))

    assert (carrying.kind, carrying.retryable) == (plain.kind, plain.retryable)


@pytest.mark.parametrize(
    ("code", "kind", "retryable"),
    [
        ("network_error", ErrorKind.STREAM_TRUNCATED, True),
        ("insufficient_system_resource", ErrorKind.OVERLOADED, True),
        ("invalid_prompt", ErrorKind.REQUEST_REJECTED, False),
        ("cyber_policy", ErrorKind.CONTENT_FILTERED, False),
        ("image_content_policy_violation", ErrorKind.CONTENT_FILTERED, False),
        ("invalid_image_url", ErrorKind.REQUEST_REJECTED, False),
        ("invalid_request_error", ErrorKind.REQUEST_REJECTED, False),
    ],
)
def test_an_error_a_stream_reports_in_band_is_named_and_retried_by_its_code(
    code: str, kind: ErrorKind, retryable: bool
) -> None:
    # The SDK raises an error event inside a 200 stream as a bare APIError:
    # the code names how that response failed, and a retry meets a final
    # failure again, as when an adapter raises it.
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    carrying = classify_error(APIError("It broke.", request, body={"code": code, "message": "It broke."}))
    plain = classify_error(APIError("It broke.", request, body={"code": "vendor_specific", "message": "It broke."}))

    assert carrying.kind is kind
    assert carrying.retryable is retryable
    assert in_band_failure_retryable(code) is retryable
    assert (plain.kind, plain.retryable) == (ErrorKind.UNKNOWN, True)


def test_an_error_a_stream_reports_in_band_with_only_a_broad_type_keeps_its_retry() -> None:
    # Only a code names the failure; the type is broad (OpenAI files many
    # errors under ``invalid_request_error``), so it names the kind alone.
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    typed = classify_error(APIError("It broke.", request, body={"type": "invalid_request_error", "message": "x"}))

    assert (typed.kind, typed.retryable) == (ErrorKind.REQUEST_REJECTED, True)


@pytest.mark.parametrize(
    ("code", "kind", "retryable"),
    [
        ("network_error", ErrorKind.STREAM_TRUNCATED, True),
        ("insufficient_system_resource", ErrorKind.OVERLOADED, True),
        ("server_error", ErrorKind.SERVER_ERROR, True),
        ("invalid_prompt", ErrorKind.REQUEST_REJECTED, False),
        ("authentication_error", ErrorKind.AUTH_FAILED, False),
        ("content_filter", ErrorKind.CONTENT_FILTERED, False),
        ("insufficient_quota", ErrorKind.QUOTA_EXHAUSTED, False),
    ],
)
def test_an_error_a_response_reports_under_its_2xx_status_is_named_and_retried_by_its_code(
    code: str, kind: ErrorKind, retryable: bool
) -> None:
    # An error body a gateway answers with under HTTP 200, or a stream's error
    # event an SDK raises with the 200 it streamed under, reports how that
    # response failed: read as an error a stream reports in-band.
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(200, request=request)
    carrying = classify_error(APIStatusError("It broke.", response=response, body={"code": code, "message": "x"}))
    plain = classify_error(APIStatusError("It broke.", response=response, body={"code": "vendor_specific"}))

    assert carrying.kind is kind
    assert carrying.retryable is retryable
    assert (plain.kind, plain.retryable) == (ErrorKind.UNKNOWN, True)


def test_an_error_a_response_reports_under_its_2xx_status_is_final_by_a_type_a_retry_meets_again() -> None:
    # Unlike an in-band error without a status, a type alone decides here
    # (Anthropic names each stream error precisely).
    request = httpx.Request("POST", "https://api.example.test/v1/messages")
    response = httpx.Response(200, request=request)
    typed = classify_error(APIStatusError("x", response=response, body={"type": "invalid_request_error"}))
    overloaded = classify_error(APIStatusError("x", response=response, body={"type": "overloaded_error"}))

    assert (typed.kind, typed.retryable) == (ErrorKind.REQUEST_REJECTED, False)
    assert (overloaded.kind, overloaded.retryable) == (ErrorKind.OVERLOADED, True)


def test_owner_terminal_veto_outranks_a_retryable_provider_response_error() -> None:
    inner = ProviderResponseError("stream_truncated", "no message_stop", retryable=True)

    result = classify_error(raised_from(LastWordsGenerationError("note failed"), inner))

    assert result.kind is ErrorKind.STREAM_TRUNCATED
    assert result.retryable is False


def test_continuation_token_invalidation_is_found_below_a_wrapper() -> None:
    inner = ProviderResponseError("stream_truncated", "?", retryable=True, invalidates_continuation_token=True)

    assert invalidates_continuation_token(inner) is True
    assert invalidates_continuation_token(raised_from(RuntimeError("wrapper"), inner)) is True
    assert invalidates_continuation_token(ProviderResponseError("stream_truncated", "?", retryable=True)) is False


def test_only_a_typed_continuation_verdict_invalidates_the_token() -> None:
    class Judged(ContinuationVerdictError):
        invalidates_continuation_token = True

    class LookAlike(Exception):
        invalidates_continuation_token = True

    assert invalidates_continuation_token(raised_from(RuntimeError("wrapper"), Judged())) is True
    assert invalidates_continuation_token(ContinuationVerdictError()) is False
    assert invalidates_continuation_token(LookAlike()) is False


def test_the_continuation_verdict_never_runs_the_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every failed wire call asks before the retry policy classifies; a
    # classifier failure here would replace the provider's own error.
    class Judged(ContinuationVerdictError):
        invalidates_continuation_token = True

    def refuse(_exc: BaseException) -> NoReturn:
        raise AssertionError("classified")

    monkeypatch.setattr(classify_module, "classify_error", refuse)

    assert invalidates_continuation_token(raised_from(RuntimeError("wrapper"), Judged())) is True
    assert invalidates_continuation_token(RuntimeError("plain")) is False
