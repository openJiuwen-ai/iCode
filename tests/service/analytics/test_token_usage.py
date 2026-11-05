# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Token-usage bucket provenance and the TokenUsage rollup."""

from __future__ import annotations

import pytest

from chrys.service.analytics import (
    Metric,
    Precision,
    UsageBucket,
    analyze_trajectory,
)
from tests.service.analytics._events import NS, EventLog


def test_usage_total_is_missing_when_one_required_normalized_bucket_is_absent(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add("model.exchange.started", 0, operation_id="a" * 32)
    log.add(
        "model.exchange.finished",
        NS,
        operation_id="a" * 32,
        payload={"outcome": "success", "duration_ms": 1000, "usage": {"normalized": {"input_total": 12}}},
        measurements={
            "/payload/duration_ms": {"source": "monotonic_clock", "method_version": 1},
            "/payload/usage/normalized/input_total": {"source": "provider", "adapter_version": 1},
        },
    )
    log.add("turn.finished", NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    usage = analyze_trajectory(path).turns[0].usage_tokens

    assert usage.value is None
    assert usage.precision is Precision.MISSING


def test_negative_normalized_usage_is_missing_instead_of_exact(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.exchange_usage("a" * 32, 0, NS, {"input_total": -1, "output_total": 20})
    log.add("turn.finished", NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    usage = analyze_trajectory(path).turns[0].usage_tokens

    assert usage.value is None
    assert usage.precision is Precision.MISSING


def test_usage_is_missing_when_a_main_exchange_has_no_terminal_event(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add("model.exchange.started", 0, operation_id="a" * 32)
    log.add("turn.finished", NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    usage = analyze_trajectory(path).turns[0].usage_tokens

    assert usage.value is None
    assert usage.precision is Precision.MISSING


def test_orphan_usage_terminal_is_unresolved_even_with_complete_buckets(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add(
        "model.exchange.finished",
        NS,
        operation_id="a" * 32,
        payload={
            "outcome": "success",
            "duration_ms": 1000,
            "usage": {"normalized": {"input_total": 100, "output_total": 20}},
        },
        measurements={
            "/payload/duration_ms": {"source": "monotonic_clock", "method_version": 1},
            "/payload/usage/normalized/input_total": {"source": "provider", "adapter_version": 1},
            "/payload/usage/normalized/output_total": {"source": "provider", "adapter_version": 1},
        },
    )
    log.add("turn.finished", NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "orphan-usage.jsonl"
    log.write(path)

    usage = analyze_trajectory(path).turns[0].usage_tokens

    assert usage.value == 120
    assert usage.precision is Precision.UNRESOLVED
    assert usage.reason == "usage requires a uniquely closed exchange and exact turn lifecycle"


def test_token_usage_splits_buckets_and_marks_unreported_optional_buckets(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.exchange_usage(
        "a" * 32,
        0,
        NS,
        {"input_total": 100, "output_total": 20, "reasoning": 7, "cache_read": 60},
    )
    log.add("turn.finished", NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)
    token_usage = analysis.token_usage

    assert token_usage is not None
    buckets = token_usage.buckets
    assert (buckets[UsageBucket.INPUT].value, buckets[UsageBucket.INPUT].precision) == (100, Precision.EXACT)
    assert (buckets[UsageBucket.OUTPUT].value, buckets[UsageBucket.OUTPUT].precision) == (20, Precision.EXACT)
    assert (buckets[UsageBucket.REASONING].value, buckets[UsageBucket.REASONING].precision) == (7, Precision.EXACT)
    assert (buckets[UsageBucket.CACHE_READ].value, buckets[UsageBucket.CACHE_READ].precision) == (60, Precision.EXACT)
    assert buckets[UsageBucket.CACHE_CREATION].value is None
    assert buckets[UsageBucket.CACHE_CREATION].precision is Precision.MISSING
    assert analysis.turns[0].token_usage is not None
    assert analysis.turns[0].token_usage.buckets == buckets


def test_token_usage_partial_optional_reporting_is_estimated_and_bad_provenance_unresolved(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.exchange_usage(
        "a" * 32,
        0,
        NS,
        {"input_total": 100, "output_total": 20, "reasoning": 7, "cache_creation": 9},
        provider_buckets=("input_total", "output_total", "reasoning"),
    )
    log.exchange_usage(
        "b" * 32,
        NS,
        2 * NS,
        {"input_total": 50, "output_total": 10, "cache_creation": 4},
        provider_buckets=("input_total", "output_total", "cache_creation"),
    )
    log.add("turn.finished", 2 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    token_usage = analyze_trajectory(path).token_usage

    assert token_usage is not None
    buckets = token_usage.buckets
    assert (buckets[UsageBucket.INPUT].value, buckets[UsageBucket.INPUT].precision) == (150, Precision.EXACT)
    assert (buckets[UsageBucket.REASONING].value, buckets[UsageBucket.REASONING].precision) == (
        7,
        Precision.ESTIMATED,
    )
    assert buckets[UsageBucket.CACHE_CREATION].value == 13
    assert buckets[UsageBucket.CACHE_CREATION].precision is Precision.UNRESOLVED
    assert buckets[UsageBucket.CACHE_CREATION].reason == "normalized usage provenance is incomplete"


def test_reasoning_tokens_above_output_are_unresolved(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.exchange_usage("a" * 32, 0, NS, {"input_total": 100, "output_total": 20, "reasoning": 21})
    log.add("turn.finished", NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    reasoning = analyze_trajectory(path).token_usage

    assert reasoning is not None
    assert reasoning.buckets[UsageBucket.REASONING].value == 21
    assert reasoning.buckets[UsageBucket.REASONING].precision is Precision.UNRESOLVED


_UNREPORTED = "one or more model requests failed or were interrupted before reporting full usage, so totals count only what was reported"


def _failed_exchange(log: EventLog, operation_id: str, start_ns: int, end_ns: int, *, outcome: str, **ids) -> None:
    log.add("model.exchange.started", start_ns, operation_id=operation_id, **ids)
    log.add(
        "model.exchange.finished",
        end_ns,
        operation_id=operation_id,
        payload={"outcome": outcome, "duration_ms": max(1, (end_ns - start_ns) // 1_000_000)},
        measurements={"/payload/duration_ms": {"source": "monotonic_clock", "method_version": 1}},
        **ids,
    )


@pytest.mark.parametrize("outcome", ["error", "stalled", "interrupted"])
def test_failed_request_without_usage_makes_totals_a_reported_lower_bound(tmp_path, outcome: str) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    _failed_exchange(log, "b" * 32, 0, NS, outcome=outcome)
    log.exchange_usage("a" * 32, NS, 2 * NS, {"input_total": 100, "output_total": 20})
    log.add("turn.finished", 2 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    assert analysis.turns[0].usage_tokens == Metric(120, Precision.ESTIMATED, _UNREPORTED)
    assert analysis.overview.usage_tokens == Metric(120, Precision.ESTIMATED, _UNREPORTED)
    buckets = analysis.token_usage.buckets
    assert buckets[UsageBucket.INPUT] == Metric(100, Precision.ESTIMATED, _UNREPORTED)
    assert buckets[UsageBucket.OUTPUT] == Metric(20, Precision.ESTIMATED, _UNREPORTED)


def test_what_a_failed_request_reported_counts_but_never_as_exact(tmp_path) -> None:
    """A cut-off stream reports its input and a placeholder output: the total is a lower bound."""
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.exchange_usage("b" * 32, 0, NS, {"input_total": 500, "output_total": 1}, outcome="error")
    log.exchange_usage("a" * 32, NS, 2 * NS, {"input_total": 100, "output_total": 20})
    log.add("turn.finished", 2 * NS, payload={"end_reason": "completed", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    assert analysis.turns[0].usage_tokens == Metric(621, Precision.ESTIMATED, _UNREPORTED)
    assert analysis.token_usage.buckets[UsageBucket.INPUT] == Metric(600, Precision.ESTIMATED, _UNREPORTED)


@pytest.mark.parametrize("reported", [{"input_total": 500}, {"output_total": 1}])
def test_a_failed_request_missing_a_total_counts_as_unreported(tmp_path, reported: dict[str, int]) -> None:
    """A failed request's half-reported usage must not take the whole turn's total away."""
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.exchange_usage("b" * 32, 0, NS, reported, outcome="error")
    log.exchange_usage("a" * 32, NS, 2 * NS, {"input_total": 100, "output_total": 20})
    log.add("turn.finished", 2 * NS, payload={"end_reason": "completed", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    assert analyze_trajectory(path).turns[0].usage_tokens == Metric(120, Precision.ESTIMATED, _UNREPORTED)


def test_successful_request_without_usage_still_leaves_totals_missing(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    _failed_exchange(log, "b" * 32, 0, NS, outcome="success")
    log.exchange_usage("a" * 32, NS, 2 * NS, {"input_total": 100, "output_total": 20})
    log.add("turn.finished", 2 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    usage = analyze_trajectory(path).turns[0].usage_tokens

    assert (usage.value, usage.precision) == (None, Precision.MISSING)


def test_retry_amplification_counts_reported_retries_when_one_failed_silently(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    retry_id = "c" * 32
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add("retry.scheduled", NS, operation_id=retry_id, payload={"retry_mode": "run"})
    log.add("retry.started", 2 * NS, operation_id=retry_id, payload={"retry_mode": "run"})
    _failed_exchange(log, "b" * 32, 2 * NS, 3 * NS, outcome="error", parent_operation_id=retry_id)
    log.exchange_usage("a" * 32, 3 * NS, 4 * NS, {"input_total": 50, "output_total": 10}, parent_operation_id=retry_id)
    log.add("turn.finished", 4 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    validation = analyze_trajectory(path).validation

    assert validation is not None
    assert validation.retry_amplification_tokens == Metric(60, Precision.ESTIMATED, _UNREPORTED)


def test_retry_amplification_with_a_failed_retrys_usage_is_a_lower_bound(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    retry_id = "c" * 32
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add("retry.scheduled", NS, operation_id=retry_id, payload={"retry_mode": "run"})
    log.add("retry.started", 2 * NS, operation_id=retry_id, payload={"retry_mode": "run"})
    log.exchange_usage(
        "b" * 32, 2 * NS, 3 * NS, {"input_total": 50, "output_total": 1}, outcome="error", parent_operation_id=retry_id
    )
    log.add("turn.finished", 3 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    validation = analyze_trajectory(path).validation

    assert validation is not None
    assert validation.retry_amplification_tokens == Metric(51, Precision.ESTIMATED, _UNREPORTED)
