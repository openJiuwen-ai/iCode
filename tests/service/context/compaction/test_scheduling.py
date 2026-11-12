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
import itertools

import pytest

from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.kernel import EXCLUDED_KEY, Message, included_token_count
from chrys.service.context.compaction import CompactionInfo, PreCompactInfo, UnifiedContextStrategy
from chrys.service.context.providers.history import CompressibleHistoryProvider
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
