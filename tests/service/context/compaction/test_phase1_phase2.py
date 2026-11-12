# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Phase 1 summarisation and Phase 2 removal of previous-turn tool groups."""

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.kernel import (
    EXCLUDED_KEY,
    Content,
    Message,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
    set_excluded,
)
from chrys.kernel.compaction import _token_count
from chrys.service.context.compaction import (
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    CompactionInfo,
    _group_id,
    _group_kind_map,
    _group_messages_by_id,
    _ordered_group_ids,
)
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _assistant_tool_call,
    _async_appender,
    _build_multi_turn,
    _build_single_turn,
    _build_tool_group,
    _estimate_tokens,
    _forced_phase4,
    _make_strategy,
    _tool_group_id_of,
    _tool_result,
    _user,
)

# ---------------------------------------------------------------------------
# Phase 1: old turn compaction
# ---------------------------------------------------------------------------


async def test_phase1_compacts_old_turns_first():
    """Old turns are compacted before touching the current turn."""
    messages = _build_multi_turn(3, groups_per_turn=3, result_size=2000)
    total = _estimate_tokens(messages)

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.90,
        target_pct=0.50,
    )
    changed = await strategy(messages)
    assert changed

    # Current turn's tool groups (the last 3) should NOT be compacted
    # Find the last user message (start of current turn)
    last_user_idx = max(i for i, m in enumerate(messages) if m.role == "user")

    # Check that no tool results AFTER the last user message are excluded
    current_turn_excluded = [
        m
        for m in messages[last_user_idx:]
        if m.additional_properties.get(EXCLUDED_KEY, False) and any(c.type == "function_result" for c in m.contents)
    ]
    assert len(current_turn_excluded) == 0, "Current turn tool results should not be compacted in phase 1"


async def test_phase1_never_splits_a_call_from_its_result_block():
    """A previous turn whose response interleaves text between the call and
    its result block must compact atomically. The call and the result block
    grouped apart lets Phase 1 exclude the call, hit the token target, and
    stop — projecting a function_result whose call is gone, which providers
    reject outright. The narration fused between the call and its result is
    excluded with the rest of the group, so the summary must carry its text
    instead of silently dropping it."""
    messages = [
        _user("Turn 1 request"),
        _assistant_tool_call("call_a", "big_tool", args={"payload": "x" * 8000}),
        _assistant_text("NARRATION checking the parser config"),
        _tool_result("call_a", "ok"),
        _assistant_text("Turn 1 answer"),
        _user("Turn 2 request"),
        _assistant_text("Turn 2 answer"),
    ]
    total = _estimate_tokens(messages)

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.90,
        target_pct=0.50,
    )
    changed = await strategy(messages)
    assert changed

    projected = project_included_messages(messages)
    projected_calls = {c.call_id for m in projected for c in m.contents if c.type == "function_call"}
    projected_results = {c.call_id for m in projected for c in m.contents if c.type == "function_result"}
    assert projected_results <= projected_calls, "projected a result whose call was compacted away"
    assert projected_calls <= projected_results, "projected a call whose result was compacted away"
    assert "call_a" not in projected_calls, "the oversized previous-turn group should have been compacted"

    summary = next(m for m in messages if m.text and m.text.startswith("[Tool call:"))
    assert "NARRATION checking the parser config" in summary.text


async def test_phase1_stops_at_target():
    """Phase 1 stops as soon as usage drops to target_pct."""
    # 5 turns with enough content that compacting 1-2 old turns reaches target
    messages = _build_multi_turn(5, groups_per_turn=2, result_size=1500)
    total = _estimate_tokens(messages)

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.90,
        target_pct=0.80,  # high target → only 1-2 old turns need compaction
    )
    changed = await strategy(messages)
    assert changed

    # Not all old turns should be compacted (target reached early)
    summaries = [m for m in messages if m.text and m.text.startswith("[Tool call:")]
    total_old_groups = 4 * 2  # 4 old turns * 2 groups each
    assert len(summaries) < total_old_groups


async def test_phase1_then_phase4_fallthrough():
    """When old turns are exhausted, falls through to Phase 4 (current turn removal)."""
    # 2 turns: small old turn, large current turn
    messages = [
        _user("Turn 1"),
        *_build_tool_group("t0_c0", "tool_0", "short"),
        _assistant_text("R1"),
        _user("Turn 2"),
    ]
    # Add many large tool calls in current turn
    for i in range(8):
        messages.extend(_build_tool_group(f"t1_c{i}", f"tool_{i}", "x" * 3000))
    messages.append(_assistant_text("R2"))

    total = _estimate_tokens(messages)

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.90,
        target_pct=0.30,  # low target forces current turn compaction
    )
    changed = await strategy(messages)
    assert changed

    # Old turn groups should have summaries (Phase 1), current turn groups
    # should be excluded (Phase 4 removes them entirely)
    excluded = [m for m in messages if m.additional_properties.get(EXCLUDED_KEY, False)]
    assert len(excluded) > 0  # old turn + some current turn groups


# ---------------------------------------------------------------------------
# _compact_group / _remove_group primitives
# ---------------------------------------------------------------------------


def test_compact_group_skips_group_with_excluded_result() -> None:
    call, result = _build_tool_group("c1", "read_file", "excluded payload", args={"path": "secret.txt"})
    set_excluded(result, excluded=True, reason="cross_turn_compression")
    messages = [call, result]
    before = _estimate_tokens(messages)
    grouped = _group_messages_by_id(messages)
    group_id = _tool_group_id_of(messages, "c1")
    strategy = _make_strategy()

    result = strategy._compact_group(messages, group_id, grouped[group_id])
    assert included_token_count(messages) == before
    assert result is None
    assert strategy._summary_cache == {}


def test_compact_group_does_not_build_summary_from_excluded_call(monkeypatch: pytest.MonkeyPatch) -> None:
    call, result = _build_tool_group("c1", "sensitive_tool_name", "visible result", args={"secret": "value"})
    set_excluded(call, excluded=True, reason="cross_turn_compression")
    messages = [call, result]
    _estimate_tokens(messages)
    grouped = _group_messages_by_id(messages)
    group_id = _tool_group_id_of(messages, "c1")
    strategy = _make_strategy()

    def fail_build_summary(_group_msgs: list[Message]) -> str:
        pytest.fail("excluded group metadata reached _build_summary")

    monkeypatch.setattr("chrys.service.context.compaction.summaries._build_summary", fail_build_summary)

    assert strategy._compact_group(messages, group_id, grouped[group_id]) is None
    assert strategy._summary_cache == {}


def test_compact_group_counts_only_its_summary() -> None:
    """The messages after an inserted summary keep their contents; counting them again per group was quadratic."""
    messages = _build_single_turn(4, result_size=3000)
    _estimate_tokens(messages)
    counted: list[str] = []

    class RecordingTokenizer(MixedLanguageTokenizer):
        def count_tokens(self, text: str) -> int:
            counted.append(text)
            return super().count_tokens(text)

    strategy = _make_strategy(tokenizer=RecordingTokenizer())
    grouped = _group_messages_by_id(messages)
    group_id = _tool_group_id_of(messages, "call_0")

    assert strategy._compact_group(messages, group_id, grouped[group_id])

    [summary] = [message for message in messages if message.message_id.startswith("tool_summary_")]
    # Once to weigh the saving, once for the inserted message; never a later message.
    assert len(counted) == 2
    assert all(summary.text in text for text in counted)
    stored = [_token_count(message) for message in messages]
    assert None not in stored
    annotate_token_counts(messages, tokenizer=MixedLanguageTokenizer(), force_retokenize=True)
    assert [_token_count(message) for message in messages] == stored


def test_remove_group_never_inserts():
    """P2's phase-entry resolution is sufficient only because _remove_group
    never inserts messages — pin that contract."""
    messages = _build_single_turn(2, result_size=500)
    _estimate_tokens(messages)  # annotate groups
    strategy = _make_strategy()
    grouped = _group_messages_by_id(messages)
    kinds = _group_kind_map(messages)
    target_gid = next(g for g in _ordered_group_ids(messages) if kinds.get(g) == "tool_call")
    before_len = len(messages)

    result = strategy._remove_group(messages, target_gid, grouped[target_gid])

    assert result
    assert len(messages) == before_len


@pytest.mark.parametrize("result_text", ["", "x"], ids=["empty-result", "tiny-result"])
def test_compact_group_never_raises_included_tokens_for_partially_excluded_group(result_text: str) -> None:
    messages = _build_tool_group("c1", "read_file", result_text)
    set_excluded(messages[0], excluded=True, reason="cross_turn_compression")
    before = _estimate_tokens(messages)
    grouped = _group_messages_by_id(messages)
    group_id = _tool_group_id_of(messages, "c1")
    strategy = _make_strategy()

    result = strategy._compact_group(messages, group_id, grouped[group_id])
    assert included_token_count(messages) == before
    assert result is None


@pytest.mark.parametrize(("result_size", "summarised"), [(600, False), (3000, True)], ids=["small", "large"])
def test_compact_group_leaves_a_group_whose_summary_saves_little(result_size: int, summarised: bool) -> None:
    """Each summary rewrites one more place in the prompt: a small saving costs more cache than it frees."""
    messages = _build_tool_group("c1", "read_file", "x" * result_size)
    before = _estimate_tokens(messages)
    grouped = _group_messages_by_id(messages)
    group_id = _tool_group_id_of(messages, "c1")
    strategy = _make_strategy()

    result = strategy._compact_group(messages, group_id, grouped[group_id])

    saved = before - included_token_count(messages)
    if summarised:
        assert result == ["read_file"]
        assert saved >= 200
    else:
        assert result is None
        assert saved == 0
        assert strategy._summary_cache == {}
        assert not any(message.additional_properties.get(EXCLUDED_KEY, False) for message in messages)


def test_compact_group_skips_group_already_removed_by_phase2() -> None:
    call = _assistant_tool_call("c1", "read_file", args={"path": "secret.txt"})
    narration = _assistant_text("checking secret payload")
    result = _tool_result("c1", "secret payload")
    messages = [call, narration, result]
    _estimate_tokens(messages)
    grouped = _group_messages_by_id(messages)
    group_id = _tool_group_id_of(messages, "c1")
    strategy = _make_strategy()

    assert strategy._remove_group(messages, group_id, grouped[group_id])
    assert strategy._compact_group(messages, group_id, grouped[group_id]) is None

    assert not strategy._summary_cache
    assert not any(
        message.additional_properties.get("_group", {}).get("_summary_of_group_ids")
        and not message.additional_properties.get(EXCLUDED_KEY)
        for message in messages
    )


# ---------------------------------------------------------------------------
# Phase 2: old turn group removal
# ---------------------------------------------------------------------------


async def test_phase2_removes_old_turn_groups():
    """Phase 2 removes old turn tool-call groups entirely when Phase 1 can't reach target."""
    # 3 turns whose summaries cannot reach the unreachable target on their own
    messages = _build_multi_turn(3, groups_per_turn=2, result_size=3000)

    received: list[CompactionInfo] = []
    strategy = _forced_phase4(
        messages,
        on_compaction=_async_appender(received),
    )
    changed = await strategy(messages)
    assert changed

    phases_fired = {r.phase for r in received}
    # Phase 1 should fire (summarise old turns)
    assert "phase1" in phases_fired
    # Phase 2 should fire (remove old turn groups entirely)
    assert "phase2" in phases_fired

    p2 = [r for r in received if r.phase == "phase2"]
    assert p2[0].compacted_groups > 0
    assert p2[0].turn_numbers  # should report which turns were affected


async def test_phase2_stops_at_target():
    """Phase 2 stops removing old turn groups once usage drops to target."""
    # 4 old turns + 1 current, moderate size
    messages = _build_multi_turn(5, groups_per_turn=2, result_size=2000)
    total = _estimate_tokens(messages)

    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        target_pct=0.50,  # reachable after removing a few old turn groups
        on_compaction=_async_appender(received),
    )
    changed = await strategy(messages)
    assert changed

    # If phase 2 fired, it should not have removed ALL old turn groups
    p2 = [r for r in received if r.phase == "phase2"]
    if p2:
        # Not all 4 old turns removed — stopped at target
        assert p2[0].compacted_groups < 4 * 2  # 4 turns * 2 groups


async def test_phase2_old_turn_user_agent_msgs_survive():
    """Phase 2 removes tool groups but user/agent text messages remain visible.

    Assertions target only *previous* turns — Phase 4 may additionally drop
    the current turn's inline assistant text along with its tool calls.
    """
    messages = _build_multi_turn(3, groups_per_turn=2, result_size=500)

    strategy = _forced_phase4(
        messages,
    )
    await strategy(messages)

    last_user_idx = max(i for i, m in enumerate(messages) if m.role == "user")

    # User messages survive unconditionally.
    for msg in messages:
        if msg.role == "user":
            assert not msg.additional_properties.get(EXCLUDED_KEY, False), "user messages must survive Phase 2"

    # Previous-turn assistant text must survive Phase 2 removal.
    for idx, msg in enumerate(messages[:last_user_idx]):
        if msg.role == "assistant" and any(c.type == "text" for c in msg.contents):
            if msg.text and msg.text.startswith("[Tool call:"):
                continue
            assert not msg.additional_properties.get(EXCLUDED_KEY, False), (
                f"prior-turn agent text at index {idx} must survive Phase 2"
            )


async def test_remove_group_excludes_summary_and_originals():
    """_remove_group excludes both original messages and any Phase 1 summary."""
    messages = _build_multi_turn(2, groups_per_turn=2, result_size=2000)

    strategy = _forced_phase4(
        messages,
    )
    await strategy(messages)

    # After all phases: old turn tool results should be excluded
    # AND any summaries from Phase 1 for those groups should also be excluded
    old_turn_excluded = 0
    summary_excluded = 0
    for msg in messages:
        if msg.role == "user":
            continue
        # Old turn function_call/result should be excluded
        for content in msg.contents:
            if (
                content.type in ("function_call", "function_result")
                and content.call_id
                and content.call_id.startswith("t0_")
            ):
                assert msg.additional_properties.get(EXCLUDED_KEY, False), (
                    f"Old turn tool message should be excluded: {content.call_id}"
                )
                old_turn_excluded += 1
        # Summary messages for old turn groups should also be excluded by Phase 2
        summary_groups = msg.additional_properties.get("_group", {}).get("_summary_of_group_ids", [])
        if any(gid in strategy._removed_group_ids for gid in summary_groups):
            assert msg.additional_properties.get(EXCLUDED_KEY, False), (
                f"Summary for removed group should be excluded: {msg.message_id}"
            )
            summary_excluded += 1

    assert old_turn_excluded > 0, "Expected old turn tool messages to be excluded"
    assert summary_excluded > 0, "Expected summary messages for removed groups to be excluded"


async def test_removed_groups_stay_excluded_across_iterations():
    """Groups removed by Phase 2/3 remain excluded in subsequent strategy calls."""
    messages = _build_multi_turn(3, groups_per_turn=2, result_size=1000)

    strategy = _forced_phase4(
        messages,
    )

    # First call: compaction fires, groups removed
    changed = await strategy(messages)
    assert changed
    assert len(strategy._removed_group_ids) > 0

    removed_before = set(strategy._removed_group_ids)

    # Simulate tool-loop: framework does list(messages) shallow copy
    messages_copy = list(messages)

    # Second call on the copy: removed groups should stay excluded
    await strategy(messages_copy)

    # _removed_group_ids should not shrink
    assert removed_before.issubset(strategy._removed_group_ids)

    # Messages from removed groups should still be excluded
    for msg in messages_copy:
        gid = _group_id(msg)
        if gid and gid in removed_before:
            assert msg.additional_properties.get(EXCLUDED_KEY, False), (
                f"Removed group {gid} should stay excluded across iterations"
            )


# ---------------------------------------------------------------------------
# Exchange-unit boundaries
# ---------------------------------------------------------------------------


def _sibling_call_run() -> list[Message]:
    return [
        Message(role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]),
        Message(role="assistant", contents=[Content.from_function_call("call_b", "tool_b", arguments={})]),
        Message(
            role="tool",
            contents=[
                Content.from_function_result("call_a", result="alpha outcome"),
                Content.from_function_result("call_b", result="beta outcome"),
            ],
        ),
    ]


class TestExchangeUnitBoundaries:
    """A run of call-carrying assistant siblings answered by one shared
    result block is ONE annotated unit, and user-role or marker messages are
    never members of the unit whose call they answer."""

    def test_phase1_summarizes_sibling_call_run_as_one_unit(self) -> None:
        messages = _sibling_call_run()
        annotate_message_groups(messages)

        kinds = _group_kind_map(messages)
        tool_gids = [gid for gid in _ordered_group_ids(messages) if kinds.get(gid) == "tool_call"]
        assert len(tool_gids) == 1
        grouped = _group_messages_by_id(messages)
        assert grouped[tool_gids[0]] == messages[:3]

        strategy = _make_strategy()
        assert strategy._compact_group(messages, tool_gids[0], grouped[tool_gids[0]])
        summary = next(m for m in messages if m.text.startswith("[Tool call:"))
        for fragment in ("tool_a", "alpha outcome", "tool_b", "beta outcome"):
            assert fragment in summary.text

    def test_phase2_removes_sibling_call_run_as_one_unit(self) -> None:
        messages = _sibling_call_run()
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._remove_group(messages, target_gid, grouped[target_gid])
        assert all(m.additional_properties.get(EXCLUDED_KEY, False) for m in messages)

    def test_phase1_leaves_user_role_result_out_of_the_summarized_unit(self) -> None:
        call_message = Message(
            role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]
        )
        user_result = Message(role="user", contents=[Content.from_function_result("call_a", result="alpha outcome")])
        messages = [call_message, user_result]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._compact_group(messages, target_gid, grouped[target_gid]) is None
        assert not call_message.additional_properties.get(EXCLUDED_KEY, False)
        assert not user_result.additional_properties.get(EXCLUDED_KEY, False)

    def test_phase1_leaves_marker_carried_result_out_of_the_summarized_unit(self) -> None:
        call_message = Message(
            role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]
        )
        marker_result = Message(
            role="assistant", contents=[Content.from_function_result("call_a", result="alpha outcome")]
        )
        marker_result.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
        messages = [call_message, marker_result]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._compact_group(messages, target_gid, grouped[target_gid]) is None
        assert not call_message.additional_properties.get(EXCLUDED_KEY, False)
        assert not marker_result.additional_properties.get(EXCLUDED_KEY, False)

    def test_phase1_keeps_image_narration_on_the_wire(self) -> None:
        """A fused narration member carrying content the text summary cannot
        represent (an image) survives compaction: the summary replaces only
        the members it can represent, and the kept member is neither excluded
        nor recorded as summarized."""
        call_message = Message(
            role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]
        )
        image_narration = Message(
            role="assistant",
            contents=[Content.from_uri("data:image/png;base64,AAAA", media_type="image/png")],
        )
        image_narration.message_id = "image-narration"
        result_message = Message(role="tool", contents=[Content.from_function_result("call_a", result="alpha outcome")])
        messages = [call_message, image_narration, result_message]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)
        assert image_narration in grouped[target_gid]

        strategy = _make_strategy()
        assert strategy._compact_group(messages, target_gid, grouped[target_gid])

        assert call_message.additional_properties.get(EXCLUDED_KEY, False)
        assert result_message.additional_properties.get(EXCLUDED_KEY, False)
        assert not image_narration.additional_properties.get(EXCLUDED_KEY, False)
        assert SUMMARIZED_BY_SUMMARY_ID_KEY not in image_narration.additional_properties
        summary = next(m for m in messages if m.text and m.text.startswith("[Tool call:"))
        original_ids = summary.additional_properties.get("_summary_of_message_ids") or []
        assert "image-narration" not in original_ids

    def test_phase1_keeps_mixed_text_and_image_narration_with_its_member(self) -> None:
        """Text riding an unsummarizable narration member stays on the wire
        with the member instead of being duplicated into the summary."""
        call_message = Message(
            role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]
        )
        narration = Message(
            role="assistant",
            contents=[
                Content.from_text("look at this chart"),
                Content.from_uri("data:image/png;base64,AAAA", media_type="image/png"),
            ],
        )
        result_message = Message(role="tool", contents=[Content.from_function_result("call_a", result="alpha outcome")])
        messages = [call_message, narration, result_message]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._compact_group(messages, target_gid, grouped[target_gid])

        assert not narration.additional_properties.get(EXCLUDED_KEY, False)
        summary = next(m for m in messages if m.text and m.text.startswith("[Tool call:"))
        assert "look at this chart" not in summary.text

    def test_phase2_keeps_fused_narration_on_the_wire(self) -> None:
        """Phase 2 removes args and results; fused narration members (text or
        image alike) are neither and stay on the wire, as they did when the
        exchange grouped them apart from the calls."""
        call_message = Message(
            role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]
        )
        text_narration = Message(role="assistant", contents=[Content.from_text("checking the chart next")])
        image_narration = Message(
            role="assistant",
            contents=[Content.from_uri("data:image/png;base64,AAAA", media_type="image/png")],
        )
        result_message = Message(role="tool", contents=[Content.from_function_result("call_a", result="alpha outcome")])
        messages = [call_message, text_narration, image_narration, result_message]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._remove_group(messages, target_gid, grouped[target_gid])

        assert call_message.additional_properties.get(EXCLUDED_KEY, False)
        assert result_message.additional_properties.get(EXCLUDED_KEY, False)
        assert not text_narration.additional_properties.get(EXCLUDED_KEY, False)
        assert not image_narration.additional_properties.get(EXCLUDED_KEY, False)

    def test_phase1_keeps_image_narration_in_a_reasoning_exchange(self) -> None:
        """Retention must survive projection in reasoning-bearing exchanges:
        the kept image is outside the atomic set (carriers + reasoning
        riders), so partial-group degrading never drags it back out."""
        call_message = Message(
            role="assistant",
            contents=[
                Content.from_text_reasoning(text="thinking"),
                Content.from_function_call("call_a", "tool_a", arguments={}),
            ],
        )
        image_narration = Message(
            role="assistant",
            contents=[Content.from_uri("data:image/png;base64,AAAA", media_type="image/png")],
        )
        result_message = Message(role="tool", contents=[Content.from_function_result("call_a", result="alpha outcome")])
        messages = [call_message, image_narration, result_message]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._compact_group(messages, target_gid, grouped[target_gid])
        projected = project_included_messages(messages)

        assert image_narration in projected
        assert not image_narration.additional_properties.get(EXCLUDED_KEY, False)
        assert call_message not in projected

    def test_phase1_reasoning_riding_narration_stays_group_bound(self) -> None:
        """A narration member that itself carries reasoning content belongs to
        the atomic set and is excluded with its group — an orphaned reasoning
        item must never outlive its exchange."""
        call_message = Message(
            role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]
        )
        narration = Message(
            role="assistant",
            contents=[
                Content.from_text_reasoning(text="thinking about the chart"),
                Content.from_uri("data:image/png;base64,AAAA", media_type="image/png"),
            ],
        )
        result_message = Message(role="tool", contents=[Content.from_function_result("call_a", result="alpha outcome")])
        messages = [call_message, narration, result_message]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._compact_group(messages, target_gid, grouped[target_gid])

        assert narration.additional_properties.get(EXCLUDED_KEY, False)

    def test_phase2_reasoning_rider_excluded_with_its_group(self) -> None:
        """Phase 2's narration skip never spares reasoning-bearing members."""
        call_message = Message(
            role="assistant", contents=[Content.from_function_call("call_a", "tool_a", arguments={})]
        )
        reasoning_rider = Message(role="assistant", contents=[Content.from_text_reasoning(text="thinking")])
        result_message = Message(role="tool", contents=[Content.from_function_result("call_a", result="alpha outcome")])
        messages = [call_message, reasoning_rider, result_message]
        annotate_message_groups(messages)
        target_gid = _tool_group_id_of(messages, "call_a")
        grouped = _group_messages_by_id(messages)

        strategy = _make_strategy()
        assert strategy._remove_group(messages, target_gid, grouped[target_gid])

        assert reasoning_rider.additional_properties.get(EXCLUDED_KEY, False)
