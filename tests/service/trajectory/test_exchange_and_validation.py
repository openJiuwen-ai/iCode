# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``ExchangeTrace`` markers and the validation middleware's recording on top of them."""

from __future__ import annotations

import pytest

from chrys.foundation.errors.kinds import ErrorKind
from chrys.foundation.errors.provider import ProviderResponseError
from chrys.foundation.trajectory.context import (
    TRAJECTORY_EXCHANGE_KWARG,
    ExchangeTrace,
    TrajectoryContext,
)
from chrys.foundation.trajectory.envelope import LinkRelation, MeasurementSource
from chrys.foundation.trajectory.event_types import (
    EventType,
    ExchangeOutcome,
    ValidationOutcome,
    ValidationReason,
)
from chrys.foundation.trajectory.ids import is_valid_analytics_id, new_analytics_id
from chrys.service.llm.observer import ExchangeRecorder
from chrys.service.trajectory.validation import ValidationTrace
from tests.service.trajectory._fakes import FakeSink, make_context

MONOTONIC = {"source": MeasurementSource.MONOTONIC_CLOCK, "method_version": 1}


def _exchange_context(sink: FakeSink, **facts: str) -> TrajectoryContext:
    context = make_context(sink).with_cycle(new_analytics_id()).with_exchange(new_analytics_id())
    return context.with_exchange_facts(facts) if facts else context


# ------------------------------------------------------------------------ ExchangeTrace


def test_exchange_trace_requires_an_exchange_operation() -> None:
    with pytest.raises(ValueError, match="exchange operation"):
        ExchangeTrace(make_context().with_cycle(new_analytics_id()))


def test_exchange_started_merges_facts_and_hangs_under_the_cycle() -> None:
    sink = FakeSink()
    context = _exchange_context(sink, model_profile_id="gpt", agent_profile_id="main")
    trace = ExchangeTrace(context)
    assert trace.operation_id == context.exchange_operation_id
    assert not trace.is_started and not trace.is_finished
    trace.started(payload={"continuation_mode": "none", "model_profile_id": "override"})
    trace.started(payload={"ignored": True})
    assert trace.is_started
    event = sink.only(EventType.MODEL_EXCHANGE_STARTED)
    assert event.operation_id == context.exchange_operation_id
    assert event.parent_operation_id == context.cycle_operation_id
    assert event.actor == context.actor
    assert event.turn_id == context.turn_id
    assert event.payload["model_profile_id"] == "override"
    assert event.payload["agent_profile_id"] == "main"
    assert event.payload["continuation_mode"] == "none"
    assert event.payload["started_at"] == event.occurred_at
    assert "context_revision_id" not in event.payload


def test_exchange_finished_payload_and_measurements_with_chunks() -> None:
    sink = FakeSink()
    context = _exchange_context(sink, model_profile_id="gpt")
    trace = ExchangeTrace(context)
    trace.set_context_revision(new_analytics_id())
    trace.started()
    trace.chunk_observed(visible=False)
    trace.chunk_observed(visible=True)
    trace.chunk_observed(visible=True)
    trace.stall_observed()
    trace.finished(
        outcome=ExchangeOutcome.SUCCESS,
        payload={"response_id": "resp_1", "usage": {"input_tokens": 3}},
        measurements={"/payload/usage": {"source": MeasurementSource.PROVIDER}},
    )
    trace.finished(outcome=ExchangeOutcome.ERROR)
    assert trace.is_finished
    started = sink.only(EventType.MODEL_EXCHANGE_STARTED)
    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert started.payload["context_revision_id"] == trace.context_revision_id
    assert finished.operation_id == context.exchange_operation_id
    assert finished.parent_operation_id == context.cycle_operation_id
    payload = finished.payload
    assert payload["outcome"] == ExchangeOutcome.SUCCESS
    assert payload["model_profile_id"] == "gpt"
    assert payload["context_revision_id"] == trace.context_revision_id
    assert payload["response_id"] == "resp_1"
    assert payload["usage"] == {"input_tokens": 3}
    assert payload["started_at"] == started.payload["started_at"]
    assert payload["ended_at"] == finished.occurred_at
    assert payload["chunk_count"] == 3
    assert payload["stall_count"] == 1
    for key in ("duration_ms", "ttfc_ms", "ttfv_ms", "inter_chunk_p50_ms", "inter_chunk_p95_ms", "max_chunk_gap_ms"):
        assert payload[key] >= 0, key
    assert payload["ttfc_ms"] <= payload["ttfv_ms"] <= payload["duration_ms"]
    assert finished.measurements == {
        "/payload/duration_ms": MONOTONIC,
        "/payload/ttfc_ms": MONOTONIC,
        "/payload/ttfv_ms": MONOTONIC,
        "/payload/inter_chunk_p50_ms": MONOTONIC,
        "/payload/inter_chunk_p95_ms": MONOTONIC,
        "/payload/max_chunk_gap_ms": MONOTONIC,
        "/payload/usage": {"source": MeasurementSource.PROVIDER},
    }


def test_exchange_finished_without_chunks_omits_chunk_timings() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()
    trace.chunk_observed(visible=False)
    trace.finished(outcome=ExchangeOutcome.ERROR)
    payload = sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload
    assert payload["chunk_count"] == 1
    assert "ttfc_ms" in payload
    assert "ttfv_ms" not in payload
    assert "inter_chunk_p50_ms" not in payload
    assert "max_chunk_gap_ms" not in payload


def test_an_exchange_that_never_started_records_nothing_when_it_closes() -> None:
    """The wire client reports the start immediately before the call.

    A trace closed without one never reached the provider — a cancel during
    request preparation — so there is no acquisition to report, and writing
    the terminal alone would leave a span whose start no reader can find.
    """
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.finished(outcome=ExchangeOutcome.CANCELLED)
    assert not trace.is_started
    assert trace.is_finished
    assert sink.event_types == []


def test_exchange_mark_outcome_overrides_the_client_outcome() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()
    trace.mark_outcome(ExchangeOutcome.STALLED)
    trace.finished(outcome=ExchangeOutcome.SUCCESS)
    assert sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload["outcome"] == ExchangeOutcome.STALLED


def test_exchange_abandon_closes_with_abandoned_once() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()
    trace.abandon()
    trace.abandon()
    finished = sink.of_type(EventType.MODEL_EXCHANGE_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload["outcome"] == ExchangeOutcome.ABANDONED


@pytest.mark.asyncio
async def test_a_forwarded_exchange_is_not_closed_by_the_client_that_failed() -> None:
    """The loop knows which failure this was; the client only knows that it was one."""
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()

    async def _boom() -> None:
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        await ExchangeRecorder(trace, owned=False).wrap_awaitable(_boom())

    assert sink.of_type(EventType.MODEL_EXCHANGE_FINISHED) == []
    # The layer above unwinds next, and its verdict — which carries the retry
    # classification — is the one that lands.
    trace.finished(outcome=ExchangeOutcome.ERROR, payload={"error_code": "RuntimeError", "retryable": True})
    assert sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload["retryable"] is True


@pytest.mark.asyncio
async def test_a_client_owned_exchange_closes_itself_when_it_fails() -> None:
    """A side call below the kernel has nobody above it to close its exchange."""
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()

    async def _boom() -> None:
        raise RuntimeError("side call failed")

    with pytest.raises(RuntimeError):
        await ExchangeRecorder(trace, owned=True).wrap_awaitable(_boom())

    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert finished.payload["outcome"] == ExchangeOutcome.ERROR
    assert finished.payload["error_code"] == "RuntimeError"
    # The failed exchange carries what the error was, not only its type: a
    # reader classifying why the request died has nothing else to go on.
    assert finished.payload["error_kind"] == ErrorKind.UNKNOWN


@pytest.mark.asyncio
async def test_a_client_owned_exchange_records_the_tokens_its_failed_response_spent() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()

    async def _boom() -> None:
        raise ProviderResponseError(
            "server_error",
            "failed midway",
            retryable=False,
            usage_details={"input_token_count": 100, "cache_read_input_token_count": 40, "output_token_count": 7},
        )

    with pytest.raises(ProviderResponseError):
        await ExchangeRecorder(trace, owned=True).wrap_awaitable(_boom())

    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert finished.payload["outcome"] == ExchangeOutcome.ERROR
    normalized = finished.payload["usage"]["normalized"]
    assert (normalized["input_total"], normalized["cache_read"], normalized["output_total"]) == (100, 40, 7)
    assert finished.measurements["/payload/usage/normalized/input_total"]["source"] == MeasurementSource.PROVIDER
    assert finished.measurements["/payload/usage/normalized/output_total"]["source"] == MeasurementSource.PROVIDER


@pytest.mark.asyncio
async def test_a_client_owned_exchange_with_no_usage_to_report_records_none() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()

    async def _boom() -> None:
        raise ProviderResponseError("server_error", "failed at once", retryable=False)

    with pytest.raises(ProviderResponseError):
        await ExchangeRecorder(trace, owned=True).wrap_awaitable(_boom())

    assert "usage" not in sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload


def test_a_finished_exchange_with_no_response_model_says_which_model_was_asked() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started(payload={"request_model": "asked"})

    trace.finished(outcome=ExchangeOutcome.ERROR)

    assert sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload["request_model"] == "asked"


def test_a_failed_exchange_names_the_model_it_asked_rather_than_its_profiles() -> None:
    """A per-request override beats the profile's model on the start marker, so it must on the close too."""
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink).with_exchange_facts({"request_model": "profile"}))
    trace.started(payload={"request_model": "override"})

    trace.finished(outcome=ExchangeOutcome.ERROR)

    assert sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload["request_model"] == "override"


def test_a_finished_exchange_with_a_response_model_needs_no_request_model() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started(payload={"request_model": "asked"})

    trace.finished(outcome=ExchangeOutcome.SUCCESS, payload={"response_model": "answered"})

    payload = sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload
    assert payload["response_model"] == "answered"
    assert "request_model" not in payload


def test_a_finished_exchange_that_never_named_a_model_stays_silent_about_it() -> None:
    sink = FakeSink()
    trace = ExchangeTrace(_exchange_context(sink))
    trace.started()

    trace.finished(outcome=ExchangeOutcome.ERROR)

    assert "request_model" not in sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload


# ---------------------------------------------------------------------- ValidationTrace


def test_validation_open_reads_the_exchange_trace_from_client_kwargs() -> None:
    trace = ExchangeTrace(_exchange_context(FakeSink()))
    opened = ValidationTrace.open({"client_kwargs": {TRAJECTORY_EXCHANGE_KWARG: trace}})
    assert opened is not None
    assert opened.exchange_operation_id == trace.operation_id
    assert ValidationTrace.open({}) is None
    assert ValidationTrace.open({"client_kwargs": {}}) is None
    assert ValidationTrace.open({"client_kwargs": {TRAJECTORY_EXCHANGE_KWARG: object()}}) is None
    assert ValidationTrace.open({"client_kwargs": "nope"}) is None


@pytest.mark.asyncio
async def test_validation_finished_rejected_links_to_the_exchange() -> None:
    sink = FakeSink()
    context = _exchange_context(sink)
    exchange = ExchangeTrace(context)
    validation = ValidationTrace(exchange)
    await validation.finished(accepted=False, reason_code=ValidationReason.EMPTY_CONTENTS)
    event = sink.only(EventType.MODEL_VALIDATION_FINISHED)
    assert is_valid_analytics_id(event.operation_id)
    assert event.operation_id != exchange.operation_id
    assert event.parent_operation_id == exchange.operation_id
    assert event.links == (
        type(event.links[0])(relation=LinkRelation.VALIDATES, target_operation_id=exchange.operation_id),
    )
    assert event.payload == {
        "outcome": ValidationOutcome.REJECTED,
        "exchange_operation_id": exchange.operation_id,
        "attempt_index": 0,
        "reason_code": ValidationReason.EMPTY_CONTENTS,
    }


@pytest.mark.asyncio
async def test_validation_finished_accepted_shape() -> None:
    sink = FakeSink()
    exchange = ExchangeTrace(_exchange_context(sink))
    await ValidationTrace(exchange).finished(accepted=True)
    assert sink.only(EventType.MODEL_VALIDATION_FINISHED).payload == {
        "outcome": ValidationOutcome.ACCEPTED,
        "exchange_operation_id": exchange.operation_id,
        "attempt_index": 0,
    }


@pytest.mark.asyncio
async def test_validation_emit_failures_are_swallowed() -> None:
    sink = FakeSink()
    validation = ValidationTrace(ExchangeTrace(_exchange_context(sink)))
    sink.fail_next = True
    await validation.finished(accepted=False, reason_code=ValidationReason.UNKNOWN)
    assert sink.drafts == []
    await validation.finished(accepted=True)
    assert sink.only(EventType.MODEL_VALIDATION_FINISHED).payload["outcome"] == ValidationOutcome.ACCEPTED
