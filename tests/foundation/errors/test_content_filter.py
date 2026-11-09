# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured gateway content rejections must not become transient 500s."""

from __future__ import annotations

import json

import pytest

from chrys.foundation.errors import ErrorKind, classify_error, clean_error_message
from tests.support.provider_errors import (
    anthropic_status,
    anthropic_stream_error,
    openai_status,
    openai_stream_error,
    raised_from,
    raised_while_handling,
)


@pytest.mark.parametrize("provider", [anthropic_status, openai_status], ids=["anthropic", "openai"])
@pytest.mark.parametrize("status", [400, 500, 502])
async def test_gateway_sensitive_words_verdict_is_terminal(provider, status: int) -> None:
    error = await provider(
        status, {"error": {"type": "new_api_error", "message": "sensitive_words_detected"}, "type": "error"}
    )

    for failure in (error, raised_from(RuntimeError("model request failed"), error)):
        result = classify_error(failure)
        assert (result.kind, result.retryable) == (ErrorKind.CONTENT_FILTERED, False)
        assert clean_error_message(failure) == f"Error code: {status} - sensitive_words_detected"


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        ("api_error", "sensitive_words_detected"),
        ("new_api_error", "content filter temporarily unavailable"),
        ("new_api_error", "upstream failed while checking sensitive_words_detected"),
    ],
)
async def test_ordinary_server_errors_keep_retrying(error_type: str, message: str) -> None:
    error = await anthropic_status(500, {"error": {"type": error_type, "message": message}})

    result = classify_error(error)

    assert (result.kind, result.retryable) == (ErrorKind.SERVER_ERROR, True)


async def test_stale_gateway_rejection_cannot_veto_a_new_server_error() -> None:
    stale = await anthropic_status(500, {"error": {"type": "new_api_error", "message": "sensitive_words_detected"}})
    fresh = await anthropic_status(500, {"error": {"type": "api_error", "message": "temporarily unavailable"}})

    def raise_fresh() -> None:
        raise fresh

    result = classify_error(raised_while_handling(stale, raise_fresh))

    assert (result.kind, result.retryable) == (ErrorKind.SERVER_ERROR, True)


@pytest.mark.parametrize("provider", [anthropic_status, openai_status], ids=["anthropic", "openai"])
@pytest.mark.parametrize("status", [400, 500, 502])
@pytest.mark.parametrize(
    "code",
    [
        "sensitive_words_detected",
        "prompt_blocked",
        "DataInspectionFailed",
        "data_inspection_failed",
        "SensitiveContentDetected",
        "SensitiveContentDetected.Violence",
        "InputTextSensitiveContentDetected.PolicyViolation",
        "OutputTextSensitiveContentDetected",
        "InputImageRiskDetection",
    ],
)
async def test_precise_content_codes_survive_gateway_status_rewrites(provider, status: int, code: str) -> None:
    error = await provider(status, {"error": {"code": code, "message": "Request declined."}})
    result = classify_error(error)
    assert (result.kind, result.retryable) == (ErrorKind.CONTENT_FILTERED, False)


@pytest.mark.parametrize(
    "message",
    ["sensitive_words_detected (request id: req-123)", "user sensitive words detected: example_marker"],
)
async def test_new_api_anthropic_envelope_without_code(message: str) -> None:
    error = await anthropic_status(500, {"error": {"type": "new_api_error", "message": message}})
    result = classify_error(error)
    assert (result.kind, result.retryable) == (ErrorKind.CONTENT_FILTERED, False)


@pytest.mark.parametrize("shape", ["flat", "nested", "json", "type"])
async def test_gateway_payload_shapes(shape: str) -> None:
    body: object = {"code": "DataInspectionFailed", "message": "Declined."}
    if shape == "nested":
        body = {"error": body}
    elif shape == "json":
        body = json.dumps({"error": body})
    elif shape == "type":
        body = {"error": {"type": "DataInspectionFailed", "message": "Declined."}}
    error = await anthropic_status(500, body)
    result = classify_error(error)
    assert (result.kind, result.retryable) == (ErrorKind.CONTENT_FILTERED, False)


@pytest.mark.parametrize(
    ("canonical", "kind", "retryable"),
    [
        ("content_policy_violation", ErrorKind.CONTENT_FILTERED, False),
        ("refusal", ErrorKind.CONTENT_FILTERED, False),
        ("payment_required", ErrorKind.QUOTA_EXHAUSTED, False),
        ("authentication", ErrorKind.AUTH_FAILED, False),
        ("invalid_request", ErrorKind.REQUEST_REJECTED, False),
        ("rate_limit_exceeded", ErrorKind.RATE_LIMITED, True),
        ("provider_overloaded", ErrorKind.OVERLOADED, True),
        ("server", ErrorKind.SERVER_ERROR, True),
    ],
)
@pytest.mark.parametrize("wire", ["chat_http", "chat_stream", "anthropic_http", "anthropic_stream", "responses_http"])
async def test_openrouter_canonical_type_outranks_lossy_protocol_type(
    canonical: str, kind: ErrorKind, retryable: bool, wire: str
) -> None:
    if wire == "responses_http":
        error = await openai_status(
            500, {"error": {"code": "server_error", "message": "Provider failed."}, "error_type": canonical}
        )
    elif wire.startswith("chat"):
        details = {"code": 500, "message": "Provider failed.", "metadata": {"error_type": canonical}}
        error = (
            await openai_status(500, {"error": details})
            if wire.endswith("http")
            else await openai_stream_error(details)
        )
    else:
        body = {"type": "error", "error": {"type": "api_error", "message": "Provider failed.", "error_type": canonical}}
        error = (
            await anthropic_status(500, body)
            if wire.endswith("http")
            else await anthropic_stream_error(json.dumps(body))
        )
    result = classify_error(error)
    assert (result.kind, result.retryable) == (kind, retryable)


@pytest.mark.parametrize("code", ["Arrearage", "insufficient_user_quota"])
@pytest.mark.parametrize("status", [400, 403, 500])
async def test_documented_account_errors_are_quota_exhaustion(code: str, status: int) -> None:
    error = await openai_status(status, {"code": code, "message": "Account cannot pay."})
    result = classify_error(error)
    assert (result.kind, result.retryable) == (ErrorKind.QUOTA_EXHAUSTED, False)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        # OpenAI's credit, spend and usage limits, as the Codex CLI reads them:
        # a 429 that waiting out a rate window cannot clear.
        *(
            (429, {"code": code, "message": "Usage limit reached."})
            for code in (
                "credit_balance_exhausted",
                "organization_spend_limit_exceeded",
                "project_spend_limit_exceeded",
                "organization_usage_limit_exceeded",
                "subscription_sharing_usage_limit_exceeded",
            )
        ),
        (429, {"type": "usage_limit_reached", "message": "The usage limit has been reached"}),
        # LiteLLM proxy: a spent key budget.
        (400, {"type": "budget_exceeded", "code": "400", "message": "Key is out of budget. Spend=10.02, Budget=10.0"}),
    ],
)
async def test_spent_credit_and_usage_limits_are_quota_exhaustion(status: int, error: dict[str, str]) -> None:
    result = classify_error(await openai_status(status, {"error": error}))
    assert (result.kind, result.retryable) == (ErrorKind.QUOTA_EXHAUSTED, False)


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        (
            (
                "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits."
            ),
            ErrorKind.QUOTA_EXHAUSTED,
        ),
        ("messages: text content blocks must be non-empty", ErrorKind.REQUEST_REJECTED),
    ],
)
async def test_anthropic_spent_credit_is_told_apart_from_a_bad_request_by_its_text(
    message: str, kind: ErrorKind
) -> None:
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    result = classify_error(await anthropic_status(400, body))
    assert (result.kind, result.retryable) == (kind, False)


@pytest.mark.parametrize(
    ("metadata", "kind"),
    [
        ({"reasons": ["policy"], "flagged_input": "example", "provider_name": "test"}, ErrorKind.CONTENT_FILTERED),
        ({"limit_source": "openrouter_guardrail_budget"}, ErrorKind.QUOTA_EXHAUSTED),
        ({"limit_source": "openrouter_workspace_budget"}, ErrorKind.QUOTA_EXHAUSTED),
        ({"limit_source": "openrouter_key_limit"}, ErrorKind.QUOTA_EXHAUSTED),
        ({"reasons": ["policy"]}, ErrorKind.AUTH_FAILED),
        ({"limit_source": "unknown_budget"}, ErrorKind.AUTH_FAILED),
        ({}, ErrorKind.AUTH_FAILED),
    ],
)
async def test_openrouter_forbidden_needs_specific_evidence(metadata: dict, kind: ErrorKind) -> None:
    result = classify_error(await openai_status(403, {"error": {"code": 403, "metadata": metadata}}))
    assert (result.kind, result.retryable) == (kind, False)


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"code": "ContentSecurityDetectionError", "message": "inspection failed"}},
        {"error": {"code": "InvalidParameter.DataInspection"}},
        {"error": {"code": "SensitiveContentDetected.UnknownSubtype"}},
        {"error": {"code": 50507, "message": "unknown error"}},
        {"error": {"message": "sensitive_words_detected"}},
        {"error": {"metadata": {"raw": {"error": {"code": "sensitive_words_detected"}}}}},
        {"error": {"metadata": {"flagged_input": "sensitive_words_detected"}}},
        {"error": {"metadata": {"error_type": ["content_policy_violation"]}}},
        {"error": {"error_type": "context_length_exceeded"}},
        {"error": {"code": {"code": "sensitive_words_detected"}}},
    ],
)
async def test_ambiguous_or_untrusted_details_do_not_turn_500_into_content_filter(body: dict) -> None:
    result = classify_error(await openai_status(500, body))
    assert (result.kind, result.retryable) == (ErrorKind.SERVER_ERROR, True)


@pytest.mark.parametrize("retry_after", [None, "0", "3"])
async def test_openrouter_in_flight_budget_is_transient_only_with_retry_after(retry_after: str | None) -> None:
    error = await openai_status(
        402, {"error": {"code": 402, "metadata": {"limit_source": "openrouter_in_flight_budget"}}}
    )
    if retry_after is not None:
        error.response.headers["retry-after"] = retry_after
    result = classify_error(error)
    expected = (ErrorKind.RATE_LIMITED, True) if retry_after == "3" else (ErrorKind.QUOTA_EXHAUSTED, False)
    assert (result.kind, result.retryable) == expected
