# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for turn resolution (``_resolve_turns``) and its strategy-level integration pins."""

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    Content,
    Message,
    project_included_messages,
)
from chrys.kernel.loop import _wire_message_view
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.context.compaction import (
    _REASON_CURRENT_TURN_DROP,
    CompactionInfo,
    PreCompactInfo,
    _resolve_turns,
)
from chrys.service.context.compaction.scoped import DEGRADED_SCOPED_PREAMBLE
from chrys.service.context.compaction.turns import _state_correspondence
from chrys.service.context.providers.history import (
    CompressedBlock,
    CompressibleHistoryProvider,
)
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _async_appender,
    _build_tool_group,
    _estimate_tokens,
    _forced_phase4,
    _has_call_id,
    _injected,
    _make_strategy,
    _nudge,
    _scoped_user_texts,
    _status_marker,
    _turn_marker,
    _user,
    _wire_view,
)
from tests.support.phase4_stubs import StubLastWordsGenerator
from tests.support.reminder_stack import reminder_pair


def _spans(resolved) -> list[tuple[int, int]]:
    return [(s.start, s.end) for s in resolved.spans]


# ---------------------------------------------------------------------------
# Turn resolution unit tests
# ---------------------------------------------------------------------------


class TestResolveTurnsStrict:
    """Stateless (unbound-strategy) resolution keeps legacy strict slicing."""

    def test_empty_messages(self) -> None:
        resolved = _resolve_turns([], None)
        assert resolved.spans == []
        assert not resolved.degraded

    def test_single_turn(self) -> None:
        resolved = _resolve_turns([_user("hello"), _assistant_text("hi")], None)
        assert _spans(resolved) == [(0, 2)]
        assert resolved.current.absolute_number == 1

    def test_multiple_turns(self) -> None:
        msgs = [
            _user("q1"),
            _assistant_text("a1"),
            _user("q2"),
            _assistant_text("a2"),
            _user("q3"),
            _assistant_text("a3"),
        ]
        resolved = _resolve_turns(msgs, None)
        assert _spans(resolved) == [(0, 2), (2, 4), (4, 6)]
        assert [s.absolute_number for s in resolved.spans] == [1, 2, 3]
        assert not resolved.degraded

    def test_prefix_before_first_opener(self) -> None:
        """Compressed summaries before the first opener are not part of any turn."""
        summary = _assistant_text("[Compressed context: ctx_abc]")
        resolved = _resolve_turns([summary, _user("q1"), _assistant_text("a1")], None)
        assert _spans(resolved) == [(1, 3)]

    def test_no_user_messages_rule5(self) -> None:
        resolved = _resolve_turns([_assistant_text("hello"), _assistant_text("world")], None)
        assert resolved.spans == []
        assert not resolved.degraded

    def test_injected_and_nudge_do_not_split(self) -> None:
        """An injected turn is ONE slice — the §2.1 bug family fix."""
        msgs = [
            _user("q1"),
            _assistant_text("a1"),
            _user("q2"),
            _assistant_text("working"),
            _injected("mid-turn note"),
            _assistant_text("more work"),
            _nudge(),
            _assistant_text("done"),
        ]
        resolved = _resolve_turns(msgs, None)
        assert _spans(resolved) == [(0, 2), (2, 8)]
        assert not resolved.degraded

    def test_legacy_unflagged_guidance_still_splits(self) -> None:
        """Documented degradation (§6): pre-flag histories keep the old split."""
        msgs = [_user("q1"), _assistant_text("a1"), _user("continue"), _assistant_text("a2")]
        resolved = _resolve_turns(msgs, None)
        assert _spans(resolved) == [(0, 2), (2, 4)]

    def test_excluded_opener_does_not_reopen_turn(self) -> None:
        """A mid-call fold (Step 0 / P3) excludes a completed turn in place;
        its opener must not keep opening a turn the state no longer holds."""
        folded_opener = _user("q1")
        folded_opener.additional_properties[EXCLUDED_KEY] = True
        folded_answer = _assistant_text("a1")
        folded_answer.additional_properties[EXCLUDED_KEY] = True
        msgs = [folded_opener, folded_answer, _user("q2"), _assistant_text("a2")]
        resolved = _resolve_turns(msgs, None)
        assert _spans(resolved) == [(2, 4)]
        assert not resolved.degraded

    def test_excluded_opener_degrades_to_flagged_tail(self) -> None:
        """Post-fold, a flagged-only visible tail degrades — the note quotes
        the guidance, never the folded opener."""
        folded_opener = _user("q1")
        folded_opener.additional_properties[EXCLUDED_KEY] = True
        folded_answer = _assistant_text("a1")
        folded_answer.additional_properties[EXCLUDED_KEY] = True
        guidance = _injected("resume with flag X")
        msgs = [folded_opener, folded_answer, guidance, _assistant_text("work")]
        resolved = _resolve_turns(msgs, None)
        assert resolved.degraded
        assert _spans(resolved) == [(0, 4)]
        assert resolved.synthetic_opener_idx == 2

    def test_legacy_split_coexists_with_flagged_turns(self) -> None:
        """A restored pre-change session still splits at its unflagged resume
        guidance while later flagged turns resolve as single slices (§6)."""
        msgs = [
            _user("q1"),
            _assistant_text("a1"),
            _user("continue"),  # legacy unflagged resume guidance
            _assistant_text("a2"),
            _user("q2"),
            _assistant_text("working"),
            _injected("mid-turn note"),
            _nudge(),
            _assistant_text("done"),
        ]
        resolved = _resolve_turns(msgs, None)
        assert _spans(resolved) == [(0, 2), (2, 4), (4, 9)]
        assert not resolved.degraded


class TestResolveTurnsWithState:
    """State-backed resolution: history-segment clip, marker regions, numbering."""

    def test_provider_prefix_user_opens_no_span(self) -> None:
        """§3.2 rule 1: a provider prefix user message never opens a turn."""
        opener1 = _user("q1")
        answer1 = _assistant_text("a1")
        opener2 = _user("q2")
        state = {"messages": [opener1, answer1, _turn_marker(1), opener2]}
        prefix_user = _user("provider context prompt")  # context_management.py shape
        wire = [prefix_user, *_wire_view(state)]

        resolved = _resolve_turns(wire, state)

        assert _spans(resolved) == [(1, 3), (3, 4)]
        assert [s.absolute_number for s in resolved.spans] == [1, 2]

    def test_mixed_shape_degrades_per_marker_region(self) -> None:
        """opener → work → marker → flagged tail: completed turn stays previous."""
        opener1 = _user("q1")
        answer1 = _assistant_text("a1")
        guidance = _injected("try again with flag X")
        state = {"messages": [opener1, answer1, _turn_marker(1), guidance]}
        wire = _wire_view(state)

        resolved = _resolve_turns(wire, state)

        assert resolved.degraded
        assert resolved.region_start == 2
        assert _spans(resolved) == [(0, 2), (2, 3)]
        assert resolved.synthetic_opener_idx == 2
        assert [s.absolute_number for s in resolved.spans] == [1, 2]

    def test_crash_cluster_keeps_turn_current(self) -> None:
        """§3.2 rule 1: an unhealthy cluster does not close a completed turn."""
        opener1 = _user("q1")
        answer1 = _assistant_text("a1")
        opener2 = _user("q2")
        work2 = _assistant_text("work2")
        guidance = _injected("guidance after crash")
        state = {
            "messages": [
                opener1,
                answer1,
                _turn_marker(1),
                opener2,
                work2,
                _status_marker(HistoryMarkerKind.INTERRUPTED),
                _turn_marker(2),
                guidance,
            ]
        }
        wire = _wire_view(state)

        resolved = _resolve_turns(wire, state)

        # ONE current turn spanning opener2 through the guidance — the
        # boundary walk skips the crash-terminated cluster, so P1/P2 never
        # see work2 as a previous turn.  (The INTERRUPTED status message is
        # not a turn marker and stays on the wire view.)
        assert _spans(resolved) == [(0, 2), (2, 6)]
        assert not resolved.degraded
        # turn_counter would say 3; the resolver refuses to close turn 2.
        assert resolved.current.absolute_number == 2

    def test_awaiting_sub_agents_cluster_keeps_turn_current(self) -> None:
        """Health = STATUS_MARKERS, not an interrupted/error enumeration."""
        opener1 = _user("q1")
        work1 = _assistant_text("work1")
        state = {
            "messages": [
                opener1,
                work1,
                _status_marker(HistoryMarkerKind.AWAITING_SUB_AGENTS),
                _turn_marker(1),
            ]
        }
        wire = _wire_view(state)

        resolved = _resolve_turns(wire, state)

        assert _spans(resolved) == [(0, 3)]
        assert resolved.current.absolute_number == 1

    def test_all_flagged_marker_less_history_degrades(self) -> None:
        """The no-prior-user resume shape: degraded slice spans the whole wire."""
        work = _assistant_text("interrupted tool work")
        nudge = _nudge()
        # A live nudge travels as the run's input prefix — on the wire but
        # not yet in state.
        state = {"messages": [work]}
        wire = [work, nudge]

        resolved = _resolve_turns(wire, state)

        assert resolved.degraded
        assert _spans(resolved) == [(0, 2)]
        assert resolved.synthetic_opener_idx == 1
        assert resolved.current.absolute_number == 1

    def test_work_only_region_behind_completed_turns(self) -> None:
        """§3.2 rule 4: assistant/tool-only region degrades with no synthetic opener."""
        opener1 = _user("q1")
        answer1 = _assistant_text("a1")
        work2 = _assistant_text("work2")
        state = {"messages": [opener1, answer1, _turn_marker(1), work2]}
        wire = _wire_view(state)

        resolved = _resolve_turns(wire, state)

        assert resolved.degraded
        assert _spans(resolved) == [(0, 2), (2, 3)]
        assert resolved.synthetic_opener_idx is None

    def test_hidden_duplicate_id_does_not_move_region(self) -> None:
        """§3.2 rule 1: the id fallback never lands on a pre-marker EXCLUDED
        state message that reuses a visible current-turn message's id."""
        opener1 = _user("q1")
        excluded_old = _assistant_text("old excluded work")
        excluded_old.message_id = "msg_1"
        excluded_old.additional_properties[EXCLUDED_KEY] = True
        opener2 = _user("q2")
        current_work = _assistant_text("current work")
        current_work.message_id = "msg_1"  # cross-run id reuse
        state = {"messages": [opener1, excluded_old, _turn_marker(1), opener2, current_work]}
        # The current-turn work reaches the wire as a REBUILT object
        # (streaming finalizer) — identity misses, the id fallback engages.
        rebuilt_work = _assistant_text("current work")
        rebuilt_work.message_id = "msg_1"
        wire = [opener1, opener2, rebuilt_work]

        resolved = _resolve_turns(wire, state)

        # A naive id lookup would map the rebuilt work onto the excluded
        # pre-marker message (state idx 1 ≤ marker) and jump region_start
        # past live work; the guarded fallback maps it to its state twin.
        assert _spans(resolved) == [(0, 1), (1, 3)]
        assert resolved.region_start == 1
        assert resolved.current.state_boundary == 3

    def test_falsy_marker_is_excluded_from_message_id_correspondence(self) -> None:
        """A malformed marker cannot shadow a real message with the same positional id."""
        real = _assistant_text("real")
        real.message_id = "msg_1"
        marker = _assistant_text("marker")
        marker.message_id = "msg_1"
        marker.additional_properties[HistoryMarkerKind.KEY] = ""
        rebuilt = _assistant_text("real")
        rebuilt.message_id = "msg_1"

        assert _state_correspondence([rebuilt], [real, marker]) == {0: 0}

    def test_identity_broken_twin_uses_guarded_id_fallback(self) -> None:
        """Rebuilt wire objects resolve via message_id with role agreement."""
        opener1 = _user("q1")
        opener1.message_id = "msg_a"
        answer1 = _assistant_text("a1")
        answer1.message_id = "msg_b"
        opener2 = _user("q2")
        opener2.message_id = "msg_c"
        state = {"messages": [opener1, answer1, _turn_marker(1), opener2]}
        # The wire carries REBUILT copies (fresh objects, same ids).
        wire_opener1 = _user("q1")
        wire_opener1.message_id = "msg_a"
        wire_answer1 = _assistant_text("a1")
        wire_answer1.message_id = "msg_b"
        wire_opener2 = _user("q2")
        wire_opener2.message_id = "msg_c"
        wire = [wire_opener1, wire_answer1, wire_opener2]

        resolved = _resolve_turns(wire, state)

        assert _spans(resolved) == [(0, 2), (2, 3)]
        assert resolved.region_start == 2
        assert resolved.current.state_boundary == 3

    def test_wire_view_wrappers_resolve_completed_turns(self) -> None:
        """The kernel wire hands per-call message VIEWS — fresh wrapper +
        fresh contents list, shared content objects.  With no message_ids
        anywhere, correspondence must still hold via shared content-object
        identity, or every completed turn silently drops out of resolution."""
        opener1 = _user("q1")
        answer1 = _assistant_text("a1")
        opener2 = _user("q2")
        state = {"messages": [opener1, answer1, _turn_marker(1), opener2]}
        wire = [_wire_message_view(m) for m in _wire_view(state)]
        assert all(
            w is not s and w.contents is not s.contents for w, s in zip(wire, [opener1, answer1, opener2], strict=True)
        )

        resolved = _resolve_turns(wire, state)

        assert _spans(resolved) == [(0, 2), (2, 3)]
        assert resolved.region_start == 2
        assert resolved.previous[0].state_boundary == 0
        assert resolved.current.state_boundary == 3

    def test_reminder_enriched_stored_opener_resolves_its_turn(self) -> None:
        """A child continuing from its own history (retry after completed work)
        sends no new input: its stored opener is the LAST user message, which the
        reminder middleware rebuilds with fresh contents.  Only the shared
        ``additional_properties`` dict ties it to state — without that tier the
        opener falls outside the history segment and the turn resolves to no
        span, so compaction never runs."""
        opener = _user("investigate the repo")
        work = [*_build_tool_group("c0", "read_file", "x" * 200), *_build_tool_group("c1", "grep", "x" * 200)]
        state = {"messages": [opener, *work]}
        wire = [_wire_message_view(m) for m in _wire_view(state)]
        wire[0] = SystemReminderMiddleware._create_enriched(wire[0], ["runtime reminder"], [])
        assert all(a is not b for a, b in zip(wire[0].contents, opener.contents, strict=False))

        resolved = _resolve_turns(wire, state)

        assert _spans(resolved) == [(0, len(wire))]
        assert resolved.current.state_boundary == 0
        assert _state_correspondence(wire, state["messages"])[0] == 0

    def test_properties_identity_shared_by_two_state_messages_is_ambiguous(self) -> None:
        """A dict aliased by two state messages proves nothing; the tier abstains."""
        first = _user("q1")
        second = _user("q2")
        second.additional_properties = first.additional_properties
        rebuilt = Message("user", ["q2 enriched"])
        rebuilt.additional_properties = first.additional_properties

        assert _state_correspondence([rebuilt], [first, second]) == {}

    def test_properties_identity_requires_role_agreement(self) -> None:
        stored = _assistant_text("a1")
        rebuilt = Message("user", ["not the same message"])
        rebuilt.additional_properties = stored.additional_properties

        assert _state_correspondence([rebuilt], [stored]) == {}

    def test_fresh_prompt_boundary_absent_from_state(self) -> None:
        """A fresh prompt's opener is not stored yet: state_boundary is None."""
        opener1 = _user("q1")
        answer1 = _assistant_text("a1")
        state = {"messages": [opener1, answer1, _turn_marker(1)]}
        fresh_opener = _user("q2")  # in-flight input, unsaved
        wire = [*_wire_view(state), fresh_opener]

        resolved = _resolve_turns(wire, state)

        assert _spans(resolved) == [(0, 2), (2, 3)]
        assert resolved.current.state_boundary is None
        assert resolved.previous[0].state_boundary == 0

    def test_numbering_seeds_from_folded_turn_range(self) -> None:
        """A current turn behind a fully folded prefix numbers max_folded + 1."""
        summary = _assistant_text("[Compressed context: ctx_old]")
        summary.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
        opener = _user("current question")
        state = {
            "messages": [summary, opener],
            "compressed_msgs": [
                CompressedBlock(compressed_context_id="ctx_sentinel", turn_range=(0, 0)),
                CompressedBlock(compressed_context_id="ctx_old", turn_range=(1, 4)),
            ],
        }
        wire = _wire_view(state)

        resolved = _resolve_turns(wire, state)

        assert _spans(resolved) == [(1, 2)]
        assert resolved.current.absolute_number == 5


# ---------------------------------------------------------------------------
# Strategy-level integration pins (each twins a resolver unit test above)
# ---------------------------------------------------------------------------


async def test_compaction_reports_absolute_turn_numbers():
    """After compress_context removes early turns, Phase 1 must report
    absolute turn numbers, not relative enumeration indices.

    The compacted list is the marker-LESS wire view, so the numbers come
    from the resolver's state-side marker scan seeded with the folded
    ``turn_range`` (§4.1) — never from wire-embedded markers.
    """
    # Simulate: turns 1-2 folded into a compressed block, turns 3-4-5 active.
    compressed_summary = _assistant_text("[Compressed context: ctx_abc]")
    compressed_summary.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY

    state_messages: list[Message] = [compressed_summary]
    # Turn 3 (first visible)
    state_messages.append(_user("Turn 3 question"))
    state_messages.extend(_build_tool_group("t3_c0", "search", "x" * 3000))
    state_messages.extend(_build_tool_group("t3_c1", "read_file", "x" * 3000))
    state_messages.append(_assistant_text("Turn 3 answer"))
    state_messages.append(_turn_marker(3))
    # Turn 4
    state_messages.append(_user("Turn 4 question"))
    state_messages.extend(_build_tool_group("t4_c0", "grep", "x" * 3000))
    state_messages.extend(_build_tool_group("t4_c1", "read_file", "x" * 3000))
    state_messages.append(_assistant_text("Turn 4 answer"))
    state_messages.append(_turn_marker(4))
    # Turn 5 (current)
    state_messages.append(_user("Turn 5 question"))
    state_messages.extend(_build_tool_group("t5_c0", "glob", "x" * 1000))
    state_messages.append(_assistant_text("Turn 5 answer"))

    state = {
        "messages": state_messages,
        "compressed_msgs": [CompressedBlock(compressed_context_id="ctx_abc", turn_range=(1, 2))],
    }
    messages = _wire_view(state)

    total = _estimate_tokens(messages)
    events: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.90,
        target_pct=0.50,
        on_compaction=_async_appender(events),
    )
    strategy.bind_state(state)
    changed = await strategy(messages)
    assert changed

    p1_events = [e for e in events if e.phase == "phase1"]
    assert p1_events, "Phase 1 should have fired"
    # Must report absolute turn numbers 3, 4 — NOT relative 1, 2
    assert 3 in p1_events[0].turn_numbers
    assert 1 not in p1_events[0].turn_numbers, (
        f"Reported relative turn 1 instead of absolute; got {p1_events[0].turn_numbers}"
    )


async def test_p1_p2_skip_injected_current_turn_groups():
    """Behavior-change pin: a mid-turn injection no longer splits the current
    task, so its pre-injection tool groups are protected from P1/P2 mechanical
    truncation and handed to Phase 4 instead — while real previous turns are
    still compacted."""
    messages = [
        _user("Turn 1 request"),
        *_build_tool_group("t1_c0", "search", "x" * 3000),
        *_build_tool_group("t1_c1", "read_file", "x" * 3000),
        _assistant_text("Turn 1 answer"),
        _user("Turn 2 request"),
        *_build_tool_group("t2_pre", "grep", "x" * 3000),
        _assistant_text("working"),
        _injected("mid-turn constraint"),
        *_build_tool_group("t2_post", "glob", "x" * 3000),
    ]
    total = _estimate_tokens(messages)

    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.01,  # forces every phase to run
        satisfied_pct=0.01,
        force_pct=0.01,
        on_compaction=_async_appender(received),
    )
    changed = await strategy(messages)
    assert changed

    p12_numbers = {n for r in received if r.phase in ("phase1", "phase2") for n in r.turn_numbers}
    assert p12_numbers == {1}, f"P1/P2 must only touch turn 1, reported {p12_numbers}"

    # The pre-injection current-turn group survives P1/P2 and is dropped by
    # Phase 4 (current_turn_drop), never truncated as a fake previous turn.
    for call_id in ("t2_pre", "t2_post"):
        for msg in messages:
            if _has_call_id(msg, call_id):
                assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP, (
                    f"{call_id} should be Phase-4 dropped, got {msg.additional_properties.get(EXCLUDE_REASON_KEY)!r}"
                )

    injected_msg = next(m for m in messages if m.additional_properties.get(HistoryMarkerKind.INJECTED_KEY))
    assert not injected_msg.additional_properties.get(EXCLUDED_KEY, False)

    p4 = [r for r in received if r.phase == "phase4"]
    assert p4 and p4[0].turn_numbers == [2]


async def test_provider_prefix_user_never_treated_as_previous_turn():
    """§3.2 rule 1 integration: a provider-contributed prefix (the system prompt
    plus the context_management user-role prompt shape) opens no span, so P1/P2
    never compact "turns" cut from provider context and the prefix itself is
    never collected nor excluded."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    state["messages"].append(_user("Turn 1 request"))
    state["messages"].extend(_build_tool_group("t1_c0", "search", "x" * 3000))
    state["messages"].append(_assistant_text("Turn 1 answer"))
    CompressibleHistoryProvider.insert_marker(state, 1)
    state["messages"].append(_user("Turn 2 request"))
    t2_group = _build_tool_group("t2_c0", "grep", "x" * 3000)
    state["messages"].extend(t2_group)

    system_msg = Message(role="system", contents=[Content.from_text("You are chrys. " + "s " * 500)])
    prefix_user = _user("Workspace context digest " + "p " * 1500)
    wire = [system_msg, prefix_user, *_wire_view(state)]
    total = _estimate_tokens(wire)

    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.01,
        on_compaction=_async_appender(received),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    p12_numbers = {n for r in received if r.phase in ("phase1", "phase2") for n in r.turn_numbers}
    assert p12_numbers == {1}
    assert not system_msg.additional_properties.get(EXCLUDED_KEY, False)
    assert not prefix_user.additional_properties.get(EXCLUDED_KEY, False)
    for msg in t2_group:
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    p4 = [r for r in received if r.phase == "phase4"]
    assert p4 and p4[0].turn_numbers == [2]


async def test_phase4_runs_when_reminder_rebuilds_the_stored_opener():
    """Integration twin of the enriched-opener resolver pin: a child retry that
    continues from its own history still compacts its current turn."""
    state: dict = {"messages": [_user("investigate the repo and report")]}
    for i in range(6):
        state["messages"].extend(_build_tool_group(f"c{i}", "read_file", "x" * 3000))
    wire = [_wire_message_view(m) for m in _wire_view(state)]
    wire[0] = SystemReminderMiddleware._create_enriched(wire[0], ["runtime reminder"], [])

    generator = StubLastWordsGenerator()
    received: list[CompactionInfo] = []
    strategy = _forced_phase4(wire, last_words_generator=generator, on_compaction=_async_appender(received))
    strategy.bind_state(state)

    assert await strategy(wire)
    assert [r.phase for r in received if r.phase == "phase4"] == ["phase4"]
    assert len(generator.calls) == 1


async def test_p1_re_resolves_spans_after_summary_insertions():
    """§4.1 one-coherent-snapshot pin: Phase 1's summary insertions shift
    later turns' indices, so each turn iteration must re-resolve.  Turn 1
    holds three groups (three insertions); turn 2's last group starts within
    three messages of its span end — a stale pre-P1 span would exclude it."""
    messages: list[Message] = [_user("Turn 1 request")]
    for g in range(3):
        messages.extend(_build_tool_group(f"t0_c{g}", f"tool_a{g}", "x" * 3000))
    messages.append(_assistant_text("Turn 1 answer"))
    messages.append(_user("Turn 2 request"))
    messages.extend(_build_tool_group("t1_c0", "tool_b0", "x" * 3000))
    messages.extend(_build_tool_group("t1_last", "tool_b_last", "x" * 3000))
    messages.append(_assistant_text("Turn 2 answer"))
    # Current turn: text only, keeps Phase 4 out of the event stream.
    messages.append(_user("Turn 3 request"))
    messages.append(_assistant_text("Turn 3 answer"))
    total = _estimate_tokens(messages)

    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.01,
        on_compaction=_async_appender(received),
    )
    changed = await strategy(messages)
    assert changed

    p1 = [r for r in received if r.phase == "phase1"]
    assert p1
    assert p1[0].compacted_groups == 5, f"stale spans skipped a shifted group: {p1[0].tool_names}"
    assert "tool_b_last" in p1[0].tool_names
    assert p1[0].turn_numbers == [1, 2]


async def test_phase4_injections_and_nudge_survive_note_rides_last_user():
    """P4 integration: injections and the nudge survive the drop, tool work on
    BOTH sides of the injection is dropped along with inline agent text, and
    the [LAST_WORDS] reminder rides the last user message (the nudge)."""

    messages = [
        _user("start the big task"),
        *_build_tool_group("pre_c0", "search", "x" * 2000),
        _assistant_text("interim findings"),
        _injected("also check the docs"),
        *_build_tool_group("post_c0", "read_file", "x" * 2000),
        _nudge(),
    ]

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

    for call_id in ("pre_c0", "post_c0"):
        for msg in messages:
            if _has_call_id(msg, call_id):
                assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    interim = next(m for m in messages if (m.text or "") == "interim findings")
    assert interim.additional_properties.get(EXCLUDED_KEY, False)

    projected = project_included_messages(messages)
    user_msgs = [m for m in projected if m.role == "user"]
    assert len(user_msgs) == 3  # opener + injection + nudge all survive
    assert user_msgs[1].additional_properties.get(HistoryMarkerKind.INJECTED_KEY)
    assert user_msgs[2].additional_properties.get(HistoryMarkerKind.CONTINUATION_KEY)

    note_blocks = [
        c.text
        for c in user_msgs[2].contents
        if c.type == "text" and (c.text or "").startswith("<system-reminder>\n[LAST_WORDS] ")
    ]
    assert len(note_blocks) == 1 and "[stub progress note]" in note_blocks[0]
    assert not any(
        (c.text or "").startswith("<system-reminder>\n[LAST_WORDS] ")
        for m in user_msgs[:2]
        for c in m.contents
        if c.type == "text"
    )

    # The generator receives the ordered current-turn user groups.
    call = generator.calls[0]
    assert _scoped_user_texts(call) == ["start the big task", "also check the docs", "continue"]
    assert call["has_continuation_nudges"] is True
    assert call["degraded_opener"] is False


async def test_phase4_reports_last_healthy_plus_one_on_unstripped_crash_cluster():
    """§4.1 divergence pin: an unstripped crash cluster leaves turn_counter
    one high; the P4 event still reports last-healthy + 1 and P1/P2 never
    truncate the crashed turn's work (crash-cluster twin, §3.2 rule 1)."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    state["messages"].append(_user("Turn 1 request " + "x " * 1500))
    state["messages"].append(_assistant_text("Turn 1 answer " + "y " * 1500))
    CompressibleHistoryProvider.insert_marker(state, 1)
    opener2 = _user("Turn 2 request")
    state["messages"].append(opener2)
    work2 = _build_tool_group("t2_c0", "search", "z" * 4000)
    state["messages"].extend(work2)
    state["messages"].append(_status_marker(HistoryMarkerKind.INTERRUPTED))
    CompressibleHistoryProvider.insert_marker(state, 2)  # turn_counter now 2
    guidance = _injected("pick it back up with flag X")
    state["messages"].append(guidance)

    wire = _wire_view(state)
    total = _estimate_tokens(wire)

    received: list[CompactionInfo] = []
    compressed_events: list = []

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.01,
        satisfied_pct=0.01,
        force_pct=0.01,
        on_compaction=_async_appender(received),
        on_compress=_async_appender(compressed_events),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    # Turn 1 is text-only: P1/P2 fire no events, and the crashed turn's work
    # is never bucketed as a previous turn.
    assert not [r for r in received if r.phase in ("phase1", "phase2")]
    # P3 folds only the completed turn 1.
    assert [info.turn_range for info in compressed_events] == [(1, 1)]
    # The crashed turn's work is Phase-4 dropped, never folded/truncated.
    for msg in work2:
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    assert not opener2.additional_properties.get(EXCLUDED_KEY, False)
    assert not guidance.additional_properties.get(EXCLUDED_KEY, False)

    p4 = [r for r in received if r.phase == "phase4"]
    assert p4
    # turn_counter+1 would say 3; the resolver reports last-healthy + 1.
    assert p4[0].turn_numbers == [2]


async def test_phase4_degraded_all_flagged_drops_work_quotes_synthetic_opener():
    """§3.2 rule 4 integration: the marker-less all-flagged resume shape spans
    the whole wire — pre-nudge tool work IS collected by P4's drop and the
    generator's user_request comes from the synthetic opener (the nudge)."""
    work = _build_tool_group("resume_c0", "search", "x" * 3000)
    state: dict = {"messages": [*work], "compressed_msgs": [], "turn_counter": 0}
    nudge = _nudge()
    wire = [*work, nudge]
    total = _estimate_tokens(wire)

    generator = StubLastWordsGenerator(text="[note]")
    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.85,
        target_pct=0.01,
        last_words_generator=generator,
        on_compaction=_async_appender(received),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    for msg in work:
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    assert not nudge.additional_properties.get(EXCLUDED_KEY, False)
    assert generator.calls and _scoped_user_texts(generator.calls[0])[0] == DEGRADED_SCOPED_PREAMBLE
    assert generator.calls[0]["degraded_opener"] is True
    assert generator.calls[0]["has_continuation_nudges"] is True
    p4 = [r for r in received if r.phase == "phase4"]
    assert p4 and p4[0].turn_numbers == [1]


async def test_phase4_degraded_provider_prefix_untouched():
    """History-segment bound + system-kind skip (§3.2 rule 1, §4.3): in a
    degraded run with memory/context providers enabled, the provider prefix
    is never collected nor excluded, and its user-role prompt never opens a
    turn that would hand the resume work to P1/P2."""
    work = _build_tool_group("c0", "search", "x" * 3000)
    state: dict = {"messages": [*work], "compressed_msgs": [], "turn_counter": 0}
    system_msg = Message(role="system", contents=[Content.from_text("You are chrys. " + "s " * 500)])
    context_prompt = _user("Workspace context digest " + "c " * 500)
    nudge = _nudge()
    wire = [system_msg, context_prompt, *work, nudge]
    total = _estimate_tokens(wire)

    generator = StubLastWordsGenerator(text="[note]")
    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.85,
        target_pct=0.01,
        last_words_generator=generator,
        on_compaction=_async_appender(received),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    assert not system_msg.additional_properties.get(EXCLUDED_KEY, False)
    assert not context_prompt.additional_properties.get(EXCLUDED_KEY, False)
    for msg in work:
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    assert not [r for r in received if r.phase in ("phase1", "phase2")]
    assert generator.calls and _scoped_user_texts(generator.calls[0])[0] == DEGRADED_SCOPED_PREAMBLE


async def test_phase4_degraded_mixed_shape_drops_only_region_work():
    """MIXED shape (§3.2): degradation is per marker region — the completed
    turn stays a previous turn (P1/P2 truncate it), P4 drops only region work,
    and the injected guidance is quoted as the synthetic opener."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    state["messages"].append(_user("Turn 1 request"))
    t1_group = _build_tool_group("t1_c0", "search", "x" * 3000)
    state["messages"].extend(t1_group)
    state["messages"].append(_assistant_text("Turn 1 answer"))
    CompressibleHistoryProvider.insert_marker(state, 1)
    guidance = _injected("resume with flag X")
    state["messages"].append(guidance)
    t2_group = _build_tool_group("t2_c0", "grep", "y" * 3000)
    state["messages"].extend(t2_group)

    wire = _wire_view(state)
    total = _estimate_tokens(wire)

    generator = StubLastWordsGenerator(text="[note]")
    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.01,
        satisfied_pct=0.01,
        force_pct=0.01,
        last_words_generator=generator,
        on_compaction=_async_appender(received),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    p12_numbers = {n for r in received if r.phase in ("phase1", "phase2") for n in r.turn_numbers}
    assert p12_numbers == {1}
    for msg in t1_group:
        assert msg.additional_properties.get(EXCLUDED_KEY, False)
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) != _REASON_CURRENT_TURN_DROP
    for msg in t2_group:
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    assert not guidance.additional_properties.get(EXCLUDED_KEY, False)
    assert generator.calls and _scoped_user_texts(generator.calls[0]) == [
        DEGRADED_SCOPED_PREAMBLE,
        "resume with flag X",
    ]
    assert generator.calls[0]["degraded_opener"] is True
    p4 = [r for r in received if r.phase == "phase4"]
    assert p4 and p4[0].turn_numbers == [2]


async def test_phase4_leading_summary_survives_structural_skip():
    """§4.3: a degraded span can start at wire index 0 and reach a prior
    compressed-context summary — the structural-group skip keeps it."""
    summary = _assistant_text("[Compressed context: ctx_prior]")
    summary.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
    work = _build_tool_group("c0", "search", "x" * 3000)
    state: dict = {
        "messages": [summary, *work],
        "compressed_msgs": [CompressedBlock(compressed_context_id="ctx_prior", turn_range=(1, 2))],
        "turn_counter": 2,
    }
    nudge = _nudge()
    wire = [summary, *work, nudge]
    total = _estimate_tokens(wire)

    received: list[CompactionInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.85,
        target_pct=0.01,
        on_compaction=_async_appender(received),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    assert not summary.additional_properties.get(EXCLUDED_KEY, False)
    for msg in work:
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP
    # Numbering rides the folded seed: max folded turn_range[1] + 1.
    p4 = [r for r in received if r.phase == "phase4"]
    assert p4 and p4[0].turn_numbers == [3]


async def test_no_phase_fires_on_summary_plus_nudge_window():
    """A [summary, flagged-nudge] window holds no tool work: no phase fires."""
    summary = _assistant_text("[Compressed context: ctx_prior] " + "s " * 2000)
    summary.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
    state: dict = {
        "messages": [summary],
        "compressed_msgs": [CompressedBlock(compressed_context_id="ctx_prior", turn_range=(1, 2))],
        "turn_counter": 2,
    }
    nudge = _nudge()
    wire = [summary, nudge]
    total = _estimate_tokens(wire)

    received: list[CompactionInfo] = []
    precompact: list[PreCompactInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.50,
        target_pct=0.10,
        on_compaction=_async_appender(received),
        on_pre_compact=_async_appender(precompact),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)

    assert not changed
    assert not received
    assert not precompact
    assert not summary.additional_properties.get(EXCLUDED_KEY, False)
    assert not nudge.additional_properties.get(EXCLUDED_KEY, False)


async def test_no_phase_fires_without_user_messages():
    """§3.2 rule 5: a history that never held user input resolves to no spans
    — early return, no phase fires."""
    wire = [_assistant_text("boot log " + "a " * 2000), _assistant_text("more " + "b " * 2000)]
    total = _estimate_tokens(wire)

    received: list[CompactionInfo] = []
    precompact: list[PreCompactInfo] = []
    strategy = _make_strategy(
        max_context_tokens=total + 50,
        trigger_pct=0.50,
        target_pct=0.10,
        on_compaction=_async_appender(received),
        on_pre_compact=_async_appender(precompact),
    )
    changed = await strategy(wire)

    assert not changed
    assert not received
    assert not precompact
