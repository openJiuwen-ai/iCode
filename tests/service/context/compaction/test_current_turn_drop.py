# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Phase 4 current-turn drop rounds: the LAST_WORDS pipeline and the drop-round breaker."""

from collections.abc import Callable

import pytest

from chrys.foundation.trajectory.metadata import ensure_analytics_item_id, read_analytics_item_id
from chrys.kernel import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    CompactionCallContext,
    annotate_token_counts,
    included_token_count,
)
from chrys.kernel import compaction as chrys_compaction
from chrys.service.context.compaction import (
    _REASON_CURRENT_TURN_DROP,
    MAX_DROP_ROUNDS_PER_TURN,
    CompactionInfo,
    UnifiedContextStrategy,
)
from chrys.service.context.compaction.last_words import LastWordsSpendBudgetExceeded
from chrys.service.context.compaction.last_words_state import DropRoundBreakerState
from tests.service.context.compaction._compaction_helpers import (
    _async_appender,
    _build_multi_turn,
    _build_single_turn,
    _estimate_tokens,
    _forced_phase4,
    _make_strategy,
    _scoped_messages,
    _scoped_user_texts,
    _tokenizer,
    _user,
)
from tests.support.phase4_stubs import StubLastWordsGenerator, StubReminderMiddleware
from tests.support.reminder_stack import reminder_pair

# ---------------------------------------------------------------------------
# Drop-all and the LAST_WORDS generator / reminder contract
# ---------------------------------------------------------------------------


def test_phase4_default_side_call_budget_is_unlimited() -> None:
    strategy = UnifiedContextStrategy(max_context_tokens=12_345)

    assert strategy._phase4_side_call_token_budget == -1
    # None (legacy call sites) normalizes to unlimited too.
    legacy = UnifiedContextStrategy(max_context_tokens=100, phase4_side_call_token_budget=None)
    assert legacy._phase4_side_call_token_budget == -1


async def test_single_turn_compaction():
    """With only one turn, Phase 4 (current turn removal) handles compaction."""
    messages = _build_single_turn(8, result_size=3000)
    total_before = _estimate_tokens(messages)

    strategy = _make_strategy(
        max_context_tokens=total_before + 100,  # just over total → trigger fires
        trigger_pct=0.90,
        target_pct=0.50,
    )
    changed = await strategy(messages)
    assert changed

    # Some messages should be excluded (Phase 4 removes groups entirely)
    excluded = [m for m in messages if m.additional_properties.get(EXCLUDED_KEY, False)]
    assert len(excluded) > 0

    # Token count should decrease
    total_after = included_token_count(messages)
    assert total_after < total_before


async def test_phase4_invokes_last_words_generator():
    """Phase 4 calls the bound LastWordsGenerator with user request + dropped
    messages, stashes the note on the middleware and refreshes this call's
    outgoing user message."""
    messages = _build_single_turn(4, result_size=1000)
    messages[0] = _user("start <system-reminder>literal</system-reminder>")

    generator = StubLastWordsGenerator(text="[stub progress note]")
    reminder = StubReminderMiddleware()
    strategy = _forced_phase4(messages, last_words_generator=generator, reminder_middleware=reminder)
    changed = await strategy(messages)
    assert changed

    assert len(generator.calls) == 1
    call = generator.calls[0]
    assert _scoped_user_texts(call)[0] == "start <system-reminder>literal</system-reminder>"
    assert call["previous_last_words"] is None  # first invocation in the turn
    assert any(any(c.type == "function_call" for c in message.contents) for message in _scoped_messages(call))
    # LAST_WORDS must be stashed on the middleware
    assert reminder.last_words.get_last_words() == "[stub progress note]"
    # The round committed (spill + note + exclusions), so the post-commit
    # committed signal fires exactly once.
    assert generator.committed_publishes == 1
    # The enrichment ran before the note existed, so without a refresh of the
    # outgoing user message this very request would ship the dropped history
    # with a stale note (or none).
    assert len(reminder.refresh_calls) == 1
    assert reminder.refresh_calls[0] is messages


async def test_phase4_outgoing_user_message_carries_fresh_note_with_real_middleware():
    """End-to-end through the real ``SystemReminderMiddleware``: after the
    Phase 4 pass, the outgoing message list itself contains the new note as a
    ``[LAST_WORDS]`` reminder block on the last user message."""

    messages = _build_single_turn(4, result_size=1000)

    generator = StubLastWordsGenerator(text="[stub progress note]")
    middleware, last_words = reminder_pair()
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=middleware,
        last_words=last_words,
    )
    changed = await strategy(messages)
    assert changed

    last_user = next(m for m in reversed(messages) if m.role == "user")
    note_blocks = [
        c.text
        for c in last_user.contents
        if c.type == "text" and (c.text or "").startswith("<system-reminder>\n[LAST_WORDS] ")
    ]
    assert len(note_blocks) == 1
    assert "[stub progress note]" in note_blocks[0]


async def test_phase4_refreshed_note_updates_token_annotations():
    """Regression: the refreshed user message shares its annotation dict with
    the pre-refresh object (exclusion flags must write through), so without a
    recompute its token_count still describes the note-less contents —
    ``tokens_after`` and every later trigger check would undercount by the
    note's size for the rest of the turn, because the incremental annotate
    skips already-counted messages."""

    note = "progress so far: " + "read another module and recorded its collaborators. " * 60
    note_tokens = _tokenizer.count_tokens(note)
    generator = StubLastWordsGenerator(text=note)
    middleware, last_words = reminder_pair()

    messages = _build_single_turn(4, result_size=1000)
    original_user = messages[0]

    received: list[CompactionInfo] = []
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=middleware,
        last_words=last_words,
        on_compaction=_async_appender(received),
    )
    changed = await strategy(messages)
    assert changed

    last_user = next(m for m in reversed(messages) if m.role == "user")
    assert last_user is not original_user
    # The recorded count must reflect the refreshed contents, note included —
    # with the stale annotation it would stay at the note-less handful of tokens.
    assert chrys_compaction._token_count(last_user) == chrys_compaction._message_token_estimate(last_user, _tokenizer)
    assert included_token_count(messages) > note_tokens
    p4 = next(r for r in received if r.phase == "phase4")
    assert p4.tokens_after == included_token_count(messages)
    # The next call's incremental annotate must keep the fresh count rather
    # than resurrect the stale one.
    annotate_token_counts(messages, tokenizer=_tokenizer)
    assert included_token_count(messages) == p4.tokens_after
    # Accepted write-through: the dict is shared with the pre-refresh
    # (history) object whose contents lack the note, so the shared count
    # overstates that object — the safe direction (compaction fires earlier).
    assert original_user.additional_properties is last_user.additional_properties


async def test_phase4_regenerates_with_previous_last_words():
    """Subsequent Phase 4 rounds feed the previous note back into the generator."""
    generator = StubLastWordsGenerator(text="[note v2]")
    reminder = StubReminderMiddleware()
    reminder.last_words.set_last_words("[note v1]")  # simulate prior Phase 4 in this turn

    messages = _build_single_turn(4, result_size=1000)

    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=reminder,
    )
    await strategy(messages)

    assert generator.calls[0]["previous_last_words"] == "[note v1]"
    assert reminder.last_words.get_last_words() == "[note v2]"


async def test_phase4_passes_only_current_scoped_timeline_and_completer():
    """Phase 4 forwards the client completer with no previous-turn messages."""

    class _SentinelCompleter:
        async def complete_last_words(  # type: ignore[no-untyped-def]
            self, base_messages, instruction, *, max_output_tokens, on_usage=None, keep_tool_choice=False
        ):
            raise AssertionError("stub generator must not invoke the completer")

    messages = _build_multi_turn(2, groups_per_turn=3, result_size=1000)

    generator = StubLastWordsGenerator(text="[note]")
    completer = _SentinelCompleter()
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
    )

    changed = await strategy(
        messages,
        CompactionCallContext(last_words_completer=completer, request_overhead_tokens=321),
    )
    assert changed

    call = generator.calls[0]
    assert call["completer"] is completer
    assert call["request_overhead_tokens"] == 321
    assert call["system_overhead_tokens"] == strategy.system_overhead_tokens
    assert _scoped_user_texts(call) == ["Turn 2 request"]
    assert all("Turn 1" not in (message.text or "") for message in _scoped_messages(call))


async def test_strategy_without_context_passes_no_completer():
    """Calling the strategy without a context (legacy call shape) still works
    and hands the generator no completer."""
    messages = _build_single_turn(4, result_size=1000)

    generator = StubLastWordsGenerator(text="[note]")
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
    )

    changed = await strategy(messages)
    assert changed
    assert generator.calls[0]["completer"] is None
    assert generator.calls[0]["scoped_groups"]


@pytest.mark.parametrize("prior_note", [None, "[prior note]"], ids=["no_prior_note", "prior_note"])
async def test_phase4_does_not_drop_when_generator_yields_nothing(prior_note: str | None) -> None:
    """An empty LAST_WORDS result never authorises a drop: with no prior note
    nothing is excluded, and a pre-existing note cannot stand in for the newly
    generated delta either."""

    class _EmptyGenerator:
        async def generate(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return ""

    reminder = StubReminderMiddleware()
    if prior_note is not None:
        reminder.last_words.set_last_words(prior_note)
    messages = _build_single_turn(4, result_size=1000)
    strategy = _forced_phase4(messages, last_words_generator=_EmptyGenerator(), reminder_middleware=reminder)
    changed = await strategy(messages)
    assert not changed
    assert reminder.last_words.get_last_words() == prior_note
    assert not any(m.additional_properties.get(EXCLUDE_REASON_KEY) == "current_turn_drop" for m in messages)


async def test_phase4_preserves_user_message() -> None:
    """The turn opener (user message) must survive Phase 4 drop-all.

    Phase 4 collects all current-turn groups and drops them, but the
    user-message group is explicitly filtered out in compaction.py.
    """
    messages = _build_single_turn(5, result_size=1500)
    user_msg = messages[0]

    strategy = _forced_phase4(
        messages,
    )
    await strategy(messages)

    assert not user_msg.additional_properties.get(EXCLUDED_KEY, False), (
        "User message (turn opener) must never be excluded by Phase 4"
    )


async def test_phase4_propagates_generator_exception() -> None:
    """If the generator raises, the exception must propagate out of the
    strategy so the caller can fail without dropping current-turn work.

    LAST_WORDS is LLM-only — there is no programmatic fallback.  Generator
    failures (transport error, empty response → ``LastWordsGenerationError``)
    are retried by the generator against the same compaction input before
    it raises.  Messages must not be mutated before the raise.
    """

    class _ExplodingGenerator:
        async def generate(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    reminder = StubReminderMiddleware()
    messages = _build_single_turn(4, result_size=1000)
    strategy = _forced_phase4(
        messages,
        last_words_generator=_ExplodingGenerator(),
        reminder_middleware=reminder,
    )

    with pytest.raises(RuntimeError, match="boom"):
        await strategy(messages)

    # No note set, and no group excluded — the strategy raised before
    # applying Phase 4 exclusions, so retry sees a clean state.
    assert reminder.last_words.get_last_words() is None
    assert not any(m.additional_properties.get(EXCLUDE_REASON_KEY) == "current_turn_drop" for m in messages)
    breaker = reminder.last_words.get_drop_round_breaker()
    assert breaker.attempts == 1
    assert breaker.consecutive_no_progress == 1
    assert breaker.tail_override is True


async def test_phase4_includes_tool_call_and_result_in_dropped_messages() -> None:
    """Dropped_messages must contain both function_call and function_result contents."""
    generator = StubLastWordsGenerator(text="[note]")
    reminder = StubReminderMiddleware()
    received: list[CompactionInfo] = []
    messages = _build_single_turn(3, result_size=1500)
    for message in messages:
        ensure_analytics_item_id(message.additional_properties)
        for content in message.contents:
            ensure_analytics_item_id(content.additional_properties)
    strategy = _forced_phase4(
        messages,
        on_compaction=_async_appender(received),
        last_words_generator=generator,
        reminder_middleware=reminder,
    )
    await strategy(messages)

    assert generator.calls
    scoped_messages = _scoped_messages(generator.calls[0])
    has_call = any(any(c.type == "function_call" for c in message.contents) for message in scoped_messages)
    has_result = any(any(c.type == "function_result" for c in message.contents) for message in scoped_messages)
    assert has_call, "Dropped messages should contain the assistant tool-call message"
    assert has_result, "Dropped messages should contain the tool-result message"
    dropped_messages = [
        message
        for message in messages
        if message.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    ]
    expected_ids = list(
        dict.fromkeys(
            item_id
            for message in dropped_messages
            for item_id in (
                read_analytics_item_id(message.additional_properties),
                *(read_analytics_item_id(content.additional_properties) for content in message.contents),
            )
            if item_id is not None
        )
    )
    assert next(info for info in received if info.phase == "phase4").consumed_item_ids == expected_ids


async def test_phase4_noop_when_collaborators_unbound() -> None:
    """When the strategy has no reminder middleware and no generator,
    Phase 4 must not silently drop current-turn work.

    This is the scenario the sub-agent path fell into before the fix —
    without collaborators ``have_note`` is False and the ``if have_note:``
    guard must prevent any exclusions.
    """
    messages = _build_single_turn(5, result_size=1500)
    total = _estimate_tokens(messages)
    # Build a strategy WITHOUT wiring collaborators (explicitly None).
    strategy = UnifiedContextStrategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        target_pct=0.01,
    )
    # No bind_reminder / set_last_words_generator calls.
    changed = await strategy(messages)
    # Either unchanged, or only phases 1/2 fired.  Phase 4 must not have
    # produced any current_turn_drop exclusions.
    assert not any(m.additional_properties.get(EXCLUDE_REASON_KEY) == "current_turn_drop" for m in messages), (
        "Phase 4 dropped groups without a LAST_WORDS note — silent amputation"
    )
    _ = changed  # outcome is fine either way; what matters is no silent drop


# ---------------------------------------------------------------------------
# Drop-round breaker
# ---------------------------------------------------------------------------


async def test_phase4_same_turn_retry_observes_failed_attempt_count() -> None:
    class FailOnceGenerator:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first failure")
            return "[recovered note]"

        async def publish_committed(self) -> None:
            return None

    generator = FailOnceGenerator()
    reminder = StubReminderMiddleware()
    messages = _build_single_turn(2, result_size=1_000)
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=reminder,
    )

    with pytest.raises(RuntimeError, match="first failure"):
        await strategy(messages)
    assert reminder.last_words.get_drop_round_breaker().attempts == 1

    assert await strategy(messages)
    assert reminder.last_words.get_drop_round_breaker().attempts == 2
    assert reminder.last_words.get_drop_round_breaker().consecutive_no_progress == 0


async def test_phase4_side_call_budget_trips_before_attempt_and_records_failed_attempt() -> None:
    class BudgetGenerator:
        async def generate(self, *_args, spend_side_call_tokens=None, **_kwargs):  # type: ignore[no-untyped-def]
            assert spend_side_call_tokens is not None
            if not spend_side_call_tokens(100):
                raise LastWordsSpendBudgetExceeded("budget")
            raise AssertionError("budget-equal attempt must not run")

    reminder = StubReminderMiddleware()
    events: list[str] = []
    messages = _build_single_turn(2, result_size=1_000)
    strategy = _forced_phase4(
        messages,
        last_words_generator=BudgetGenerator(),
        reminder_middleware=reminder,
        phase4_side_call_token_budget=100,
        on_context_pressure=lambda reason, _breaker, _budget: events.append(reason),
    )

    assert not await strategy(messages)

    breaker = reminder.last_words.get_drop_round_breaker()
    assert breaker.attempts == 1
    assert breaker.side_call_tokens == 100
    assert breaker.consecutive_no_progress == 1
    assert breaker.tail_override is True
    assert breaker.disabled is True
    assert events == ["side_call_budget"]


async def test_phase4_second_no_progress_disables_and_publishes_pressure_event() -> None:
    generator = StubLastWordsGenerator(text="")
    reminder = StubReminderMiddleware()
    events: list[tuple[str, object, int]] = []
    messages = _build_single_turn(2, result_size=1_000)
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=reminder,
        on_context_pressure=lambda reason, breaker, budget: events.append((reason, breaker, budget)),
    )

    assert not await strategy(messages)
    first = reminder.last_words.get_drop_round_breaker()
    assert first.tail_override is True and first.disabled is False
    assert not await strategy(messages)
    second = reminder.last_words.get_drop_round_breaker()
    assert second.consecutive_no_progress == 2
    assert second.disabled is True
    assert events and events[-1][0] == "generation_failure"

    assert not await strategy(messages)
    assert len(generator.calls) == 2
    assert reminder.last_words.get_drop_round_breaker().attempts == 2
    assert len(events) == 1


@pytest.mark.parametrize(
    ("make_breaker", "expected_event", "expected_trips"),
    [
        pytest.param(
            lambda: DropRoundBreakerState(attempts=2, consecutive_no_progress=2, disabled=True),
            "disabled",
            [],
            id="restored_disabled",
        ),
        pytest.param(
            lambda: DropRoundBreakerState(attempts=MAX_DROP_ROUNDS_PER_TURN, side_call_tokens=10),
            "round_limit",
            [f"{MAX_DROP_ROUNDS_PER_TURN} attempts limit exceeded for current turn"],
            id="entry_round_limit",
        ),
    ],
)
async def test_phase4_breaker_refusal_publishes_pressure_once_before_generator_call(
    make_breaker: Callable[[], DropRoundBreakerState], expected_event: str, expected_trips: list[str]
) -> None:
    """A breaker that refuses entry publishes one pressure event and never calls
    the generator; later triggers re-enter through "disabled" and stay silent."""
    generator = StubLastWordsGenerator()
    reminder = StubReminderMiddleware()
    reminder.last_words.set_drop_round_breaker(make_breaker())
    events: list[str] = []
    messages = _build_single_turn(2, result_size=1_000)
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=reminder,
        on_context_pressure=lambda reason, _breaker, _budget: events.append(reason),
    )

    assert not await strategy(messages)

    assert generator.calls == []
    assert reminder.last_words.get_drop_round_breaker().disabled is True
    assert events == [expected_event]
    # The first round-limit trip surfaces one terminal failure card with the reason.
    assert generator.breaker_trips == expected_trips

    assert not await strategy(messages)

    assert generator.calls == []
    assert events == [expected_event]
    assert generator.breaker_trips == expected_trips


async def test_phase4_drop_that_would_free_less_than_five_points_is_not_attempted() -> None:
    generator = StubLastWordsGenerator(text="[note]")
    reminder = StubReminderMiddleware()
    messages = _build_single_turn(1, result_size=5)
    messages[0] = _user("large opener " + "context " * 20_000)
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=reminder,
    )

    assert not await strategy(messages)

    assert generator.calls == []
    assert reminder.last_words.get_drop_round_breaker() == DropRoundBreakerState()


async def test_phase4_unlimited_budget_never_refuses_spend_or_entry() -> None:
    generator = StubLastWordsGenerator(text="[note]")
    reminder = StubReminderMiddleware()
    reminder.last_words.set_drop_round_breaker(DropRoundBreakerState(side_call_tokens=10_000_000))
    messages = _build_single_turn(2, result_size=1_000)
    strategy = _forced_phase4(
        messages,
        last_words_generator=generator,
        reminder_middleware=reminder,
    )

    assert await strategy(messages)

    assert len(generator.calls) == 1
    # Spend still accumulates for observability but never refuses.
    assert strategy._spend_side_call_tokens(5_000) is True
    assert reminder.last_words.get_drop_round_breaker().side_call_tokens == 10_005_000
    assert generator.breaker_trips == []
