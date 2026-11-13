# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the UnifiedContextStrategy pass as a whole: trigger gating, group identity, callbacks, cascades."""

import os
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from PIL import Image

from chrys.kernel import (
    EXCLUDED_KEY,
    Content,
    Message,
    annotate_message_groups,
    included_token_count,
)
from chrys.kernel import compaction as chrys_compaction
from chrys.service.context.compaction import (
    CompactionInfo,
    MixedLanguageTokenizer,
    PreCompactInfo,
    UnifiedContextStrategy,
    _content_signature,
    _dedup_message_ids,
    _group_id,
)
from chrys.service.context.providers.history import (
    CompressibleHistoryProvider,
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
    _tokenizer,
    _tool_result,
    _user,
)
from tests.support.phase4_stubs import StubReminderMiddleware


def _generated_png_bytes(size: tuple[int, int] = (512, 512)) -> bytes:
    image = Image.effect_noise(size, 100).convert("RGB")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


# ---------------------------------------------------------------------------
# Trigger gating and token estimation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["text_only", "tool_groups"])
async def test_no_compaction_under_trigger(shape: str) -> None:
    """Under ``trigger_pct`` nothing changes and the compaction callback never fires."""
    if shape == "text_only":
        messages = [_user("hello"), _assistant_text("hi")]
    else:
        messages = _build_single_turn(3, result_size=500)

    received: list[CompactionInfo] = []
    strategy = _make_strategy(max_context_tokens=1_000_000, on_compaction=_async_appender(received))
    changed = await strategy(messages)
    assert not changed
    assert received == []


@pytest.mark.parametrize(
    ("prompt", "n_images", "expected_image_tokens", "ceiling"),
    [
        pytest.param("describe this @clipboard-image.png", 1, 255, 20_000, id="single"),
        pytest.param("compare @first.png and @second.png", 2, 510, 25_000, id="multiple"),
    ],
)
async def test_png_attachment_does_not_trigger_auto_compaction_at_low_usage(
    prompt: str, n_images: int, expected_image_tokens: int, ceiling: int
) -> None:
    """Large PNG payloads are budgeted (and summed) as image tokens, not as base64 text."""
    data = _generated_png_bytes()
    assert len(data) > 300_000
    messages = [
        _user("x" * 40_000),
        _assistant_text("answered"),
        Message(
            role="user",
            contents=[Content.from_text(prompt), *(Content.from_data(data, "image/png") for _ in range(n_images))],
        ),
    ]
    assert chrys_compaction.estimate_message_image_tokens(messages[-1]) == expected_image_tokens
    events: list[CompactionInfo] = []
    strategy = _make_strategy(max_context_tokens=100_000, on_compaction=_async_appender(events))

    changed = await strategy(messages)

    assert not changed
    assert events == []
    assert strategy.last_included_tokens < ceiling


async def test_a_note_a_user_message_no_longer_carries_does_not_count_toward_the_trigger() -> None:
    """Each call's copy of a user message shares its count with history, so the count must follow this call's copy."""
    messages = _build_multi_turn(6)
    opener_index = [index for index, message in enumerate(messages) if message.role == "user"][3]
    opener = messages[opener_index]
    # The call that last counted this opener sent it with a last-words note.
    noted = Message(
        "user", [*opener.contents, Content.from_text("progress note " * 3_000)], message_id=opener.message_id
    )
    noted.additional_properties = opener.additional_properties
    messages[opener_index] = noted
    stale_tokens = _estimate_tokens(messages)
    messages[opener_index] = opener
    messages.append(_user("Turn 7 request"))
    events: list[CompactionInfo] = []
    strategy = _make_strategy(max_context_tokens=14_000, trigger_pct=0.85, on_compaction=_async_appender(events))

    changed = await strategy(messages)

    assert not changed
    assert events == []
    tokens = sum(chrys_compaction._message_token_estimate(message, _tokenizer) for message in messages)
    assert strategy.last_included_tokens == tokens
    assert tokens < 0.85 * 14_000 < stale_tokens


async def test_no_tool_groups_no_change():
    """No-op when there are no tool-call groups, even if over trigger."""
    messages = [_user("hello"), _assistant_text("world " * 500)]
    strategy = _make_strategy(max_context_tokens=100, trigger_pct=0.01, target_pct=0.005)
    changed = await strategy(messages)
    assert not changed


# ---------------------------------------------------------------------------
# Constructor validation and group identity
# ---------------------------------------------------------------------------


def test_invalid_params():
    """Constructor rejects invalid parameters."""
    with pytest.raises(ValueError, match="max_context_tokens"):
        UnifiedContextStrategy(max_context_tokens=0)
    with pytest.raises(ValueError, match="trigger_pct"):
        UnifiedContextStrategy(trigger_pct=0.0)
    with pytest.raises(ValueError, match="target_pct"):
        UnifiedContextStrategy(trigger_pct=0.85, target_pct=0.90)


def test_content_signature_distinguishes_function_result_images():
    first = Message(
        "tool",
        [
            Content.from_function_result(
                "call_1",
                result=[Content.from_text("same"), Content.from_data(b"first-image", "image/png")],
            )
        ],
    )
    second = Message(
        "tool",
        [
            Content.from_function_result(
                "call_1",
                result=[Content.from_text("same"), Content.from_data(b"second-image", "image/png")],
            )
        ],
    )

    assert _content_signature(first) != _content_signature(second)


def test_content_signature_ignores_unknown_function_result_uri_without_crashing():
    message = Message(
        "tool",
        [
            Content.from_function_result(
                "call_1",
                result=[Content.from_text("same"), Content.from_uri("https://example.com/blob")],
            )
        ],
    )

    assert _content_signature(message)


def test_dedup_message_ids_unique():
    """Unique IDs are left unchanged."""
    msgs = [
        Message("user", ["hi"], message_id="msg_1"),
        Message("assistant", ["hey"], message_id="msg_2"),
    ]
    _dedup_message_ids(msgs)
    assert msgs[0].message_id == "msg_1"
    assert msgs[1].message_id == "msg_2"


def test_dedup_message_ids_duplicates():
    """Duplicate IDs get positional suffixes."""
    msgs = [
        Message("user", ["hi"], message_id="msg_1"),
        Message("assistant", ["a"], message_id="msg_2"),
        Message("user", ["q2"], message_id="msg_1"),
        Message("assistant", ["b"], message_id="msg_2"),
    ]
    _dedup_message_ids(msgs)
    assert msgs[0].message_id == "msg_1"
    assert msgs[1].message_id == "msg_2"
    assert msgs[2].message_id == "msg_1_i2"
    assert msgs[3].message_id == "msg_2_i3"


def test_dedup_message_ids_no_id():
    """Messages without IDs are left untouched."""
    msgs = [Message("user", ["hi"]), Message("user", ["bye"])]
    _dedup_message_ids(msgs)
    assert msgs[0].message_id is None
    assert msgs[1].message_id is None


async def test_dedup_renames_persist_on_state_messages():
    """Dedup renames land on stored history because SessionContext retains message aliases.

    The kernel ``SessionContext.extend_messages`` does not copy, so the wire
    list the strategy dedups holds the very state objects — renamed
    ``message_id`` values persist across runs (and to disk). Code doing
    message_id-based cross-run accounting must not assume ids are immutable.
    """
    state_messages = [
        Message("user", ["hi"], message_id="dup"),
        Message("user", ["again"], message_id="dup"),
    ]
    strategy = _make_strategy()  # budget far above usage: no compaction fires

    await strategy(state_messages)

    assert state_messages[0].message_id == "dup"
    assert state_messages[1].message_id == "dup_i1"


def test_dedup_message_ids_separates_cross_run_groups():
    """Cross-run tool calls that share message ids become separate groups once deduplicated."""
    messages = [
        Message("user", ["What does file A do?"], message_id="msg_1"),
        _assistant_tool_call("call_r1", "read_file"),
        _tool_result("call_r1", "file A content: " + "a" * 3000),
        _assistant_text("File A does X."),
        Message("user", ["Now read file B."], message_id="msg_5"),
        _assistant_tool_call("call_r2", "read_file"),
        _tool_result("call_r2", "file B content: " + "b" * 3000),
        _assistant_text("File B does Y."),
    ]
    messages[1].message_id = "msg_2"
    messages[2].message_id = "msg_3"
    messages[5].message_id = "msg_2"  # duplicate!
    messages[6].message_id = "msg_3"  # duplicate!

    _dedup_message_ids(messages)
    annotate_message_groups(messages)
    group_ids = set()
    for m in messages:
        ga = m.additional_properties.get("_group", {})
        if ga.get("kind") == "tool_call":
            group_ids.add(ga.get("id"))
    assert len(group_ids) == 2, f"expected 2 separate groups, got {len(group_ids)}"


async def test_duplicate_ids_compaction_with_dedup():
    """With dedup, compaction correctly treats cross-run tool calls as separate groups."""
    messages = [Message("user", ["start"], message_id="u1")]
    for run in range(3):
        messages.append(
            Message(
                "assistant",
                [Content.from_function_call(f"call_run{run}", "read_file", arguments={})],
                message_id="msg_2",
            )
        )
        messages.append(
            Message(
                "tool",
                [Content.from_function_result(f"call_run{run}", result=f"run {run}: " + "x" * 2000)],
                message_id="msg_3",
            )
        )

    strategy = _forced_phase4(
        messages,
    )
    changed = await strategy(messages)
    assert changed

    # With dedup: 3 separate groups. Phase 4 drops ALL current-turn groups
    # once LAST_WORDS is produced — so all 3 should be excluded.
    excluded_groups = {
        _group_id(m) for m in messages if m.additional_properties.get(EXCLUDED_KEY, False) and _group_id(m) is not None
    }
    assert len(excluded_groups) == 3, f"Expected 3 excluded groups (drop-all), got {len(excluded_groups)}"


# ---------------------------------------------------------------------------
# Compaction callbacks and event payloads
# ---------------------------------------------------------------------------


async def test_on_compaction_called_with_correct_info():
    """Callback receives CompactionInfo with accurate group count, tool names, token deltas and note flag."""
    messages = _build_single_turn(6, result_size=2000)
    measured_before = _estimate_tokens(messages)

    received: list[CompactionInfo] = []
    strategy = _forced_phase4(messages, on_compaction=_async_appender(received))
    changed = await strategy(messages)
    assert changed

    # Phase 4 drops all 6 tool-call groups + the trailing assistant_text group.
    assert len(received) >= 1
    p4 = [r for r in received if r.phase == "phase4"]
    assert len(p4) == 1
    assert p4[0].compacted_groups == 7  # 6 tool-call groups + 1 assistant_text
    assert p4[0].tool_names == [f"tool_{i}" for i in range(6)]
    assert p4[0].tokens_before > p4[0].tokens_after
    assert p4[0].tokens_after > 0
    assert p4[0].last_words_generated is True

    # Every callback has consistent token deltas and tool names: Phase 4 drops
    # tool_call AND inline assistant_text groups, while tool_names only lists
    # function names from tool_call groups.
    for info in received:
        assert info.compacted_groups >= 1
        assert len(info.tool_names) <= info.compacted_groups
        assert info.tokens_before > info.tokens_after
    # tokens_before / tokens_after match independent measurement.
    assert received[0].tokens_before == measured_before
    assert received[-1].tokens_after == included_token_count(messages)

    # With drop-all semantics every tool_call/result message in the current
    # turn is excluded — only the user message survives.
    excluded = [m for m in messages if m.additional_properties.get(EXCLUDED_KEY, False) and _group_id(m) is not None]
    assert len(excluded) >= 6  # at least one per tool-call group (call + result)


async def test_on_pre_compact_called_before_phase4():
    messages = _build_single_turn(4, result_size=2000)

    received: list[PreCompactInfo] = []
    strategy = _forced_phase4(
        messages,
        on_pre_compact=_async_appender(received),
    )

    await strategy(messages)

    assert [info.trigger for info in received] == ["phase4"]
    assert received[0].usage_pct > 0
    assert received[0].tokens_before > 0


# ---------------------------------------------------------------------------
# Whole-pass integration: cascade, multi-turn, sub-agent, debug dump, after_run
# ---------------------------------------------------------------------------


async def test_full_tool_phase_cascade():
    """Old-tool phases and Phase 4 fire in order when target is impossibly low."""
    # 3 old turns + 1 current turn, many groups
    messages = _build_multi_turn(4, groups_per_turn=4, result_size=3000)

    received: list[CompactionInfo] = []
    strategy = _forced_phase4(
        messages,
        on_compaction=_async_appender(received),
    )
    changed = await strategy(messages)
    assert changed

    phases_fired = [r.phase for r in received]
    # Old-tool phases and current-turn Phase 4 should have fired in order.
    assert "phase1" in phases_fired
    assert "phase2" in phases_fired
    assert "phase4" in phases_fired

    # Verify ordering: phase1 before phase2 before phase4
    phase_order = [p for p in phases_fired if p.startswith("phase")]
    for i in range(len(phase_order) - 1):
        assert phase_order[i] <= phase_order[i + 1], f"Phases fired out of order: {phases_fired}"


async def test_reminder_debug_dump_written_on_compaction(tmp_path):
    """A changed compaction dumps the injected LAST_WORDS reminder, owner-only."""
    messages = _build_multi_turn(3, groups_per_turn=2, result_size=2000)
    total = _estimate_tokens(messages)
    reminder = StubReminderMiddleware()
    reminder.last_words.set_last_words("[dump-me progress note]")
    debug_dir = tmp_path / "compactions" / "debug"
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        target_pct=0.50,
        reminder_middleware=reminder,
        debug_log_dir=debug_dir,
    )
    changed = await strategy(messages)
    assert changed

    dumps = sorted(debug_dir.glob("reminder_*.log"))
    assert len(dumps) == 1
    text = dumps[0].read_text(encoding="utf-8")
    assert text.startswith("<system-reminder>\n[LAST_WORDS]")
    assert "[dump-me progress note]" in text
    if os.name == "posix":
        assert (dumps[0].stat().st_mode & 0o777) == 0o600


async def test_reminder_debug_dump_skipped_without_debug_dir_or_content(tmp_path):
    """No debug dir or nothing to inject -> no dump and no error."""
    messages = _build_multi_turn(3, groups_per_turn=2, result_size=2000)
    total = _estimate_tokens(messages)
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        target_pct=0.50,
    )
    assert await strategy(messages)

    # Empty reminder state renders None -> nothing written even with a dir.
    messages = _build_multi_turn(3, groups_per_turn=2, result_size=2000)
    total = _estimate_tokens(messages)
    debug_dir = tmp_path / "compactions" / "debug"
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.90,
        target_pct=0.50,
        debug_log_dir=debug_dir,
    )
    assert await strategy(messages)
    # render returned None before any filesystem work — the dir is never created
    assert not debug_dir.exists()


async def test_multi_turn_progressive_compaction():
    """Progressive: each call compacts oldest turns first as context grows."""
    # 4 turns with moderate content
    messages = _build_multi_turn(4, groups_per_turn=2, result_size=2000)
    total = _estimate_tokens(messages)

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.90,
        target_pct=0.50,
    )
    changed = await strategy(messages)
    assert changed

    # Verify summaries exist (from old turns being compacted)
    summaries = [m for m in messages if m.text and m.text.startswith("[Tool call:")]
    assert len(summaries) > 0

    total_after = included_token_count(messages)
    assert total_after < total


async def test_single_turn_sub_agent_only_fires_phase4():
    """Sub-agents are single-turn: only phase 4 should fire (current turn removal).

    Simulates the Explore sub-agent scenario: one user message followed by
    many tool calls.  With only one turn, ``previous_turns`` is empty so
    phases 1-2 are no-ops.  Phase 4 drops every current-turn tool-call
    group once LAST_WORDS has been produced.
    """
    # Single turn: user + 10 tool groups + assistant (simulates Explore sub-agent)
    messages = _build_single_turn(10, result_size=2000)
    total = _estimate_tokens(messages)

    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.50,
        on_compaction=_async_appender(received),
    )
    changed = await strategy(messages)
    assert changed

    # Only phase 4 callbacks — never phase 1/2
    assert all(info.phase == "phase4" for info in received), (
        f"Expected only phase4, got phases: {[info.phase for info in received]}"
    )
    assert len(received) == 1
    assert received[0].compacted_groups > 0
    # Phase 4 reports the resolver's current-span absolute number (§4.1).
    assert received[0].turn_numbers == [1]


async def test_after_run_callback_not_suppressed_by_stale_summary_cache():
    """Regression: after_run compaction callback must fire when no intra-run
    compaction happened, even if _summary_cache has entries from a prior run.

    Scenario:
      - Run 1: compaction triggers during tool loop → _summary_cache populated,
        callback fires correctly.
      - Run 2: usage grows but only crosses the threshold in after_run
        (not during the tool loop).  The after_run compaction callback must
        still fire because no intra-run compaction occurred in THIS run.

    Before the fix, ``already_compacted = bool(_summary_cache)`` was always
    True after the first-ever compaction, permanently suppressing after_run
    callbacks and hiding Compact:Phase* events from the debug panel.
    """
    tokenizer = MixedLanguageTokenizer()

    # ── Setup: strategy + callback tracking ──
    callback_events: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=500,
        trigger_pct=0.80,
        target_pct=0.50,
        on_compaction=_async_appender(callback_events),
        tokenizer=tokenizer,
    )

    provider = CompressibleHistoryProvider(compaction_strategy=strategy)

    # Minimal mocks for lifecycle hooks
    mock_agent = MagicMock()
    mock_session = MagicMock()
    mock_session.state = {}
    mock_context = MagicMock()
    mock_context.session_id = "test-session"

    # ── Run 1: build history that triggers compaction ──
    state_run1: dict = {
        "messages": _build_multi_turn(3, groups_per_turn=2, result_size=300),
        "compressed_msgs": [],
        "turn_counter": 3,
    }

    # Simulate before_run
    await provider.before_run(agent=mock_agent, session=mock_session, context=mock_context, state=state_run1)

    # Simulate intra-run compaction (framework calls strategy during tool loop)
    changed = await strategy(list(state_run1["messages"]))
    assert changed, "Run 1: strategy should trigger compaction"
    # Phase 4 populates _removed_group_ids (and Phase 1 may populate _summary_cache)
    compaction_state_run1 = len(strategy._summary_cache) + len(strategy._removed_group_ids)
    assert compaction_state_run1 > 0, "Run 1: compaction state should be populated"
    events_after_run1_intra = len(callback_events)
    assert events_after_run1_intra > 0, "Run 1: intra-run callback should have fired"

    # Simulate after_run — callback should be suppressed (intra-run already fired)
    await provider.after_run(agent=mock_agent, session=mock_session, context=mock_context, state=state_run1)
    events_after_run1_after = len(callback_events)
    # after_run may or may not compact further, but if it does the callback is
    # correctly suppressed because intra-run already handled it
    assert events_after_run1_after == events_after_run1_intra, (
        "Run 1: after_run should not produce duplicate callback events"
    )

    # ── Run 2: add more messages, but DON'T trigger intra-run compaction ──
    new_turn_messages = [
        _user("Run 2 question"),
        *_build_tool_group("r2_c0", "search", "y" * 300),
        *_build_tool_group("r2_c1", "read_file", "y" * 300),
        _assistant_text("Run 2 response"),
    ]
    state_run2 = state_run1  # same state dict, as engine reuses it
    state_run2["messages"].extend(new_turn_messages)

    # Simulate before_run for run 2 — this snapshots _summary_cache size
    await provider.before_run(agent=mock_agent, session=mock_session, context=mock_context, state=state_run2)

    # Track compaction state before after_run
    state_before = len(strategy._summary_cache) + len(strategy._removed_group_ids)

    # Now simulate after_run — compaction should trigger AND callback must fire
    # because no intra-run compaction happened in run 2.
    events_before_run2_after = len(callback_events)
    await provider.after_run(agent=mock_agent, session=mock_session, context=mock_context, state=state_run2)

    # The key assertion: if after_run compacted anything, the callback must
    # have fired.  We check by seeing if compaction state grew.
    state_after = len(strategy._summary_cache) + len(strategy._removed_group_ids)
    compaction_happened_in_after_run = state_after > state_before

    if compaction_happened_in_after_run:
        assert len(callback_events) > events_before_run2_after, (
            f"Run 2 after_run compacted but callback was suppressed. "
            f"This means Compact:Phase* events are lost. "
            f"Compaction state was {state_before} before, {state_after} after."
        )
