# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How a compaction pass is scheduled: what starts it, where it stops and when it escalates.

A pass runs in one of three modes, fixed when it starts.  Usage at the trigger
starts a hard pass; a turn that ended above the force line books a
turn-boundary pass for the next request; a provider overflow forces a recovery
pass.  Every pass aims for the target, and a later phase starts only while
usage is still above the satisfied line, so a pass that has made room never
does a little more work on every following request.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
import logging
from typing import Any

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.kernel import (
    EXCLUDED_KEY,
    CompactionCallContext,
    Message,
    estimate_message_tokens,
    included_token_count,
)
from chrys.service.agent_middleware.system_reminder import wrap_system_reminder
from chrys.service.context.compaction import CompactionInfo, PreCompactInfo, UnifiedContextStrategy
from chrys.service.context.compaction.budgets import BOUNDARY_SUSPENDED_STATE_KEY, TURN_BOUNDARY_PENDING_STATE_KEY
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.state.serializers import deserialize_state, serialize_state
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _async_appender,
    _build_multi_turn,
    _build_tool_group,
    _estimate_tokens,
    _has_call_id,
    _make_strategy,
    _user,
    _wire_view,
)
from tests.service.trajectory._fakes import FakeSink, make_context

# The helper's defaults: trigger 0.85 and target 0.50 put the satisfied line
# near 0.617 and the force line near 0.733.
_EVERY_GATE_OPEN = {"target_pct": 0.01, "satisfied_pct": 0.01, "force_pct": 0.01}


def _window(messages: list[Message], usage: float) -> int:
    """The context window at which *messages* occupy *usage* of it."""
    return round(_estimate_tokens(messages) / usage)


def _usage(strategy: UnifiedContextStrategy, messages: list[Message]) -> float:
    return strategy._usage_pct(included_token_count(messages))


def _started_triggers(sink: FakeSink) -> list[str]:
    return [event.payload["trigger"] for event in sink.of_type(EventType.COMPACTION_STARTED)]


def _turn_touched(messages: list[Message], turn_index: int, groups: int = 3) -> bool:
    """Whether any tool group of the 0-based *turn_index* built by ``_build_multi_turn`` was summarised or removed."""
    return any(
        message.additional_properties.get(EXCLUDED_KEY, False)
        for message in messages
        for group in range(groups)
        if _has_call_id(message, f"t{turn_index}_call_{group}")
    )


def _ballast_turns(turns: int, *, text_chars: int, tool_chars: int, current_tool_chars: int) -> list[Message]:
    """Finished turns of plain text plus one tool result each, then a turn in progress with one tool result.

    Only a tool result can be summarised or removed without provider state,
    so the three sizes decide how far each phase can bring usage down.
    """
    messages: list[Message] = []
    for turn in range(turns):
        messages.append(_user(f"Turn {turn + 1} request " + "word " * (text_chars // 5)))
        messages.extend(_build_tool_group(f"t{turn}_call_0", "search", "x" * tool_chars))
        messages.append(_assistant_text(f"Turn {turn + 1} answer"))
    messages.append(_user("Current request"))
    messages.extend(_build_tool_group("current_call_0", "read_file", "y" * current_tool_chars))
    return messages


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def test_default_lines_split_the_span_between_target_and_trigger_in_thirds() -> None:
    strategy = _make_strategy(trigger_pct=0.80, target_pct=0.50)

    assert strategy.satisfied_pct == pytest.approx(0.60)
    assert strategy.force_pct == pytest.approx(0.70)


@pytest.mark.parametrize(
    ("satisfied_pct", "force_pct"),
    [(0.40, 0.70), (0.60, 0.90), (0.70, 0.60)],
    ids=["satisfied-below-target", "force-above-trigger", "force-below-satisfied"],
)
def test_lines_out_of_order_are_rejected(satisfied_pct: float, force_pct: float) -> None:
    with pytest.raises(ValueError, match="satisfied_pct and force_pct"):
        _make_strategy(trigger_pct=0.85, target_pct=0.50, satisfied_pct=satisfied_pct, force_pct=force_pct)


# ---------------------------------------------------------------------------
# What starts a pass
# ---------------------------------------------------------------------------


async def test_below_the_trigger_nothing_runs_until_a_pass_is_booked() -> None:
    messages = _build_multi_turn(4)
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.80))
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        assert await strategy(messages) is False
        assert _started_triggers(sink) == []

        assert strategy.request_turn_boundary_pass() is True
        assert await strategy(messages) is True
        # The booking is spent: the next request is back to the trigger.
        assert await strategy(messages) is False

    assert _started_triggers(sink) == ["turn_boundary"]
    assert _usage(strategy, messages) <= strategy.target_pct


async def test_a_booked_pass_at_the_trigger_runs_as_a_hard_pass() -> None:
    """Usage at the trigger lifts the boundary pass's limits: the newest completed turn is compacted too."""
    messages = _build_multi_turn(4)
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.90), **_EVERY_GATE_OPEN)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.request_turn_boundary_pass()
        assert await strategy(messages) is True

    assert _started_triggers(sink) == ["usage_threshold"]
    assert _turn_touched(messages, 2)


async def test_an_overflow_outranks_both_other_reasons() -> None:
    messages = _build_multi_turn(4)
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.90))
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.request_turn_boundary_pass()
        strategy.note_context_overflow()
        assert await strategy(messages) is True
        assert await strategy(messages) is False

    # One pass answered all three reasons.
    assert _started_triggers(sink) == ["context_overflow"]


async def test_booking_is_refused_while_compaction_is_off() -> None:
    messages = _build_multi_turn(4)
    strategy = UnifiedContextStrategy(max_context_tokens=_window(messages, 0.80), compaction_enabled=False)

    assert strategy.request_turn_boundary_pass() is False
    assert await strategy(messages) is False


# ---------------------------------------------------------------------------
# Turn-boundary pass
# ---------------------------------------------------------------------------


async def test_a_boundary_pass_never_touches_the_newest_completed_turn_or_the_current_one() -> None:
    """Even with every gate open: the turn the user just read stays whole, and Phase 4 never runs."""
    messages = _build_multi_turn(4)
    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=_window(messages, 0.80),
        on_compaction=_async_appender(received),
        **_EVERY_GATE_OPEN,
    )

    strategy.request_turn_boundary_pass()
    assert await strategy(messages) is True

    assert {info.phase for info in received} == {"phase1", "phase2"}
    assert {number for info in received for number in info.turn_numbers} == {1, 2}
    assert _turn_touched(messages, 0)
    assert _turn_touched(messages, 1)
    assert not _turn_touched(messages, 2)
    assert not _turn_touched(messages, 3)


async def test_a_boundary_pass_never_folds_the_newest_completed_turn() -> None:
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    for turn in range(1, 4):
        state["messages"].append(_user(f"Turn {turn} request " + "x " * 1500))
        state["messages"].append(_assistant_text(f"Turn {turn} answer " + "y " * 1500))
        CompressibleHistoryProvider.insert_marker(state, turn)
    wire = [*_wire_view(state), _user("Turn 4 request")]
    folds: list = []
    strategy = _make_strategy(
        max_context_tokens=_window(wire, 0.80),
        on_compress=_async_appender(folds),
        **_EVERY_GATE_OPEN,
    )
    strategy.bind_state(state)

    strategy.request_turn_boundary_pass()
    assert await strategy(wire) is True

    assert [info.turn_range for info in folds] == [(1, 1), (2, 2)]
    assert any(
        "Turn 3 answer" in message.text for message in wire if not message.additional_properties.get(EXCLUDED_KEY)
    )


async def test_a_boundary_pass_with_nothing_it_may_touch_is_not_reported_as_a_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The request was below the trigger: no room was needed, so freeing none is not a failure."""
    messages = [
        _user("Turn 1 request"),
        *_build_tool_group("recent_call", "read_file", "y" * 60_000),
        _assistant_text("Turn 1 answer"),
        _user("Turn 2 request"),
    ]
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.80))
    sink = FakeSink()

    with trajectory_scope(make_context(sink)), caplog.at_level(logging.WARNING):
        strategy.request_turn_boundary_pass()
        assert await strategy(messages) is False

    assert _started_triggers(sink) == ["turn_boundary"]
    assert sink.of_type(EventType.COMPACTION_SKIPPED) == []
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert strategy.request_turn_boundary_pass() is False


async def test_a_booking_found_with_compaction_off_is_dropped_without_a_skip() -> None:
    """The session booked the pass under an agent that compacts; this one does not."""
    messages = _build_multi_turn(4)
    state = _empty_state()
    state[TURN_BOUNDARY_PENDING_STATE_KEY] = True
    strategy = _bound(_make_strategy(max_context_tokens=_window(messages, 0.80), compaction_enabled=False), state)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        assert await strategy(messages) is False

    assert sink.of_type(EventType.COMPACTION_SKIPPED) == []
    assert _started_triggers(sink) == []
    assert TURN_BOUNDARY_PENDING_STATE_KEY not in state


async def test_a_boundary_pass_that_ends_above_the_satisfied_line_suspends_booking() -> None:
    """Nearly everything sits in the newest completed turn, which the pass may not touch."""
    messages = [
        _user("Turn 1 request"),
        *_build_tool_group("old_call", "search", "x" * 3000),
        _assistant_text("Turn 1 answer"),
        _user("Turn 2 request"),
        *_build_tool_group("recent_call", "read_file", "y" * 60_000),
        _assistant_text("Turn 2 answer"),
        _user("Turn 3 request"),
    ]
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.80))
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.request_turn_boundary_pass()
        assert await strategy(messages) is True
        assert _usage(strategy, messages) > strategy.satisfied_pct

        # Suspended: the same turns would only be worked over again.
        assert strategy.request_turn_boundary_pass() is False
        assert await strategy(messages) is False

        # Usage at or below the satisfied line (here: a shorter history) lifts it.
        shorter = [message for message in messages if not _has_call_id(message, "recent_call")]
        assert await strategy(shorter) is False
        assert strategy.request_turn_boundary_pass() is True

    assert _started_triggers(sink) == ["turn_boundary"]


async def test_a_booking_is_dropped_once_usage_is_at_or_below_the_satisfied_line() -> None:
    messages = _build_multi_turn(4)
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.55))

    strategy.request_turn_boundary_pass()

    assert await strategy(messages) is False
    assert not _turn_touched(messages, 0)
    assert not strategy._turn_boundary_pending


# ---------------------------------------------------------------------------
# What a pass reports
# ---------------------------------------------------------------------------


async def test_a_pass_says_what_the_trigger_looked_at_beside_what_the_messages_hold() -> None:
    """The trigger judges the whole request; the messages alone can sit below the line it crossed."""
    messages = _build_multi_turn(4)
    window = _window(messages, 0.80)
    strategy = _make_strategy(max_context_tokens=window)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        assert await strategy(messages, CompactionCallContext(request_overhead_tokens=window // 10)) is True

    started = sink.only(EventType.COMPACTION_STARTED).payload
    assert started["trigger"] == "usage_threshold"
    assert started["max_context_tokens"] == window
    assert started["trigger_tokens"] == round(0.85 * window)
    assert started["tokens_before"] < started["trigger_tokens"] <= started["estimated_input_tokens"]
    assert started["estimated_input_tokens"] == started["tokens_before"] + window // 10


async def test_each_fold_of_a_pass_reports_the_request_it_left() -> None:
    """Fold by fold the figures chain, so what the folds freed adds up to what the pass freed."""
    state = _empty_state()
    for turn in range(1, 7):
        state["messages"].extend(
            [_user(f"Turn {turn} request " + "word " * 400), _assistant_text(f"Turn {turn} answer " + "word " * 400)]
        )
        CompressibleHistoryProvider.insert_marker(state, turn)
    wire = [*_wire_view(state), _user("Current request")]
    strategy = _bound(_make_strategy(max_context_tokens=_window(wire, 0.95), **_EVERY_GATE_OPEN), state)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        assert await strategy(wire) is True

    folds = [
        event.payload
        for event in sink.of_type(EventType.COMPACTION_PHASE_FINISHED)
        if event.payload["phase"] == "phase3"
    ]
    finished = sink.only(EventType.COMPACTION_FINISHED).payload
    assert len(folds) > 2
    for earlier, later in itertools.pairwise(folds):
        assert earlier["tokens_after"] == later["tokens_before"]
    assert folds[-1]["tokens_after"] == finished["tokens_after"] == included_token_count(wire)
    assert sum(fold["tokens_before"] - fold["tokens_after"] for fold in folds) == (
        finished["tokens_before"] - finished["tokens_after"]
    )


# A message new to a request is counted with the id the request gives it.
_ID_TOKENS = 2


async def test_a_fold_after_the_run_counts_the_summary_that_replaces_the_turn() -> None:
    """There is no request to count then: the figure is worked out, and the summary is part of it."""
    messages = [_user("Turn one request " * 200), _assistant_text("Turn one answer " * 200), _user("Turn two request")]
    state: dict[str, Any] = {"messages": messages[:2], "compressed_msgs": [], "turn_counter": 1}
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    strategy = _bound(_make_strategy(max_context_tokens=100 * _estimate_tokens(list(messages))), state)
    assert await strategy(messages) is False
    folded = _estimate_tokens(messages[:2])
    summary = "Turn one summary. " * 40
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.queue_compression(marker_id, summary)
        assert await strategy.flush_pending_compressions()

    finished = sink.only(EventType.COMPACTION_FINISHED).payload
    # What stands in place of the turn is the summary message, which holds more than the summary's text.
    [summary_message] = _wire_view(state)
    summary_tokens = estimate_message_tokens(summary_message, strategy.tokenizer)
    assert summary_tokens > strategy.tokenizer.count_tokens(summary)
    assert finished["tokens_before"] - finished["tokens_after"] == folded - summary_tokens
    # The next request gives that message an id, which the figure could not count.
    next_request = _estimate_tokens([*_wire_view(state), messages[2]])
    assert 0 <= next_request - finished["tokens_after"] <= _ID_TOKENS


async def test_folds_after_the_run_each_start_from_what_the_one_before_left() -> None:
    state = _empty_state()
    marker_ids: list[str] = []
    for turn in (1, 2):
        state["messages"].extend([_user(f"Turn {turn} request " * 200), _assistant_text(f"Turn {turn} answer " * 200)])
        marker_ids.append(CompressibleHistoryProvider.insert_marker(state, turn))
    wire = [*_wire_view(state), _user("Turn three request")]
    strategy = _bound(_make_strategy(max_context_tokens=100 * _estimate_tokens(list(wire))), state)
    assert await strategy(wire) is False
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        for turn, marker_id in enumerate(marker_ids, start=1):
            strategy.queue_compression(marker_id, f"Turn {turn} summary. " * 40)
        assert await strategy.flush_pending_compressions()

    first, second = (event.payload for event in sink.of_type(EventType.COMPACTION_FINISHED))
    assert first["tokens_after"] == second["tokens_before"]
    assert first["tokens_before"] > first["tokens_after"] > second["tokens_after"]
    next_request = _estimate_tokens([*_wire_view(state), wire[-1]])
    assert 0 <= next_request - second["tokens_after"] <= 2 * _ID_TOKENS


async def test_a_fold_after_the_run_keeps_the_catalog_it_sends_again() -> None:
    """The catalog rides the folded user message, and the next request puts it back: those tokens were not freed."""
    catalog = "<available_skills>\n  <name>review</name>\n</available_skills>"
    opener = _user("Turn one request")
    opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY] = [
        {"kind": "catalog", "text": catalog, "name": "skills"}
    ]
    state = _empty_state()
    state["messages"].extend([opener, _assistant_text("Turn one answer " * 100)])
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    wire = [*_wire_view(state), _user("Turn two request")]
    strategy = _bound(_make_strategy(max_context_tokens=100 * _estimate_tokens(list(wire))), state)
    assert await strategy(wire) is False
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.queue_compression(marker_id, "Turn one summary. " * 20)
        assert await strategy.flush_pending_compressions()

    finished = sink.only(EventType.COMPACTION_FINISHED).payload
    # What the fold really left: the summary in place of the turn, plus the catalog re-sent beside it, counted as
    # the serialized text content it rides on.
    catalog_tokens = strategy.tokenizer.count_tokens(json.dumps(wrap_system_reminder(catalog), ensure_ascii=False))
    assert catalog_tokens > 0
    [summary_message] = _wire_view(state)
    summary_tokens = estimate_message_tokens(summary_message, strategy.tokenizer)
    freed = finished["tokens_before"] - finished["tokens_after"]
    assert (
        freed == _estimate_tokens([opener, _assistant_text("Turn one answer " * 100)]) - summary_tokens - catalog_tokens
    )


async def test_a_fold_after_the_run_frees_a_catalog_a_later_message_has_replaced() -> None:
    """The surviving message holds the newer version, so the next request sends nothing back for the folded one."""
    older = "<available_skills>\n  <name>review</name>\n</available_skills>"
    newer = "<available_skills>\n  <name>review</name>\n  <name>deploy</name>\n</available_skills>"
    opener = _user("Turn one request")
    opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY] = [
        {"kind": "catalog", "text": older, "name": "skills"}
    ]
    later = _user("Turn two request")
    later.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY] = [
        {"kind": "catalog", "text": newer, "name": "skills"}
    ]
    answer = _assistant_text("Turn one answer " * 100)
    state = _empty_state()
    state["messages"].extend([opener, answer])
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    folded = list(state["messages"])
    state["messages"].append(later)
    wire = [*_wire_view(state), _user("Turn three request")]
    strategy = _bound(_make_strategy(max_context_tokens=100 * _estimate_tokens(list(wire))), state)
    assert await strategy(wire) is False
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.queue_compression(marker_id, "Turn one summary. " * 20)
        assert await strategy.flush_pending_compressions()

    finished = sink.only(EventType.COMPACTION_FINISHED).payload
    [summary_message] = [message for message in _wire_view(state) if message is not later]
    summary_tokens = estimate_message_tokens(summary_message, strategy.tokenizer)
    freed = finished["tokens_before"] - finished["tokens_after"]
    # The folded message's catalog went with it: nothing is put back for it,
    # because the message the fold leaves already holds the newer version.
    assert freed == included_token_count(folded) - summary_tokens


async def test_a_fold_after_the_run_frees_only_what_the_last_request_still_carried() -> None:
    """A tool result an earlier phase took out is in the folded turn, and already out of the count."""
    state = _empty_state()
    state["messages"].extend(
        [
            _user("Turn one request"),
            *_build_tool_group("call_1", "read_file", "word " * 3000),
            _assistant_text("Turn one answer " * 50),
        ]
    )
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    state["messages"].extend([_user("Turn two request"), _assistant_text("Turn two answer " * 50)])
    CompressibleHistoryProvider.insert_marker(state, 2)
    wire = [*_wire_view(state), _user("Turn three request")]
    strategy = _bound(_make_strategy(max_context_tokens=_window(wire, 0.90)), state)
    assert await strategy(wire) is True
    assert any(message.additional_properties.get(EXCLUDED_KEY) for message in wire)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.queue_compression(marker_id, "Turn one summary. " * 20)
        assert await strategy.flush_pending_compressions()

    finished = sink.only(EventType.COMPACTION_FINISHED).payload
    assert finished["tokens_before"] == included_token_count(wire)
    sent_next = [m for m in [*_wire_view(state), wire[-1]] if not m.additional_properties.get(EXCLUDED_KEY)]
    assert 0 <= _estimate_tokens(sent_next) - finished["tokens_after"] <= _ID_TOKENS


async def test_a_fold_the_agent_asked_for_starts_from_this_requests_count_of_a_user_message() -> None:
    """A user message keeps the count of the last request it was in; this one sends it with more on it."""
    state = _empty_state()
    state["messages"].extend([_user("Turn one request " * 100), _assistant_text("Turn one answer " * 100)])
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    opener = _user("Turn two request")
    wire = [*_wire_view(state), opener]
    strategy = _bound(_make_strategy(max_context_tokens=100 * _estimate_tokens(list(wire))), state)
    assert await strategy(wire) is False
    # What a reminder does: another message in its place, on the same properties, with more to send.
    wire[-1] = Message(
        "user", [*opener.contents, "reminder " * 300], additional_properties=opener.additional_properties
    )
    sent = estimate_message_tokens(wire[-1], strategy.tokenizer)
    stale = included_token_count(wire)
    stale_opener = included_token_count([opener])
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.queue_compression(marker_id, "Turn one summary.")
        assert await strategy(wire) is True

    finished = sink.only(EventType.COMPACTION_FINISHED).payload
    assert sent > stale_opener
    assert finished["tokens_before"] == stale - stale_opener + sent


# ---------------------------------------------------------------------------
# The schedule outlives the strategy that made it
# ---------------------------------------------------------------------------


def _saved_and_loaded(state: dict[str, Any]) -> dict[str, Any]:
    """*state* as another process finds it: through the session file's JSON."""
    return deserialize_state(json.loads(json.dumps(serialize_state(state))))


def _bound(strategy: UnifiedContextStrategy, state: dict[str, Any]) -> UnifiedContextStrategy:
    strategy.bind_state(state)
    return strategy


def _empty_state() -> dict[str, Any]:
    return {"messages": [], "compressed_msgs": [], "turn_counter": 0}


async def test_a_booking_is_kept_by_the_next_process_of_the_session() -> None:
    """One process per turn: the turn that books the pass and the request that runs it never share a strategy."""
    messages = _build_multi_turn(4)
    window = _window(messages, 0.80)
    state = _empty_state()
    assert _bound(_make_strategy(max_context_tokens=window, **_EVERY_GATE_OPEN), state).request_turn_boundary_pass()

    resumed = _bound(_make_strategy(max_context_tokens=window, **_EVERY_GATE_OPEN), _saved_and_loaded(state))
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        assert await resumed(messages) is True

    assert _started_triggers(sink) == ["turn_boundary"]


async def test_a_completed_boundary_pass_is_not_run_again_by_the_next_process() -> None:
    messages = _build_multi_turn(4)
    window = _window(messages, 0.80)
    state = _empty_state()
    strategy = _bound(_make_strategy(max_context_tokens=window, **_EVERY_GATE_OPEN), state)
    strategy.request_turn_boundary_pass()
    assert await strategy(messages) is True

    assert TURN_BOUNDARY_PENDING_STATE_KEY not in _saved_and_loaded(state)


async def test_a_suspension_is_kept_by_the_next_process_of_the_session() -> None:
    messages = [
        _user("Turn 1 request"),
        *_build_tool_group("old_call", "search", "x" * 3000),
        _assistant_text("Turn 1 answer"),
        _user("Turn 2 request"),
        *_build_tool_group("recent_call", "read_file", "y" * 60_000),
        _assistant_text("Turn 2 answer"),
        _user("Turn 3 request"),
    ]
    window = _window(messages, 0.80)
    state = _empty_state()
    strategy = _bound(_make_strategy(max_context_tokens=window), state)
    strategy.request_turn_boundary_pass()
    assert await strategy(messages) is True
    assert _usage(strategy, messages) > strategy.satisfied_pct

    resumed = _bound(_make_strategy(max_context_tokens=window), _saved_and_loaded(state))

    assert resumed.request_turn_boundary_pass() is False


async def test_a_schedule_at_or_below_the_satisfied_line_leaves_nothing_in_the_state() -> None:
    messages = _build_multi_turn(4)
    state = _empty_state()
    strategy = _bound(_make_strategy(max_context_tokens=_window(messages, 0.55)), state)
    strategy.request_turn_boundary_pass()
    assert state[TURN_BOUNDARY_PENDING_STATE_KEY] is True

    assert await strategy(messages) is False

    assert TURN_BOUNDARY_PENDING_STATE_KEY not in state
    assert BOUNDARY_SUSPENDED_STATE_KEY not in state


async def test_a_rolled_back_attempt_gives_the_booking_back() -> None:
    """The pass ran inside the attempt and its work goes with it: the retry has to run it again."""
    messages = _build_multi_turn(4)
    state = _empty_state()
    strategy = _bound(_make_strategy(max_context_tokens=_window(messages, 0.80), **_EVERY_GATE_OPEN), state)
    strategy.request_turn_boundary_pass()
    snapshot = strategy.snapshot_retry_state()
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        assert await strategy(copy.deepcopy(messages)) is True
        assert TURN_BOUNDARY_PENDING_STATE_KEY not in state
        strategy.restore_retry_state(snapshot)
        assert state[TURN_BOUNDARY_PENDING_STATE_KEY] is True

        assert await strategy(messages) is True

    assert _started_triggers(sink) == ["turn_boundary", "turn_boundary"]


async def test_a_rolled_back_attempt_takes_its_suspension_with_it() -> None:
    messages = [
        _user("Turn 1 request"),
        *_build_tool_group("old_call", "search", "x" * 3000),
        _assistant_text("Turn 1 answer"),
        _user("Turn 2 request"),
        *_build_tool_group("recent_call", "read_file", "y" * 60_000),
        _assistant_text("Turn 2 answer"),
        _user("Turn 3 request"),
    ]
    state = _empty_state()
    strategy = _bound(_make_strategy(max_context_tokens=_window(messages, 0.80)), state)
    strategy.request_turn_boundary_pass()
    snapshot = strategy.snapshot_retry_state()
    assert await strategy(copy.deepcopy(messages)) is True
    assert state[BOUNDARY_SUSPENDED_STATE_KEY] is True

    strategy.restore_retry_state(snapshot)

    assert BOUNDARY_SUSPENDED_STATE_KEY not in state
    assert state[TURN_BOUNDARY_PENDING_STATE_KEY] is True


async def test_a_booking_made_before_the_first_bind_is_carried_into_the_state() -> None:
    state = _empty_state()
    strategy = _make_strategy()
    strategy.request_turn_boundary_pass()

    strategy.bind_state(state)

    assert state[TURN_BOUNDARY_PENDING_STATE_KEY] is True


async def test_a_rebound_strategy_takes_the_schedule_of_the_state_it_is_given() -> None:
    """A rewind hands over the state of an earlier turn, which had booked nothing."""
    strategy = _bound(_make_strategy(), _empty_state())
    strategy.request_turn_boundary_pass()

    strategy.bind_state(_empty_state())

    assert not strategy._turn_boundary_pending


# ---------------------------------------------------------------------------
# Escalation gates
# ---------------------------------------------------------------------------


async def test_a_hard_pass_at_or_below_the_satisfied_line_does_not_escalate() -> None:
    """Phase 1 misses the target by a little: removing the calls too would rewrite the prompt for no real gain."""
    messages = _ballast_turns(6, text_chars=3000, tool_chars=3000, current_tool_chars=3000)
    received: list[CompactionInfo] = []
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.86), on_compaction=_async_appender(received))

    assert await strategy(messages) is True

    assert strategy.target_pct < _usage(strategy, messages) <= strategy.satisfied_pct
    assert [info.phase for info in received] == ["phase1"]


async def test_a_hard_pass_above_the_satisfied_line_escalates_to_phase_2_but_not_phase_4() -> None:
    messages = _ballast_turns(6, text_chars=4200, tool_chars=1400, current_tool_chars=3000)
    received: list[CompactionInfo] = []
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.86), on_compaction=_async_appender(received))

    assert await strategy(messages) is True

    assert strategy.satisfied_pct < _usage(strategy, messages) <= strategy.force_pct
    assert [info.phase for info in received] == ["phase1", "phase2"]
    assert not any(m.additional_properties.get(EXCLUDED_KEY) for m in messages if _has_call_id(m, "current_call_0"))


async def test_a_hard_pass_left_above_the_force_line_drops_current_turn_work() -> None:
    messages = _ballast_turns(6, text_chars=2000, tool_chars=1400, current_tool_chars=60_000)
    received: list[CompactionInfo] = []
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.86), on_compaction=_async_appender(received))

    assert await strategy(messages) is True

    assert [info.phase for info in received] == ["phase1", "phase2", "phase4"]
    assert all(m.additional_properties.get(EXCLUDED_KEY) for m in messages if _has_call_id(m, "current_call_0"))


async def test_an_overflow_pass_has_no_gates() -> None:
    """The provider counted more than the estimate, so the estimate's lines say nothing about real room."""
    messages = _ballast_turns(6, text_chars=4200, tool_chars=1400, current_tool_chars=3000)
    received: list[CompactionInfo] = []
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.70), on_compaction=_async_appender(received))

    strategy.note_context_overflow()
    assert await strategy(messages) is True

    # The same history under a hard pass stops after Phase 2; here every phase runs.
    assert [info.phase for info in received] == ["phase1", "phase2", "phase4"]


async def test_an_overflow_pass_stops_lower_the_lower_it_started() -> None:
    """Real usage was at least at the trigger: the pass frees the share a hard pass would have freed there."""
    messages = _build_multi_turn(4)
    strategy = _make_strategy(max_context_tokens=_window(messages, 0.70))
    entry = _usage(strategy, messages)

    strategy.note_context_overflow()
    assert await strategy(messages) is True

    assert _usage(strategy, messages) <= strategy.target_pct * entry / strategy.trigger_pct < strategy.target_pct


# ---------------------------------------------------------------------------
# Interrupted passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), RuntimeError("hook failed")], ids=["cancel", "error"])
async def test_an_interrupted_boundary_pass_runs_again_as_a_boundary_pass(failure: BaseException) -> None:
    """The booking is its own flag: a retry must not come back as an overflow pass, which protects nothing."""
    messages = _build_multi_turn(4)
    failures = [failure]

    async def _fail_once(_info: PreCompactInfo) -> None:
        if failures:
            raise failures.pop()

    strategy = _make_strategy(
        max_context_tokens=_window(messages, 0.80),
        on_pre_compact=_fail_once,
        **_EVERY_GATE_OPEN,
    )
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.request_turn_boundary_pass()
        with pytest.raises(type(failure)):
            await strategy(messages)
        assert strategy._turn_boundary_pending
        assert not strategy._context_overflow_pending

        assert await strategy(messages) is True

    assert _started_triggers(sink) == ["turn_boundary", "turn_boundary"]
    assert not _turn_touched(messages, 2)
    assert not _turn_touched(messages, 3)


async def test_an_interrupted_overflow_pass_runs_again_as_an_overflow_pass() -> None:
    messages = _build_multi_turn(4)
    failures: list[BaseException] = [asyncio.CancelledError()]

    async def _fail_once(_info: PreCompactInfo) -> None:
        if failures:
            raise failures.pop()

    strategy = _make_strategy(max_context_tokens=_window(messages, 0.70), on_pre_compact=_fail_once)
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        strategy.note_context_overflow()
        with pytest.raises(asyncio.CancelledError):
            await strategy(messages)
        assert await strategy(messages) is True
        assert await strategy(messages) is False

    assert _started_triggers(sink) == ["context_overflow", "context_overflow"]


# ---------------------------------------------------------------------------
# Across many turns
# ---------------------------------------------------------------------------


async def test_a_long_tool_heavy_session_compacts_in_few_large_passes() -> None:
    """Each turn adds about 4% of the window; a pass makes room for many turns, not for one."""
    turn_chars = 6000
    window = round(_estimate_tokens(_build_multi_turn(1, groups_per_turn=3, result_size=turn_chars // 3)) / 0.04)
    strategy = _make_strategy(max_context_tokens=window)
    messages: list[Message] = []
    pass_turns: list[int] = []

    for turn in range(60):
        messages.append(_user(f"Turn {turn + 1} request"))
        for group in range(3):
            messages.extend(_build_tool_group(f"t{turn}_call_{group}", "search", "x" * (turn_chars // 3)))
        if await strategy(messages):
            pass_turns.append(turn)
        messages.append(_assistant_text(f"Turn {turn + 1} answer"))
        # The provider's check when the turn ends.
        if _usage(strategy, messages) > strategy.force_pct:
            strategy.request_turn_boundary_pass()

    assert 1 <= len(pass_turns) <= 6
    # A pass on consecutive turns is the treadmill this scheduling replaces.
    assert all(later - earlier >= 4 for earlier, later in itertools.pairwise(pass_turns))
    assert _usage(strategy, messages) < strategy.trigger_pct
