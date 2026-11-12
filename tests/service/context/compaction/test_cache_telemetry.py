# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Each response's cache read is judged against the request before it; head rewrites are counted per turn."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import pytest

from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.envelope import MeasurementSource
from chrys.foundation.trajectory.event_types import EventType, PromptCacheMissCause
from chrys.kernel import Message
from chrys.kernel.compaction import included_token_count
from chrys.service.context.compaction.cache_telemetry import (
    CACHE_IDLE_SECONDS,
    CACHE_MISS_MIN_TOKENS,
    THRASH_WINDOW_TURNS,
    PromptCacheMiss,
    PromptCacheTelemetry,
)
from chrys.service.context.middleware.usage import UsageTrackingMiddleware
from chrys.service.context.providers.history import CompressibleHistoryProvider
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _build_multi_turn,
    _build_single_turn,
    _build_tool_group,
    _estimate_tokens,
    _forced_phase4,
    _make_strategy,
    _user,
)
from tests.service.trajectory._fakes import FakeSink, make_context

if TYPE_CHECKING:
    from chrys.service.context.compaction.events import CompactionInfo, PreCompactInfo

_STRATEGY_LOGGER = "chrys.service.context.compaction.strategy"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def _warmed(clock: _Clock | None = None, *, input_tokens: int = 50_000) -> PromptCacheTelemetry:
    """A tracker whose provider has shown a cache read, with a request of *input_tokens* behind it."""
    telemetry = PromptCacheTelemetry(clock=clock or _Clock())
    assert telemetry.observe_response(input_tokens=input_tokens - 2_000, cache_read_tokens=0) is None
    assert telemetry.observe_response(input_tokens=input_tokens, cache_read_tokens=input_tokens - 2_000) is None
    return telemetry


def test_a_request_that_read_the_one_before_it_from_the_cache_is_no_miss() -> None:
    telemetry = _warmed()

    assert telemetry.observe_response(input_tokens=53_000, cache_read_tokens=50_000) is None
    # A read may stop short of the previous request by one cache block.
    assert telemetry.observe_response(input_tokens=56_000, cache_read_tokens=53_000 - CACHE_MISS_MIN_TOKENS) is None


def test_a_request_that_read_less_than_the_one_before_it_sent_is_a_miss_of_unknown_cause() -> None:
    telemetry = _warmed()

    miss = telemetry.observe_response(input_tokens=53_000, cache_read_tokens=50_000 - CACHE_MISS_MIN_TOKENS - 1)

    assert miss == PromptCacheMiss(
        cause=PromptCacheMissCause.UNKNOWN,
        missed_tokens=CACHE_MISS_MIN_TOKENS + 1,
        input_tokens=53_000,
        previous_input_tokens=50_000,
        cache_read_tokens=50_000 - CACHE_MISS_MIN_TOKENS - 1,
    )


def test_only_the_first_request_after_a_rewrite_is_put_down_to_compaction() -> None:
    telemetry = _warmed()
    telemetry.note_rewrite(head=False, turn=None)

    first = telemetry.observe_response(input_tokens=30_000, cache_read_tokens=8_000)
    second = telemetry.observe_response(input_tokens=33_000, cache_read_tokens=0)

    assert first is not None
    assert second is not None
    assert (first.cause, second.cause) == (PromptCacheMissCause.COMPACTION, PromptCacheMissCause.UNKNOWN)
    # A compacted request is shorter than the one before it: it can miss no more than it sent.
    assert first.missed_tokens == 30_000 - 8_000


def test_a_miss_after_a_pause_longer_than_the_cache_lives_is_put_down_to_the_pause() -> None:
    clock = _Clock()
    telemetry = _warmed(clock)

    clock.now += CACHE_IDLE_SECONDS
    in_time = telemetry.observe_response(input_tokens=53_000, cache_read_tokens=0)
    clock.now += CACHE_IDLE_SECONDS + 1
    expired = telemetry.observe_response(input_tokens=56_000, cache_read_tokens=0)

    assert in_time is not None
    assert expired is not None
    assert (in_time.cause, expired.cause) == (PromptCacheMissCause.UNKNOWN, PromptCacheMissCause.IDLE)


def test_the_pause_is_between_two_requests_not_the_time_an_answer_took() -> None:
    """The provider decided hit or miss when the request arrived; what it generated after is no pause."""
    clock = _Clock()
    telemetry = _warmed(clock)

    telemetry.request_prepared()
    clock.now += CACHE_IDLE_SECONDS + 100
    slow_answer = telemetry.observe_response(input_tokens=53_000, cache_read_tokens=0)
    # The next request follows at once, but the one before it was sent long ago.
    telemetry.request_prepared()
    after_slow_answer = telemetry.observe_response(input_tokens=56_000, cache_read_tokens=0)

    assert slow_answer is not None
    assert after_slow_answer is not None
    assert (slow_answer.cause, after_slow_answer.cause) == (PromptCacheMissCause.UNKNOWN, PromptCacheMissCause.IDLE)


async def test_the_time_a_request_took_to_prepare_is_no_pause_either() -> None:
    """A hook or a note can take minutes before the request goes out; the next one follows it, not them."""
    clock = _Clock()

    async def _slow_hook(_info: PreCompactInfo) -> None:
        clock.now += CACHE_IDLE_SECONDS + 1

    messages = _build_multi_turn(3, groups_per_turn=3, result_size=1000)
    strategy = _forced_phase4(messages, on_pre_compact=_slow_hook)
    strategy._cache_telemetry = PromptCacheTelemetry(clock=clock)
    middleware = UsageTrackingMiddleware(compaction_strategy=strategy)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        middleware._handle_usage(_usage(40_000, 0))
        middleware._handle_usage(_usage(44_000, 40_000))
        started = clock.now
        assert await strategy(messages)
        assert clock.now - started > CACHE_IDLE_SECONDS
        middleware._handle_usage(_usage(20_000, 2_000))
        clock.now += 1
        assert not await strategy([_user("And the lexer?")])
        middleware._handle_usage(_usage(23_000, 0))

    assert [event.payload["cause"] for event in sink.of_type(EventType.PROMPT_CACHE_MISSED)] == [
        PromptCacheMissCause.COMPACTION,
        PromptCacheMissCause.UNKNOWN,
    ]


def test_a_provider_that_never_reads_from_a_cache_has_none_to_miss() -> None:
    telemetry = PromptCacheTelemetry(clock=_Clock())

    for input_tokens in (40_000, 45_000, 50_000):
        assert telemetry.observe_response(input_tokens=input_tokens, cache_read_tokens=0) is None

    # Once it has read from one, the next request that does not is a miss.
    assert telemetry.observe_response(input_tokens=55_000, cache_read_tokens=50_000) is None
    assert telemetry.observe_response(input_tokens=60_000, cache_read_tokens=0) is not None


def test_usage_that_does_not_say_what_the_request_sent_is_no_observation() -> None:
    """A failed response can report its output alone; as a request of nothing it would hide the next miss."""
    telemetry = _warmed(input_tokens=50_000)
    telemetry.note_rewrite(head=False, turn=None)

    assert telemetry.observe_response(input_tokens=0, cache_read_tokens=0) is None
    miss = telemetry.observe_response(input_tokens=20_000, cache_read_tokens=2_000)

    assert miss == PromptCacheMiss(
        cause=PromptCacheMissCause.COMPACTION,
        missed_tokens=18_000,
        input_tokens=20_000,
        previous_input_tokens=50_000,
        cache_read_tokens=2_000,
    )


def test_a_session_rewound_to_an_earlier_turn_starts_the_count_afresh() -> None:
    """Turns counted after the one rewound to no longer exist, and would never age out of the window."""
    telemetry = PromptCacheTelemetry(clock=_Clock())

    results = [_rewrite_in_a_new_request(telemetry, turn) for turn in (20, 21, 3, 14, 15)]

    assert results == [None] * 5


@pytest.mark.parametrize(
    ("turns", "reported"),
    [
        ((1, 5, 10), (1, 5, 10)),
        ((4, 4, 4), (4, 4, 4)),
        ((1, 5, 11), None),
        ((1, 12, 23), None),
    ],
    ids=["within-ten-turns", "same-turn", "first-falls-out", "far-apart"],
)
def test_three_head_rewrites_within_ten_turns_are_thrash(
    turns: tuple[int, ...], reported: tuple[int, ...] | None
) -> None:
    telemetry = PromptCacheTelemetry(clock=_Clock())

    results = [_rewrite_in_a_new_request(telemetry, turn) for turn in turns]

    assert THRASH_WINDOW_TURNS == 10
    assert results == [None, None, reported]


def _rewrite_in_a_new_request(telemetry: PromptCacheTelemetry, turn: int) -> tuple[int, ...] | None:
    telemetry.begin_request()
    return telemetry.note_rewrite(head=True, turn=turn)


def test_thrash_is_reported_once_and_counted_afresh() -> None:
    telemetry = PromptCacheTelemetry(clock=_Clock())
    for turn in (1, 2):
        assert _rewrite_in_a_new_request(telemetry, turn) is None
    assert _rewrite_in_a_new_request(telemetry, 3) == (1, 2, 3)

    assert _rewrite_in_a_new_request(telemetry, 4) is None
    assert _rewrite_in_a_new_request(telemetry, 5) is None
    assert _rewrite_in_a_new_request(telemetry, 6) == (4, 5, 6)


def test_the_steps_that_rewrite_one_request_count_once() -> None:
    """The provider is sent one changed prefix, whatever number of steps changed it."""
    telemetry = PromptCacheTelemetry(clock=_Clock())

    telemetry.begin_request()
    assert [telemetry.note_rewrite(head=True, turn=1) for _ in range(3)] == [None] * 3
    # A rewrite between two requests is carried by the next one, with that request's own.
    assert telemetry.note_rewrite(head=True, turn=1, before_next_request=True) is None
    telemetry.begin_request()
    assert [telemetry.note_rewrite(head=True, turn=2) for _ in range(3)] == [None] * 3

    assert _rewrite_in_a_new_request(telemetry, 3) == (1, 1, 3)


def test_a_current_turn_drop_and_a_rewrite_without_a_turn_number_are_not_counted_as_thrash() -> None:
    telemetry = PromptCacheTelemetry(clock=_Clock())

    results = [
        telemetry.note_rewrite(head=False, turn=1),
        telemetry.note_rewrite(head=False, turn=1),
        telemetry.note_rewrite(head=False, turn=1),
        _rewrite_without_a_turn(telemetry),
        _rewrite_without_a_turn(telemetry),
        _rewrite_without_a_turn(telemetry),
    ]

    assert results == [None] * 6


def _rewrite_without_a_turn(telemetry: PromptCacheTelemetry) -> tuple[int, ...] | None:
    telemetry.begin_request()
    return telemetry.note_rewrite(head=True, turn=None)


# ---------------------------------------------------------------------------
# The strategy reports what the tracker decides
# ---------------------------------------------------------------------------


def _usage(input_tokens: int, cache_read: int | None, **extra: Any) -> dict[str, Any]:
    usage: dict[str, Any] = {"input_token_count": input_tokens, "output_token_count": 10, **extra}
    if cache_read is not None:
        usage["cache_read_input_token_count"] = cache_read
    return usage


async def test_the_request_after_a_pass_is_recorded_as_that_compactions_miss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = _build_multi_turn(3, groups_per_turn=3, result_size=1000)
    strategy = _forced_phase4(messages)
    middleware = UsageTrackingMiddleware(compaction_strategy=strategy)
    sink = FakeSink()

    with caplog.at_level(logging.INFO, logger=_STRATEGY_LOGGER), trajectory_scope(make_context(sink)):
        middleware._handle_usage(_usage(40_000, 0))
        middleware._handle_usage(_usage(44_000, 40_000))
        assert sink.of_type(EventType.PROMPT_CACHE_MISSED) == []
        assert await strategy(messages)
        middleware._handle_usage(_usage(12_000, 3_000))
        middleware._handle_usage(_usage(15_000, 0))

    compaction, unknown = sink.of_type(EventType.PROMPT_CACHE_MISSED)
    assert compaction.payload == {
        "cause": PromptCacheMissCause.COMPACTION,
        "missed_tokens": 9_000,
        "input_tokens": 12_000,
        "previous_input_tokens": 44_000,
        "cache_read_tokens": 3_000,
    }
    assert unknown.payload["cause"] == PromptCacheMissCause.UNKNOWN
    assert unknown.payload["missed_tokens"] == 12_000
    assert {entry["source"] for entry in compaction.measurements.values()} == {MeasurementSource.PROVIDER}
    # The price of a compaction is expected; a miss nothing explains is a warning.
    misses = [record for record in caplog.records if record.getMessage().startswith("Prompt cache miss:")]
    assert [record.levelno for record in misses] == [logging.INFO, logging.WARNING]
    assert "cause=compaction, missed_tokens=9000" in misses[0].getMessage()


def test_a_provider_specific_cache_count_is_read_when_the_normalized_one_is_absent() -> None:
    strategy = _forced_phase4(_build_single_turn(2))
    middleware = UsageTrackingMiddleware(compaction_strategy=strategy)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        middleware._handle_usage(_usage(40_000, None))
        middleware._handle_usage(_usage(44_000, None, **{"prompt/cached_tokens": 40_000}))
        middleware._handle_usage(_usage(48_000, None, **{"prompt/cached_tokens": 44_000}))
        assert sink.of_type(EventType.PROMPT_CACHE_MISSED) == []
        middleware._handle_usage(_usage(52_000, None, **{"prompt/cached_tokens": 10_000}))

    assert sink.only(EventType.PROMPT_CACHE_MISSED).payload["missed_tokens"] == 38_000


def test_a_hosted_aggregate_is_not_judged() -> None:
    """Its input count sums the provider's internal passes: it says nothing about one request's prefix."""
    strategy = _forced_phase4(_build_single_turn(2))
    middleware = UsageTrackingMiddleware(compaction_strategy=strategy, use_local_context_estimate_for_hosted_usage=True)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        middleware._handle_usage(_usage(40_000, 0))
        middleware._handle_usage(_usage(44_000, 40_000))
        middleware._handle_usage(_usage(400_000, 0), use_local_context_estimate=True)
        # The aggregate was not taken for the previous request either.
        middleware._handle_usage(_usage(47_000, 44_000))

    assert sink.of_type(EventType.PROMPT_CACHE_MISSED) == []


def _head_rewriting_pass() -> list[Message]:
    messages = _build_multi_turn(3, groups_per_turn=3, result_size=1000)
    messages.pop()
    return messages


async def test_three_passes_that_rewrite_the_head_within_ten_turns_are_reported_as_thrash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = _forced_phase4(_head_rewriting_pass())
    state: dict[str, Any] = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    strategy.bind_state(state)
    sink = FakeSink()

    with caplog.at_level(logging.WARNING, logger=_STRATEGY_LOGGER), trajectory_scope(make_context(sink)):
        for completed_turns in (3, 7):
            state["turn_counter"] = completed_turns
            assert await strategy(_head_rewriting_pass())
        assert sink.of_type(EventType.COMPACTION_THRASH_DETECTED) == []
        state["turn_counter"] = 12
        assert await strategy(_head_rewriting_pass())

    thrash = sink.only(EventType.COMPACTION_THRASH_DETECTED)
    assert thrash.payload == {"rewrites": 3, "window_turns": 10, "turn_numbers": [3, 7, 12]}
    [warning] = [record for record in caplog.records if record.getMessage().startswith("Compaction thrash:")]
    assert "rewritten 3 times within 10 turns (after turns 3, 7, 12)" in warning.getMessage()


async def test_drops_of_the_current_turn_alone_are_not_thrash() -> None:
    """A long task has to drop its own older work now and then; the head stays as the provider cached it."""
    strategy = _forced_phase4(_build_single_turn(6, result_size=4000))
    strategy.bind_state({"messages": [], "compressed_msgs": [], "turn_counter": 1})
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        for _ in range(4):
            messages = _build_single_turn(6, result_size=4000)
            messages.pop()
            assert await strategy(messages)

    assert len(sink.of_type(EventType.COMPACTION_PHASE_FINISHED)) == 4
    assert sink.of_type(EventType.COMPACTION_THRASH_DETECTED) == []


async def test_a_fold_the_model_asked_for_reports_counts_of_the_request_it_folded() -> None:
    """Both figures are counts of the same list, newest messages included, so their difference is the fold's."""

    def _request() -> list[Message]:
        return [
            _user("Turn one request " * 200),
            _assistant_text("Turn one answer " * 200),
            _user("Turn two request"),
            *_build_tool_group("call_0", "read_file", "x" * 4000),
        ]

    # Counted on a copy: the request itself holds messages no call has counted yet.
    before = _estimate_tokens(_request())
    messages = _request()
    state: dict[str, Any] = {"messages": messages[:2], "compressed_msgs": [], "turn_counter": 0}
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    strategy = _make_strategy(max_context_tokens=100 * before)
    strategy.bind_state(state)
    strategy.queue_compression(marker_id, "Turn one summary")
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        assert await strategy(messages)

    started = sink.only(EventType.COMPACTION_STARTED)
    finished = sink.only(EventType.COMPACTION_FINISHED)
    assert started.payload["trigger"] == "agent_request"
    assert started.payload["tokens_before"] == finished.payload["tokens_before"] == before
    assert finished.payload["tokens_after"] == strategy.last_included_tokens
    assert 0 < finished.payload["tokens_after"] < before
    assert {entry["source"] for pointer, entry in finished.measurements.items() if "tokens" in pointer} == {
        MeasurementSource.LOCAL_TOKENIZER
    }


@pytest.mark.parametrize("after_run", [False, True], ids=["in-the-next-request", "after-the-run"])
async def test_the_request_after_a_fold_the_model_asked_for_is_that_folds_miss(after_run: bool) -> None:
    messages = [_user("Turn one request " * 200), _assistant_text("Turn one answer " * 200), _user("Turn two request")]
    state: dict[str, Any] = {"messages": messages[:2], "compressed_msgs": [], "turn_counter": 1}
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    strategy = _make_strategy(max_context_tokens=100 * _estimate_tokens(list(messages)))
    strategy.bind_state(state)
    middleware = UsageTrackingMiddleware(compaction_strategy=strategy)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        middleware._handle_usage(_usage(40_000, 0))
        middleware._handle_usage(_usage(44_000, 40_000))
        strategy.queue_compression(marker_id, "Turn one summary")
        if after_run:
            assert await strategy.flush_pending_compressions()
        else:
            assert await strategy(messages)
        middleware._handle_usage(_usage(20_000, 2_000))

    assert sink.only(EventType.PROMPT_CACHE_MISSED).payload["cause"] == PromptCacheMissCause.COMPACTION


async def test_a_fold_and_a_pass_in_one_request_are_one_rewrite_of_the_head() -> None:
    """The provider is sent one changed prefix, whatever number of steps changed it."""
    # Every line sits far below what the fold leaves, so the pass still runs after it.
    strategy = _make_strategy(
        max_context_tokens=_estimate_tokens(_head_rewriting_pass()),
        trigger_pct=0.02,
        target_pct=0.01,
        satisfied_pct=0.01,
        force_pct=0.01,
    )
    sink = FakeSink()

    async def _fold_then_pass(completed_turns: int) -> None:
        messages = _head_rewriting_pass()
        second_turn = [index for index, message in enumerate(messages) if message.role == "user"][1]
        state: dict[str, Any] = {"messages": messages[:second_turn], "compressed_msgs": [], "turn_counter": 0}
        marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
        state["turn_counter"] = completed_turns
        strategy.bind_state(state)
        strategy.queue_compression(marker_id, "Turn one summary")
        assert await strategy(messages)

    with trajectory_scope(make_context(sink)):
        await _fold_then_pass(3)
        await _fold_then_pass(7)
        # Each request both folded a turn and compacted what precedes the current one.
        triggers = [event.payload["trigger"] for event in sink.of_type(EventType.COMPACTION_STARTED)]
        assert triggers == ["agent_request", "usage_threshold", "agent_request", "usage_threshold"]
        head_phases = [
            event.payload["phase"]
            for event in sink.of_type(EventType.COMPACTION_PHASE_FINISHED)
            if event.payload["phase"] != 4
        ]
        assert len(head_phases) >= 2
        assert sink.of_type(EventType.COMPACTION_THRASH_DETECTED) == []
        strategy.bind_state({"messages": [], "compressed_msgs": [], "turn_counter": 9})
        assert await strategy(_head_rewriting_pass())

    assert sink.only(EventType.COMPACTION_THRASH_DETECTED).payload["turn_numbers"] == [3, 7, 9]


async def test_a_pass_cancelled_while_it_reports_a_change_still_explains_the_next_miss() -> None:
    """The change is kept when its notification is cancelled, so the miss it causes is still its own."""

    async def _cancelled(_info: CompactionInfo) -> None:
        raise asyncio.CancelledError

    messages = _build_multi_turn(3, groups_per_turn=3, result_size=1000)
    before = _estimate_tokens(list(messages))
    strategy = _forced_phase4(messages, on_compaction=_cancelled)
    middleware = UsageTrackingMiddleware(compaction_strategy=strategy)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        middleware._handle_usage(_usage(40_000, 0))
        middleware._handle_usage(_usage(44_000, 40_000))
        with pytest.raises(asyncio.CancelledError):
            await strategy(messages)
        # The first phase's change is in the list the next request is built from.
        assert included_token_count(messages) < before
        middleware._handle_usage(_usage(30_000, 2_000))

    assert sink.only(EventType.PROMPT_CACHE_MISSED).payload["cause"] == PromptCacheMissCause.COMPACTION


async def test_the_request_after_a_drop_of_the_current_turn_is_that_drops_miss() -> None:
    messages = _build_single_turn(6, result_size=4000)
    messages.pop()
    strategy = _forced_phase4(messages)
    middleware = UsageTrackingMiddleware(compaction_strategy=strategy)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        middleware._handle_usage(_usage(40_000, 0))
        middleware._handle_usage(_usage(44_000, 40_000))
        assert await strategy(messages)
        assert [event.payload["phase"] for event in sink.of_type(EventType.COMPACTION_PHASE_FINISHED)] == ["phase4"]
        middleware._handle_usage(_usage(20_000, 2_000))

    assert sink.only(EventType.PROMPT_CACHE_MISSED).payload["cause"] == PromptCacheMissCause.COMPACTION
