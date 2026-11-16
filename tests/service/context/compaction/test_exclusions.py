# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for exclusion anchors and exclusion-flag persistence across runs, tool loops and session restore."""

import gc
import weakref
from copy import copy

import pytest

from chrys.kernel import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    Agent,
    AgentSession,
    FunctionTool,
    Message,
    included_token_count,
    project_included_messages,
)
from chrys.service.context.compaction import (
    _REASON_CURRENT_TURN_DROP,
    CompactionInfo,
    _ExclusionAnchor,
)
from chrys.service.context.providers.history import (
    CompressibleHistoryProvider,
)
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _assistant_tool_call,
    _async_appender,
    _build_multi_turn,
    _build_single_turn,
    _build_tool_group,
    _estimate_tokens,
    _forced_phase4,
    _has_call_id,
    _make_strategy,
    _tool_result,
    _user,
)

# ---------------------------------------------------------------------------
# Exclusion persistence across runs
# ---------------------------------------------------------------------------


async def test_excluded_flags_persist_on_originals():
    """Running the strategy on originals persists _excluded flags."""
    messages = _build_single_turn(6, result_size=2000)

    strategy = _forced_phase4(
        messages,
    )

    changed = await strategy(messages)
    assert changed

    excluded_ids_run1 = {m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY, False)}
    assert len(excluded_ids_run1) > 0

    await strategy(messages)
    excluded_ids_run2 = {m.message_id for m in messages if m.additional_properties.get(EXCLUDED_KEY, False)}
    assert excluded_ids_run1 == excluded_ids_run2


async def test_after_run_compaction_prevents_recompaction():
    """Simulates the full cross-run cycle with after_run compaction."""
    messages = _build_single_turn(6, result_size=2000)

    strategy = _forced_phase4(
        messages,
    )
    provider = CompressibleHistoryProvider(compaction_strategy=strategy)

    state: dict = {"messages": messages}

    await strategy(state["messages"])

    excluded_count = sum(1 for m in state["messages"] if m.additional_properties.get(EXCLUDED_KEY, False))
    assert excluded_count > 0

    visible = await provider.get_messages(None, state=state)
    assert len(visible) < len(state["messages"])
    # Excluded messages must not appear in visible output
    for m in visible:
        assert not m.additional_properties.get(EXCLUDED_KEY, False)


async def _run_tool_loop_with_current_turn_compaction(*, stream: bool) -> list[Message]:
    """Run the real Agent tool loop and return persisted history messages."""

    def _big_tool() -> str:
        return "tool output\n" * 5000

    strategy = _make_strategy(
        max_context_tokens=1000,
        trigger_pct=0.01,
        target_pct=0.001,
    )
    history = CompressibleHistoryProvider(compaction_strategy=strategy)
    state: dict[str, object] = {}
    session = AgentSession()
    session.state[history.source_id] = state

    mock = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("_big_tool", "c1", {})]),
            MockResponse(text="done"),
        ]
    )
    tool = FunctionTool(func=_big_tool, name="_big_tool", description="Big tool")
    agent = Agent(
        client=mock,
        instructions="test",
        tools=[tool],
        context_providers=[history],
    )

    async with agent:
        if stream:
            response_stream = agent.run("q", session=session, stream=True, compaction_strategy=strategy)
            async for _ in response_stream:
                pass
            await response_stream.get_final_response()
        else:
            await agent.run("q", session=session, compaction_strategy=strategy)

    messages = state["messages"]
    assert isinstance(messages, list)
    return messages


@pytest.mark.parametrize("stream", [False, True], ids=["non_streaming", "streaming"])
async def test_current_turn_exclusions_persist_for_streaming_and_non_streaming(stream: bool) -> None:
    """Current-turn Phase 4 exclusions must survive both response paths.

    Non-streaming stores response messages directly enough that identity-based
    anchors work.  Streaming rebuilds the same messages from updates before
    ``after_run`` persists them, so this also covers the structural fallback.
    """
    messages = await _run_tool_loop_with_current_turn_compaction(stream=stream)

    assistant_call = next(
        m
        for m in messages
        if m.role == "assistant" and any(c.type == "function_call" for c in m.contents) and _has_call_id(m, "c1")
    )
    tool_result = next(
        m
        for m in messages
        if m.role == "tool" and any(c.type == "function_result" for c in m.contents) and _has_call_id(m, "c1")
    )

    assert assistant_call.additional_properties.get(EXCLUDED_KEY) is True
    assert assistant_call.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    assert tool_result.additional_properties.get(EXCLUDED_KEY) is True
    assert tool_result.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP


# ---------------------------------------------------------------------------
# _ExclusionAnchor
# ---------------------------------------------------------------------------


def test_exclusion_anchor_ignores_contents_list_hit_with_wrong_shape() -> None:
    """A list-identity hit must not beat a valid structural match."""
    wrong_id_hit = _assistant_text("wrong message")
    structural_match = _tool_result("c1", "current")
    anchor = _ExclusionAnchor(
        reason=_REASON_CURRENT_TURN_DROP,
        role="tool",
        contents_ref=weakref.ref(wrong_id_hit.contents),
        call_ids=("c1",),
    )

    assert anchor.find_in([wrong_id_hit, structural_match], set()) == 1


def test_exclusion_anchor_ignores_message_id_hit_with_wrong_shape() -> None:
    """A duplicate message_id must not beat a valid structural match."""
    wrong_id_hit = _assistant_tool_call("old", "zsh")
    wrong_id_hit.message_id = "dup"
    structural_match = _tool_result("c1", "current")
    structural_match.message_id = "dup"
    anchor = _ExclusionAnchor(
        reason=_REASON_CURRENT_TURN_DROP,
        role="tool",
        contents_ref=None,
        call_ids=("c1",),
        message_id="dup",
    )

    assert anchor.find_in([wrong_id_hit, structural_match], set()) == 1


def test_exclusion_anchor_from_wire_view_binds_state_original_not_structural_twin() -> None:
    """Shared-content-ids tier: a view-snapshotted anchor keeps identity precision.

    The wire hands clients per-call views (fresh wrapper + fresh contents
    list, SAME content objects). An anchor built from the view must bind the
    state original through the shared content ids — not fall through to the
    newest-first structural scan, which would bind a byte-equal twin sitting
    later in state.
    """
    original = _tool_result("c1", "same payload")
    twin = _tool_result("c1", "same payload")
    view = copy(original)
    view.contents = list(original.contents)
    anchor = _ExclusionAnchor.from_message(view)

    assert anchor.find_in([original, twin], set()) == 0


def test_exclusion_anchor_does_not_retain_its_source() -> None:
    """Identity tiers hold WEAK references: an anchor never keeps its source
    message alive, and a dead tier simply stops matching — the structural
    fallback still lands the exclusion on an equal-shaped rebuild, exactly
    the streaming-rebuild path."""
    strategy = _make_strategy()
    source = _tool_result("c1", "same payload")
    source.additional_properties[EXCLUDED_KEY] = True
    source.additional_properties[EXCLUDE_REASON_KEY] = _REASON_CURRENT_TURN_DROP
    source_ref = weakref.ref(source)
    content_ref = weakref.ref(source.contents[0])

    strategy._snapshot_excluded_anchors([source])
    snapshot = strategy.snapshot_retry_state()

    strategy._excluded_anchors = []
    del source
    gc.collect()
    assert source_ref() is None, "anchors must not pin their source"
    assert content_ref() is None

    strategy.restore_retry_state(snapshot)
    target = _tool_result("c1", "same payload")
    strategy.persist_exclusions_to_state([target])

    assert target.additional_properties[EXCLUDED_KEY] is True
    assert strategy._excluded_anchors == []


def test_exclusion_anchor_replaced_contents_list_stops_identity_matching() -> None:
    """Snapshot semantics: the list-identity tier captures THE list at
    snapshot time. Replacing the message's contents list defeats tier 1;
    tier 2 (shared content objects) still binds the same message."""
    source = _tool_result("c1", "payload")
    anchor = _ExclusionAnchor.from_message(source)
    assert anchor.contents_ref is not None

    source.contents = list(source.contents)

    assert anchor.contents_ref() is not source.contents
    assert anchor.find_in([source], set()) == 0  # content_refs tier


def test_exclusion_anchor_shared_list_twin_keeps_identity_match_after_owner_dies() -> None:
    """A shared-list twin keeps the list object alive, so the list-identity
    tier keeps matching it — exactly today's captured-id semantics."""
    source = _tool_result("c1", "payload")
    twin = _tool_result("c1", "payload")
    twin.contents = source.contents  # share THE list object
    anchor = _ExclusionAnchor.from_message(source)

    del source
    gc.collect()

    assert anchor.contents_ref is not None
    assert anchor.contents_ref() is twin.contents
    assert anchor.find_in([twin], set()) == 0


# ---------------------------------------------------------------------------
# Tool-loop shared-object regressions
# ---------------------------------------------------------------------------


async def test_excluded_flags_reset_prevents_tool_loop_message_loss():
    """Regression: stale _excluded flags on shared Message objects must not
    cause silent loss of NEW messages when the strategy sees a copied list.

    Chrys mutates the active tool-loop list in place, but still defends
    copied/replayed lists where Message objects are shared
    and ``project_included_messages`` filters ``_excluded=True``.

    This test verifies that newly added tool results (batch 2) are NOT
    excluded, while legitimately compacted originals remain excluded and
    their summaries are present.
    """
    # ── Build a multi-turn conversation that will trigger compaction ──
    # Turn 1: old turn with large tool results
    history: list[Message] = [
        _user("Turn 1 question"),
        *_build_tool_group("t1_c0", "search", "x" * 3000),
        *_build_tool_group("t1_c1", "read_file", "x" * 3000),
        _assistant_text("Turn 1 answer"),
    ]
    # Turn 2 (current): tool loop in progress
    history.append(_user("Turn 2 question"))
    # Batch 1: glob + grep
    batch1 = [
        *_build_tool_group("t2_glob", "glob", "x" * 2000),
        *_build_tool_group("t2_grep", "grep", "x" * 2000),
    ]
    history.extend(batch1)

    total = _estimate_tokens(history)

    # Same strategy instance used across both iterations (matching real framework)
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        target_pct=0.50,
    )

    # ── Iteration 1: the loop shallow-copies, compacts, projects ──
    shallow_copy_1 = list(history)  # list copy, shared Message objects
    changed = await strategy(shallow_copy_1)
    assert changed, "Strategy should trigger on iteration 1"

    projected_1 = project_included_messages(shallow_copy_1)
    assert len(projected_1) < len(shallow_copy_1), "Some messages should be excluded"

    # Verify some original Message objects now have _excluded=True (shared)
    excluded_in_original = [m for m in history if m.additional_properties.get(EXCLUDED_KEY, False)]
    assert len(excluded_in_original) > 0, (
        "Shared Message objects should have _excluded=True after strategy runs on the copy"
    )

    # ── Simulate tool loop: add new batch (edit_file) to the accumulator ──
    batch2 = _build_tool_group("t2_edit", "edit_file", "ok")
    history.extend(batch2)

    # ── Iteration 2: same strategy, higher max_context so usage is below trigger ──
    strategy.max_context_tokens = 1_000_000
    shallow_copy_2 = list(history)
    changed = await strategy(shallow_copy_2)
    assert not changed, "Strategy should NOT trigger on iteration 2"

    projected_2 = project_included_messages(shallow_copy_2)

    # ── KEY ASSERTIONS ──
    # 1. Newly added batch2 messages must NOT be excluded
    for m in batch2:
        assert not m.additional_properties.get(EXCLUDED_KEY, False), (
            "New tool results from batch 2 should not be excluded"
        )

    # 2. Batch 2 messages should appear in the projected output
    batch2_ids = {id(m) for m in batch2}
    projected_ids = {id(m) for m in projected_2}
    assert batch2_ids <= projected_ids, "Newly added batch 2 messages must survive projection"


async def test_no_double_compaction_across_tool_loop_iterations():
    """Regression: compaction must not re-compact already-compacted groups
    when a later compaction pass receives a copied list.

    Chrys keeps summaries on the active loop list; this keeps the defensive
    copied/replayed-list path covered. The strategy must cache
    and re-inject summaries so that:
    1. Already-compacted groups keep ``_excluded=True`` and are skipped
    2. The callback fires only once per group (no duplicate events)
    3. ``project_included_messages`` returns summaries (not bare originals)
    """
    # Build a multi-turn conversation that triggers compaction
    # Turn 1: old turn with large tool results
    history: list[Message] = [
        _user("Turn 1 question"),
        *_build_tool_group("t1_c0", "search", "x" * 3000),
        *_build_tool_group("t1_c1", "read_file", "x" * 3000),
        _assistant_text("Turn 1 answer"),
    ]
    # Turn 2 (current): tool loop in progress
    history.append(_user("Turn 2 question"))
    batch1 = [
        *_build_tool_group("t2_glob", "glob", "x" * 2000),
        *_build_tool_group("t2_grep", "grep", "x" * 2000),
    ]
    history.extend(batch1)

    total = _estimate_tokens(history)

    callback_events: list[CompactionInfo] = []

    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        target_pct=0.50,
        on_compaction=_async_appender(callback_events),
    )

    # ── Iteration 1: framework shallow-copies, strategy compacts ──
    copy_1 = list(history)
    assert await strategy(copy_1), "Strategy should compact on iteration 1"
    project_included_messages(copy_1)

    # Should have fired callback(s) for phase 1
    assert len(callback_events) > 0, "Callback should fire on iteration 1"
    events_after_iter1 = len(callback_events)

    # ── Simulate tool loop: LLM responds, adds a new tool call ──
    batch2 = _build_tool_group("t2_edit", "edit_file", "ok")
    history.extend(batch2)

    # ── Iteration 2: same strategy, new copy — should NOT re-compact ──
    copy_2 = list(history)
    await strategy(copy_2)
    projected_2 = project_included_messages(copy_2)

    # The key assertions:
    # 1. No additional callback events from re-compacting the same groups
    assert len(callback_events) == events_after_iter1, (
        f"Expected {events_after_iter1} callback events but got {len(callback_events)}. "
        f"Strategy fired duplicate compaction events on iteration 2."
    )

    # 2. Projected messages should include summaries (not bare originals)
    projected_texts = [m.text or "" for m in projected_2]
    has_summary = any("[Tool call:" in t for t in projected_texts)
    assert has_summary, (
        "Projected messages should contain summary text from compaction, but no summaries found — re-injection failed."
    )

    # 3. No original excluded messages should leak through
    excluded_in_projected = [m for m in projected_2 if m.additional_properties.get(EXCLUDED_KEY, False)]
    assert len(excluded_in_projected) == 0, "No excluded messages should appear in projected output"


# ---------------------------------------------------------------------------
# Session restore
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("turns", "groups_per_turn", "result_size", "trigger_pct", "target_pct", "required_reason"),
    [
        pytest.param(4, 3, 3000, 0.90, 0.50, None, id="phase1"),
        pytest.param(4, 3, 3000, 0.90, 0.10, "budget_tool_removal", id="phase2"),
        pytest.param(6, 4, 5000, 0.85, 0.50, None, id="large_session"),
    ],
)
async def test_session_restore_preserves_compaction_exclusions(
    turns: int,
    groups_per_turn: int,
    result_size: int,
    trigger_pct: float,
    target_pct: float,
    required_reason: str | None,
) -> None:
    """A fresh strategy must not clear _excluded flags set by prior Phase 1/2 compaction.

    Simulates session restore: run strategy #1 to compact, then create a fresh
    strategy #2 (empty caches) and run it on the same messages. The flags from
    strategy #1 must be preserved, so ``included_token_count`` stays close to
    the post-compaction value instead of inflating back to the all-visible
    level (the reported ~88K -> ~157K regression). A very low target forces
    Phase 2 removal on top of the Phase 1 summaries.
    """
    messages = _build_multi_turn(turns, groups_per_turn=groups_per_turn, result_size=result_size)
    total = _estimate_tokens(messages)

    strategy1 = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=trigger_pct,
        target_pct=target_pct,
        satisfied_pct=target_pct,
    )
    changed = await strategy1(messages)
    assert changed, "Strategy #1 should have compacted"
    if required_reason is not None:
        removals = [m for m in messages if m.additional_properties.get(EXCLUDE_REASON_KEY) == required_reason]
        assert removals, f"Strategy #1 should have excluded groups with reason {required_reason!r}"

    excluded_after_s1 = {id(m) for m in messages if m.additional_properties.get(EXCLUDED_KEY, False)}
    tokens_after_s1 = included_token_count(messages)
    assert excluded_after_s1, "Strategy #1 should have excluded some messages"

    # What the inflated count WOULD be if every flag were cleared: all
    # annotated groups visible.
    all_visible_tokens = 0
    for m in messages:
        ann = m.additional_properties.get("_group", {})
        tc = ann.get("token_count")
        if isinstance(tc, int):
            all_visible_tokens += tc
    assert all_visible_tokens > tokens_after_s1 * 1.3, (
        "Test setup: all-visible should be significantly larger than compacted"
    )

    # Create a fresh strategy (simulating session restore — empty caches)
    strategy2 = _make_strategy(max_context_tokens=total + 100, trigger_pct=trigger_pct, target_pct=target_pct)
    assert not strategy2._summary_cache
    assert not strategy2._removed_group_ids

    events: list[CompactionInfo] = []
    strategy2._on_compaction = _async_appender(events)
    changed2 = await strategy2(messages)

    excluded_after_s2 = {id(m) for m in messages if m.additional_properties.get(EXCLUDED_KEY, False)}
    tokens_after_s2 = included_token_count(messages)

    # The fresh strategy must not un-exclude previously compacted messages
    assert excluded_after_s1 <= excluded_after_s2, "Fresh strategy cleared flags that were set by prior compaction"
    assert tokens_after_s2 <= tokens_after_s1 * 1.05, (
        f"Token count inflated after fresh strategy: {tokens_after_s2:,} vs {tokens_after_s1:,} "
        f"(all-visible would be {all_visible_tokens:,})"
    )
    # No redundant compaction callback should fire
    assert not events, f"Fresh strategy fired {len(events)} compaction events (expected 0)"
    assert not changed2


async def test_fresh_strategy_clears_non_compaction_excluded():
    """Flags without compaction _exclude_reason must still be cleared by a fresh strategy."""
    messages = _build_multi_turn(2, groups_per_turn=2, result_size=500)

    # Manually set _excluded on some messages WITHOUT a compaction reason
    messages[1].additional_properties[EXCLUDED_KEY] = True
    messages[2].additional_properties[EXCLUDED_KEY] = True

    strategy = _make_strategy(max_context_tokens=1_000_000)
    await strategy(messages)

    # Those manually-excluded messages should have been cleared
    assert not messages[1].additional_properties.get(EXCLUDED_KEY, False)
    assert not messages[2].additional_properties.get(EXCLUDED_KEY, False)


async def test_session_restore_with_if_branch_preserves_old_flags():
    """When the current session HAS done new compaction (if branch), old
    compaction flags from a prior session must still be preserved."""
    # Build messages, compact with strategy #1
    messages = _build_multi_turn(4, groups_per_turn=3, result_size=3000)
    total_before = _estimate_tokens(messages)

    strategy1 = _make_strategy(
        max_context_tokens=total_before + 100,
        trigger_pct=0.90,
        target_pct=0.50,
    )
    await strategy1(messages)
    excluded_after_s1 = {id(m) for m in messages if m.additional_properties.get(EXCLUDED_KEY, False)}

    # Add a new turn with lots of content (simulating new user interaction)
    new_turn = [
        _user("New big request"),
        *_build_tool_group("new_c0", "search", "z" * 5000),
        *_build_tool_group("new_c1", "read_file", "z" * 5000),
        *_build_tool_group("new_c2", "write_file", "z" * 5000),
        _assistant_text("New big response"),
    ]
    messages.extend(new_turn)
    total_with_new = _estimate_tokens(messages)

    # Create strategy #2 (fresh — simulating session restore) and compact
    strategy2 = _make_strategy(
        max_context_tokens=total_with_new + 100,
        trigger_pct=0.85,
        target_pct=0.50,
    )
    await strategy2(messages)

    # Old excluded messages must still be excluded
    for m in messages:
        if id(m) in excluded_after_s1:
            assert m.additional_properties.get(EXCLUDED_KEY, False), "Old compaction flag was cleared by strategy #2"


# ---------------------------------------------------------------------------
# Anchor consumption and retry snapshots
# ---------------------------------------------------------------------------


async def test_stale_current_turn_drop_anchors_do_not_exclude_next_turn_twin() -> None:
    """Exclusion anchors are single-use — consumed by the after_run persist.

    Under-trigger turns return before ``_snapshot_excluded_anchors``, so
    without consumption the previous trigger-passing pass's anchors stay
    resident and ``after_run`` replays them every turn.  Pass 3 structural
    matching scans newest-first, so a later turn that emits a structural
    twin of a dropped message (repeated identical tool call or byte-equal
    assistant text) would bind the NEW live message and silently exclude
    it — with no code path ever clearing a ``current_turn_drop`` flag.
    """
    messages = _build_single_turn(2, result_size=1_000)
    strategy = _forced_phase4(
        messages,
    )

    changed = await strategy(messages)

    assert changed
    dropped = [m for m in messages if m.additional_properties.get(EXCLUDE_REASON_KEY) == "current_turn_drop"]
    assert dropped
    assert strategy._excluded_anchors

    # after_run of the triggering turn.  Streaming finalizers rebuild the
    # current-turn response messages before storing them, so state holds
    # structural twins: distinct objects without the wire-minted
    # message_id — exactly the pass-3 binding path.
    state_messages = [
        _user("start"),
        *_build_tool_group("call_0", "tool_0", "x" * 1_000),
        *_build_tool_group("call_1", "tool_1", "x" * 1_000),
        _assistant_text("done"),
    ]
    strategy.persist_exclusions_to_state(state_messages)

    first_pass_excluded = [
        m for m in state_messages if m.additional_properties.get(EXCLUDE_REASON_KEY) == "current_turn_drop"
    ]
    assert len(first_pass_excluded) == len(dropped)
    # Consumed: one snapshot pass, one persist pass.
    assert strategy._excluded_anchors == []

    # Next turn stays under trigger (no compaction, no re-snapshot) and
    # happens to repeat the same tool call and closing text.  Its
    # after_run persist must not bind the resident anchors onto these
    # live messages.
    next_turn = [
        _user("again"),
        *_build_tool_group("call_0", "tool_0", "x" * 1_000),
        _assistant_text("done"),
    ]
    state_messages.extend(next_turn)
    strategy.persist_exclusions_to_state(state_messages)

    for live in next_turn:
        assert live.additional_properties.get(EXCLUDED_KEY, False) is False


async def test_retry_rollback_snapshot_discards_attempt_anchors() -> None:
    """Retry rollback restores anchor state captured at attempt entry.

    Phase 4 can commit mid-attempt and record current-turn-drop anchors; a
    transient-error rollback then restores pre-attempt history, so the
    dropped messages never reach state.  Replaying those anchors at
    after_run would let the newest-first structural fallback bind the
    successful retry's identical calls/text and silently exclude live
    messages — the retry loop therefore restores the anchor list together
    with history.
    """
    messages = _build_single_turn(2, result_size=1_000)
    strategy = _forced_phase4(
        messages,
    )

    pre_attempt = strategy.snapshot_retry_state()
    changed = await strategy(messages)
    assert changed
    assert strategy._excluded_anchors

    strategy.restore_retry_state(pre_attempt)
    assert strategy._excluded_anchors == []

    # The successful retry emits the same tool calls and closing text;
    # after_run persists against state holding those live twins — they
    # must stay included.
    state_messages = [
        _user("start"),
        *_build_tool_group("call_0", "tool_0", "x" * 1_000),
        *_build_tool_group("call_1", "tool_1", "x" * 1_000),
        _assistant_text("done"),
    ]
    strategy.persist_exclusions_to_state(state_messages)
    for live in state_messages:
        assert live.additional_properties.get(EXCLUDED_KEY, False) is False


def test_retry_state_restore_keeps_pre_attempt_anchors() -> None:
    """Anchors predating the attempt are restored, not wiped.

    Anchors surviving an interrupted prior run (after_run never fired) are
    still awaiting their one persist pass; a retry rollback inside the
    next run must put them back exactly as captured.
    """
    strategy = _make_strategy()
    prior = _assistant_text("from prior run")
    prior.additional_properties[EXCLUDED_KEY] = True
    prior.additional_properties[EXCLUDE_REASON_KEY] = "current_turn_drop"
    strategy._snapshot_excluded_anchors([prior])
    pre_attempt = strategy.snapshot_retry_state()
    assert pre_attempt

    # Mid-attempt Phase 4 replaces the list before the attempt fails.
    strategy._excluded_anchors = []
    strategy.restore_retry_state(pre_attempt)
    assert tuple(strategy._excluded_anchors) == pre_attempt.anchors
