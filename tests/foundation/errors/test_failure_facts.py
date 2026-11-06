# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The verdict a frontend gets: whether a failure may pass later, apart from what the retry lanes do now."""

from __future__ import annotations

from typing import NoReturn

import anthropic
import pytest

import chrys.foundation.errors.facts as facts_module
from chrys.foundation.errors import ROUTE_EXTENSION_KEY, ErrorKind, RouteFacts, classify_error, failure_facts, origin_of
from chrys.foundation.retry import StreamStall, StreamStallExhausted
from chrys.service.agent_middleware.response_validation import TerminalResponseValidationError
from chrys.service.context.compaction.last_words import LastWordsGenerationError
from chrys.service.llm.chat_completions.validation import raise_invalid_response
from tests.foundation.errors._corpus import CORPUS, Case
from tests.support.provider_errors import (
    API_HOST,
    anthropic_error_event,
    anthropic_status,
    anthropic_stream_error,
    openai_status,
    raised_from,
)

# The cases whose lane policy says no although the failure itself may pass
# later: an owner already retried it to the end, or its own budget concluded
# it. A response validation rejected for anything but showing nothing
# ("terminal-validation") is not among them.
_TRANSIENT_ALTHOUGH_NOT_RETRYABLE = frozenset({"last-words-over-429", "stream-stall"})


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case.id)
async def test_transient_differs_from_retryable_only_where_a_budget_concluded_the_failure(case: Case) -> None:
    result = classify_error(await case.build())

    assert result.retryable is case.retryable
    assert result.transient is (case.retryable or case.id in _TRANSIENT_ALTHOUGH_NOT_RETRYABLE)


def test_every_case_the_lanes_refuse_but_may_pass_is_in_the_corpus() -> None:
    assert {case.id for case in CORPUS} >= _TRANSIENT_ALTHOUGH_NOT_RETRYABLE


async def _quota() -> BaseException:
    return await openai_status(402, {"error": {"message": "Insufficient Balance", "type": "unknown_error"}})


async def _rate_limit() -> BaseException:
    return await openai_status(429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}})


async def test_a_rate_limit_compaction_gave_up_on_may_pass_later() -> None:
    exc = raised_from(LastWordsGenerationError("Failed to generate last words"), await _rate_limit())

    facts = failure_facts(exc)

    # No lane replays it, yet the user's retry later is a new attempt.
    assert classify_error(exc).retryable is False
    assert facts is not None
    assert (facts.kind, facts.retryable) == (ErrorKind.RATE_LIMITED, True)


async def test_an_empty_balance_beneath_compaction_still_needs_a_fix_first() -> None:
    exc = raised_from(LastWordsGenerationError("Failed to generate last words"), await _quota())

    facts = failure_facts(exc)

    assert facts is not None
    assert (facts.kind, facts.retryable) == (ErrorKind.QUOTA_EXHAUSTED, False)


def test_a_stall_that_used_up_its_budget_may_pass_later() -> None:
    exc = raised_from(StreamStallExhausted("stream stalled 3 times"), StreamStall("no chunk for 120s"))

    facts = failure_facts(exc)

    assert facts is not None
    assert (facts.kind, facts.retryable, facts.status_code) == (ErrorKind.STREAM_STALLED, True, None)


@pytest.mark.parametrize(("blank", "retryable"), [(True, True), (False, False)], ids=["showed-nothing", "rejected"])
def test_only_a_response_that_showed_nothing_may_pass_later(*, blank: bool, retryable: bool) -> None:
    exc = TerminalResponseValidationError("The model returned an empty response")
    exc.blank_response = blank

    facts = failure_facts(exc)

    assert classify_error(exc).retryable is False
    assert facts is not None
    assert (facts.kind, facts.retryable) == (ErrorKind.INVALID_RESPONSE, retryable)


def test_an_endpoint_that_is_not_the_models_api_needs_a_fix_first() -> None:
    """A base URL that answers with a web page fails the same way every time: fix the profile first."""
    try:
        raise_invalid_response(
            "Chat Completions API returned an invalid response (HTTP 200, Content-Type 'text/html'): "
            "decoded payload type is str; expected a ChatCompletion object."
        )
    except Exception as exc:
        facts = failure_facts(exc)
    else:
        raise AssertionError("raise_invalid_response must raise")

    assert facts is not None
    assert (facts.kind, facts.retryable) == (ErrorKind.INVALID_RESPONSE, False)


@pytest.mark.parametrize(
    ("in_stream", "status"), [(True, None), (False, 529)], ids=["error-event-after-200", "http-529"]
)
async def test_the_status_is_an_error_status_never_the_200_a_failed_stream_was_sent_under(
    *, in_stream: bool, status: int | None
) -> None:
    """Anthropic reports an overload inside a stream it already answered with 200: no status says why it failed."""
    if in_stream:
        exc = await anthropic_stream_error(anthropic_error_event("overloaded_error", "Overloaded"))
    else:
        exc = await anthropic_status(
            529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        )
    assert isinstance(exc, anthropic.APIStatusError)
    # Every model request carries its route, so the model service answered.
    route = RouteFacts(origin_of(f"https://{API_HOST}"), None, first_hop_reached=True)
    exc.response.request.extensions[ROUTE_EXTENSION_KEY] = route

    facts = failure_facts(exc)

    assert facts is not None
    assert (facts.kind, facts.retryable, facts.status_code) == (ErrorKind.OVERLOADED, True, status)


def test_a_classifier_failure_gives_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(exc: BaseException) -> NoReturn:
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(facts_module, "classify_error", broken)

    assert failure_facts(ValueError("x")) is None
