# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A current-turn drop keeps the newest work of the turn and drops what is older."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from chrys.kernel import EXCLUDED_KEY, Message
from chrys.service.context.compaction import MAX_DROP_ROUNDS_PER_TURN, UnifiedContextStrategy
from chrys.service.context.compaction.last_words_state import DropRoundBreakerState, ManifestEntry
from chrys.service.context.compaction.spill import SpillQuota
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _build_tool_group,
    _estimate_tokens,
    _has_call_id,
    _make_strategy,
    _user,
)
from tests.support.phase4_stubs import StubLastWordsGenerator, StubReminderMiddleware

_GROUP_WORDS = 2_000
_GROUP_TOKENS = _estimate_tokens(_build_tool_group("probe", "tool", "word " * _GROUP_WORDS))
"""One of ``_turn``'s tool groups, as the strategy counts it."""


def _turn(groups: int, *, words: int = _GROUP_WORDS) -> list[Message]:
    """A turn of *groups* equal tool groups and nothing after the last result, as a request finds it."""
    messages = [_user("Refactor the parser")]
    for index in range(groups):
        messages.extend(_build_tool_group(f"call_{index}", "read_file", "word " * words))
    return messages


def _window_keeping(groups: float) -> int:
    """A window whose recent-tail allowance (8% of it) holds *groups* of ``_turn``'s tool groups."""
    return int(groups * _GROUP_TOKENS / 0.08)


def _strategy(window: int, **kwargs: Any) -> UnifiedContextStrategy:
    """A strategy whose pass always reaches the current turn: every line sits far below the turn's size."""
    lines = {"trigger_pct": 0.02, "target_pct": 0.01, "satisfied_pct": 0.01, "force_pct": 0.01}
    return _make_strategy(max_context_tokens=window, **{**lines, **kwargs})


def _dropped_calls(messages: list[Message]) -> list[str]:
    return [
        content.call_id
        for message in messages
        if message.role == "assistant" and message.additional_properties.get(EXCLUDED_KEY, False)
        for content in message.contents
        if content.type == "function_call"
    ]


def _visible(messages: list[Message]) -> list[Message]:
    return [message for message in messages if not message.additional_properties.get(EXCLUDED_KEY, False)]


async def test_the_newest_groups_that_fit_the_allowance_stay_and_the_older_ones_go() -> None:
    generator = StubLastWordsGenerator(text="[note]")
    messages = _turn(6)
    strategy = _strategy(_window_keeping(2.5), last_words_generator=generator)

    assert await strategy(messages)

    assert _dropped_calls(messages) == ["call_0", "call_1", "call_2", "call_3"]
    kept = _visible(messages)
    assert [message.role for message in kept] == ["user", "assistant", "tool", "assistant", "tool"]
    assert _has_call_id(kept[1], "call_4")
    assert _has_call_id(kept[4], "call_5")
    # The note is written from the whole turn, the work that stays included.
    [call] = generator.calls
    assert sum(group.kind == "tool_call" for group in call["scoped_groups"]) == 6


async def test_the_kept_work_starts_at_a_tool_call_and_keeps_the_text_between_calls() -> None:
    messages = [
        _user("Refactor the parser"),
        *_build_tool_group("call_0", "read_file", "word " * _GROUP_WORDS),
        *_build_tool_group("call_1", "read_file", "word " * _GROUP_WORDS),
        _assistant_text("That was the old parser; now the new one."),
        *_build_tool_group("call_2", "read_file", "word " * 200),
        _assistant_text("One more file to check."),
        *_build_tool_group("call_3", "read_file", "word " * 200),
    ]
    strategy = _strategy(_window_keeping(0.9))

    assert await strategy(messages)

    assert _dropped_calls(messages) == ["call_0", "call_1"]
    # The text ahead of the first kept call introduces dropped work and goes with it.
    assert messages[5].additional_properties.get(EXCLUDED_KEY, False)
    assert [message.text for message in _visible(messages) if message.role == "assistant" and message.text] == [
        "One more file to check."
    ]


async def test_a_turn_that_fits_the_allowance_starts_no_round() -> None:
    generator = StubLastWordsGenerator()
    reminder = StubReminderMiddleware()
    messages = _turn(2)
    strategy = _strategy(_window_keeping(3), last_words_generator=generator, reminder_middleware=reminder)

    assert not await strategy(messages)

    assert generator.calls == []
    assert _dropped_calls(messages) == []
    assert reminder.last_words.get_drop_round_breaker() == DropRoundBreakerState()


async def test_a_drop_that_would_free_under_five_points_starts_no_round() -> None:
    """Such a round would spend a side call and a breaker attempt to rewrite the prefix for nothing."""
    generator = StubLastWordsGenerator()
    reminder = StubReminderMiddleware()
    older = _build_tool_group("call_0", "read_file", "word " * (_GROUP_WORDS * 3 // 4))
    messages = [
        _user("Refactor the parser"),
        *older,
        *_build_tool_group("call_1", "read_file", "word " * _GROUP_WORDS),
    ]
    window = 50_000
    # The newest group stays, the older one no longer fits beside it, and is under five points of the window.
    assert _GROUP_TOKENS < 0.08 * window < _GROUP_TOKENS + _estimate_tokens(older) < 0.05 * window + _GROUP_TOKENS
    strategy = _strategy(window, last_words_generator=generator, reminder_middleware=reminder)

    assert not await strategy(messages)

    assert generator.calls == []
    assert _dropped_calls(messages) == []
    assert reminder.last_words.get_drop_round_breaker() == DropRoundBreakerState()


async def test_an_overflow_pass_drops_even_what_would_free_under_five_points() -> None:
    """The provider refused the request: a little room may be all the resend needs."""
    generator = StubLastWordsGenerator(text="[note]")
    reminder = StubReminderMiddleware()
    messages = [_user("Refactor the parser"), *_build_tool_group("call_0", "read_file", "word " * _GROUP_WORDS)]
    window = 80_000
    # All the tool work of the turn is under five points of the window.
    assert _estimate_tokens(messages) < 0.05 * window
    strategy = _make_strategy(
        max_context_tokens=window,
        last_words_generator=generator,
        reminder_middleware=reminder,
        trigger_pct=0.90,
        target_pct=0.20,
        satisfied_pct=0.60,
        force_pct=0.70,
    )
    assert strategy.note_context_overflow()

    assert await strategy(messages)

    assert len(generator.calls) == 1
    assert _dropped_calls(messages) == ["call_0"]
    # A round the pass was allowed to run for little is not a failed one: two
    # of them must not leave the turn without the drop its next overflow needs.
    assert reminder.last_words.get_drop_round_breaker() == DropRoundBreakerState(attempts=1)


async def test_an_overflow_drop_that_frees_little_still_ends_a_run_of_failed_rounds() -> None:
    """A failed round, a committed drop, a failed round: the two failures are not in a row."""
    reminder = StubReminderMiddleware()
    reminder.last_words.set_drop_round_breaker(DropRoundBreakerState(attempts=1, consecutive_no_progress=1))
    messages = [_user("Refactor the parser"), *_build_tool_group("call_0", "read_file", "word " * _GROUP_WORDS)]
    window = 80_000
    assert _estimate_tokens(messages) < 0.05 * window
    strategy = _make_strategy(
        max_context_tokens=window,
        last_words_generator=StubLastWordsGenerator(text="[note]"),
        reminder_middleware=reminder,
        trigger_pct=0.90,
        target_pct=0.20,
        satisfied_pct=0.60,
        force_pct=0.70,
    )
    assert strategy.note_context_overflow()

    assert await strategy(messages)

    assert _dropped_calls(messages) == ["call_0"]
    breaker = reminder.last_words.get_drop_round_breaker()
    assert (breaker.attempts, breaker.consecutive_no_progress, breaker.disabled) == (2, 0, False)


async def test_an_overflow_pass_gives_up_kept_work_until_the_stop_point() -> None:
    messages = _turn(6)
    window = _window_keeping(2.5)
    strategy = _strategy(window, trigger_pct=0.90, target_pct=0.20, satisfied_pct=0.60, force_pct=0.70)
    # The estimate sits at a fifth of the window: only the provider's refusal makes the pass run.
    entry_usage = strategy._usage_pct(_estimate_tokens(messages))
    assert entry_usage < 0.25
    assert strategy.note_context_overflow()

    assert await strategy(messages)

    # Stop point: the target scaled by how far below the trigger the estimate was.
    stop = 0.20 * entry_usage / 0.90
    assert _dropped_calls(messages) == ["call_0", "call_1", "call_2", "call_3", "call_4"]
    assert strategy._usage_pct(strategy.last_included_tokens) <= stop
    # The two groups the allowance holds would have left the pass above it.
    assert strategy._usage_pct(2 * _GROUP_TOKENS) > stop


async def test_an_overflow_pass_makes_room_for_the_note_it_writes_too() -> None:
    """The stop point is for the request that goes out, and the note goes out with it."""
    note = "progress " * (_GROUP_WORDS // 2)
    messages = _turn(6)
    strategy = _strategy(
        _window_keeping(2.5),
        last_words_generator=StubLastWordsGenerator(text=note),
        trigger_pct=0.90,
        target_pct=0.20,
        satisfied_pct=0.60,
        force_pct=0.70,
    )
    stop = 0.20 * strategy._usage_pct(_estimate_tokens(messages)) / 0.90
    note_tokens = strategy.tokenizer.count_tokens(note)
    # The one group a short note leaves (the test above) no longer fits beside this one.
    assert strategy._usage_pct(_GROUP_TOKENS) < stop < strategy._usage_pct(_GROUP_TOKENS + note_tokens)
    assert strategy.note_context_overflow()

    assert await strategy(messages)

    assert _dropped_calls(messages) == [f"call_{index}" for index in range(6)]


async def test_the_same_turn_in_a_hard_pass_keeps_what_the_overflow_pass_gave_up() -> None:
    """The control for the test above: only an overflow pass spends the kept work."""
    messages = _turn(6)
    strategy = _strategy(_window_keeping(2.5))

    assert await strategy(messages)

    assert _dropped_calls(messages) == ["call_0", "call_1", "call_2", "call_3"]


async def test_a_newest_group_too_large_to_stay_leaves_both_ends_of_its_result_and_its_record(
    tmp_path: Path,
) -> None:
    reminder = StubReminderMiddleware()
    result = "HEAD-OF-OUTPUT " + "middle " * (3 * _GROUP_WORDS) + "exit code 1: TAIL-OF-OUTPUT"
    messages = [
        _user("Run the suite"),
        *_build_tool_group("call_0", "read_file", "word " * _GROUP_WORDS),
        *_build_tool_group("call_big", "shell", result, args={"command": "make test"}),
    ]
    strategy = _strategy(
        _window_keeping(1.5),
        reminder_middleware=reminder,
        spill_root=tmp_path,
        spill_quota=SpillQuota(),
        spill_session_id="oversized",
    )

    assert await strategy(messages)

    assert _dropped_calls(messages) == ["call_0", "call_big"]
    rows = {row["tool"]: row for row in reminder.last_words.get_last_words_manifest()}
    assert "excerpt" not in rows["read_file"]
    excerpt = rows["shell"]["excerpt"]
    assert excerpt.startswith("HEAD-OF-OUTPUT ")
    assert "exit code 1: TAIL-OF-OUTPUT" in excerpt
    assert len(excerpt) < 400
    # The request shows the excerpt next to the record that holds the whole result.
    [block] = reminder.last_words.render()
    record_name = Path(rows["shell"]["relative_path"]).name
    [line] = [line for line in block.splitlines() if record_name in line]
    assert "shell(" in line
    assert block.splitlines()[block.splitlines().index(line) + 1] == f"    excerpt: {' '.join(excerpt.split())}"
    assert result in (tmp_path / rows["shell"]["relative_path"]).read_text(encoding="utf-8")


def test_a_manifest_entry_keeps_its_excerpt_through_saved_state_and_old_state_has_none() -> None:
    entry = ManifestEntry(
        record_id="record-1",
        group_id="group-1",
        record_dir="compactions/dropped/turn001",
        relative_path="compactions/dropped/turn001/001_shell_00000001.md",
        turn=1,
        round=1,
        sequence=1,
        tool="shell",
        display_argument='command="make test"',
        outcome="ok",
        size_chars=40_000,
        excerpt="first lines ... last lines (40000 chars)",
    )

    state = entry.to_state()
    assert ManifestEntry.from_state(state) == entry

    legacy = {key: value for key, value in state.items() if key != "excerpt"}
    restored = ManifestEntry.from_state(legacy)
    assert restored is not None
    assert restored.excerpt == ""
    assert restored.to_state() == legacy
    assert ManifestEntry.from_state({**state, "excerpt": "x" * 2_000}) is None


async def test_the_eighth_round_of_a_turn_is_the_last() -> None:
    generator = StubLastWordsGenerator(text="[note]")
    reminder = StubReminderMiddleware()
    strategy = _strategy(_window_keeping(2.5), last_words_generator=generator, reminder_middleware=reminder)

    for round_number in range(1, MAX_DROP_ROUNDS_PER_TURN + 1):
        assert await strategy(_turn(6)), round_number
    assert MAX_DROP_ROUNDS_PER_TURN == 8
    assert len(generator.calls) == 8
    assert reminder.last_words.get_drop_round_breaker().attempts == 8

    ninth = _turn(6)
    assert not await strategy(ninth)

    assert len(generator.calls) == 8
    assert _dropped_calls(ninth) == []
    assert reminder.last_words.get_drop_round_breaker().disabled is True
    assert generator.breaker_trips == ["8 attempts limit exceeded for current turn"]
