# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the awaiting-sub-agents marker in :class:`SessionHistoryManager`.

These exercise the Tier 3 marker lifecycle: insert, update in place,
remove, and interaction with the other trailing-marker helpers
(``has_trailing_error_markers``, ``remove_trailing_markers``,
``remove_all_status_markers``).
"""

from __future__ import annotations

import pytest

from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import (
    AWAITING_SUB_AGENTS_MESSAGE,
    EXECUTION_FAILED_MESSAGE,
    EXECUTION_INTERRUPTED_MESSAGE,
    SESSION_CLOSED_MESSAGE,
    SUB_AGENT_STATE_DISCARDED_MESSAGE,
    HistoryMarkerKind,
)
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY, read_analytics_item_id
from chrys.kernel import Content, Message
from chrys.service.session.history import SessionHistoryManager


def _new_history(*messages: Message) -> SessionHistoryManager:
    h = SessionHistoryManager()
    h.bind({"messages": list(messages)})
    return h


def _awaiting(msg: Message) -> bool:
    return msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.AWAITING_SUB_AGENTS


@pytest.mark.parametrize("invocation_ids", [["solo"], ["first", "second", "third"]])
def test_upsert_appends_marker_with_compatibility_literal_and_semantics(invocation_ids: list[str]) -> None:
    h = _new_history(Message("user", ["hi"]))
    h.upsert_awaiting_sub_agents_marker(invocation_ids)

    msgs = h.messages
    assert len(msgs) == 2
    assert _awaiting(msgs[-1])
    assert msgs[-1].text.encode("utf-8") == f"Awaiting {len(invocation_ids)} sub-agent(s)".encode()
    assert msgs[-1].additional_properties[HistoryMarkerKind.STATUS_CODE_KEY] == (
        HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS
    )
    assert msgs[-1].additional_properties["_invocation_ids"] == invocation_ids


def test_upsert_self_heals_code_and_preserves_stale_compatibility_literal() -> None:
    marker = Message("assistant", ["Awaiting 1 sub-agent(s)"])
    marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.AWAITING_SUB_AGENTS
    marker.additional_properties["_invocation_ids"] = ["old"]
    h = _new_history(Message("user", ["hi"]), marker)

    h.upsert_awaiting_sub_agents_marker(["first", "second", "third"])

    msgs = h.messages
    awaiting_msgs = [m for m in msgs if _awaiting(m)]
    assert len(awaiting_msgs) == 1
    assert awaiting_msgs[0] is marker
    assert marker.text.encode("utf-8") == b"Awaiting 1 sub-agent(s)"
    assert marker.additional_properties["_invocation_ids"] == ["first", "second", "third"]
    assert marker.additional_properties[HistoryMarkerKind.STATUS_CODE_KEY] == (
        HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS
    )


def test_upsert_with_empty_ids_removes_marker() -> None:
    h = _new_history(Message("user", ["hi"]))
    h.upsert_awaiting_sub_agents_marker(["a"])
    h.upsert_awaiting_sub_agents_marker([])

    assert not any(_awaiting(m) for m in h.messages)


def test_remove_awaiting_strips_all_markers() -> None:
    """Even defensive duplicates get cleaned up."""
    h = _new_history(Message("user", ["hi"]))
    # Append two markers (shouldn't happen in practice but tests robustness).
    for ids in (["a"], ["b"]):
        marker = Message("assistant", ["Awaiting"])
        marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.AWAITING_SUB_AGENTS
        marker.additional_properties["_invocation_ids"] = ids
        h.messages.append(marker)

    h.remove_awaiting_sub_agents_marker()
    assert not any(_awaiting(m) for m in h.messages)


def test_has_trailing_error_markers_recognises_awaiting() -> None:
    h = _new_history(Message("user", ["hi"]))
    h.upsert_awaiting_sub_agents_marker(["a"])
    assert h.has_trailing_error_markers() is True


def test_has_trailing_error_markers_skips_turn_markers_to_find_awaiting() -> None:
    """Turn markers between awaiting and end shouldn't hide the awaiting marker."""
    h = _new_history(Message("user", ["hi"]))
    h.upsert_awaiting_sub_agents_marker(["a"])

    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    h.messages.append(turn)

    assert h.has_trailing_error_markers() is True


def test_trailing_status_marker_skips_turn_markers_and_returns_source() -> None:
    interrupted = Message("assistant", ["Execution interrupted"])
    interrupted.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    interrupted.additional_properties["_interrupted_by"] = "error"
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN

    h = _new_history(Message("user", ["hi"]), interrupted, turn)

    assert h.trailing_status_marker() == (HistoryMarkerKind.INTERRUPTED, "error")
    assert h.has_trailing_error_markers() is True


def test_trailing_status_marker_with_only_turn_markers_is_none() -> None:
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN

    h = _new_history(Message("user", ["hi"]), turn)

    assert h.trailing_status_marker() is None


def test_insert_interrupted_marker_precedes_trailing_turn_marker() -> None:
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    turn.additional_properties["_turn"] = 1
    h = _new_history(Message("user", ["hi"]), turn)

    h.insert_interrupted_marker(source="error")

    kinds = [message.additional_properties.get(HistoryMarkerKind.KEY) for message in h.messages]
    assert kinds == [None, HistoryMarkerKind.INTERRUPTED, HistoryMarkerKind.TURN]
    marker = h.messages[1]
    assert marker.text.encode("utf-8") == b"Execution interrupted"
    assert marker.additional_properties[HistoryMarkerKind.STATUS_CODE_KEY] == (
        HistoryMarkerKind.STATUS_EXECUTION_INTERRUPTED
    )
    assert h.trailing_status_marker() == (HistoryMarkerKind.INTERRUPTED, "error")


def test_explicit_diagnostic_interruption_remains_unstamped() -> None:
    h = _new_history(Message("user", ["hi"]))

    h.insert_interrupted_marker(reason="provider detail", source="error")

    marker = h.messages[-1]
    assert marker.text.encode("utf-8") == b"provider detail"
    assert HistoryMarkerKind.STATUS_CODE_KEY not in marker.additional_properties


@pytest.mark.parametrize(
    ("definition", "legacy_literal"),
    [
        (EXECUTION_INTERRUPTED_MESSAGE, b"Execution interrupted"),
        (EXECUTION_FAILED_MESSAGE, b"Execution failed"),
        (SESSION_CLOSED_MESSAGE, b"Session closed before the turn finished"),
        (SUB_AGENT_STATE_DISCARDED_MESSAGE, b"Sub-agent state was discarded on reload"),
    ],
)
def test_fixed_status_definitions_preserve_legacy_literal_bytes(definition, legacy_literal: bytes) -> None:
    assert format_message(definition.bind()).encode("utf-8") == legacy_literal


@pytest.mark.parametrize("count", [1, 2])
def test_awaiting_definition_preserves_legacy_literal_bytes(count: int) -> None:
    assert format_message(AWAITING_SUB_AGENTS_MESSAGE.bind(count=count)).encode("utf-8") == (
        f"Awaiting {count} sub-agent(s)".encode()
    )


def test_remove_trailing_markers_strips_awaiting_and_turn() -> None:
    h = _new_history(Message("user", ["hi"]))
    h.upsert_awaiting_sub_agents_marker(["a"])
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    h.messages.append(turn)

    h.remove_trailing_markers()
    assert [m.role for m in h.messages] == ["user"]


def test_remove_trailing_markers_decrements_turn_counter() -> None:
    """Popping a trailing ``turn`` marker must roll ``turn_counter`` back
    by one.  Regression for the retry-flow desync where a failed turn's
    marker was stripped here but the counter stayed advanced, causing
    the next :meth:`insert_turn_marker` to skip a slot and leave
    ``turn_counter`` ``N + 1`` for an ``N``-turn conversation (visible
    in the rollback modal as "Turn 3 (Current)" for a 2-turn session
    after restore).
    """
    h = SessionHistoryManager()
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    turn.additional_properties["_turn"] = 1
    interrupted = Message("assistant", ["Execution interrupted"])
    interrupted.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    h.bind(
        {
            "messages": [Message("user", ["hi"]), interrupted, turn],
            "turn_counter": 1,
        }
    )

    h.remove_trailing_markers()
    assert [m.role for m in h.messages] == ["user"]
    # Counter rolled back to 0 so the next insert_turn_marker re-uses slot 1.
    assert h.state["turn_counter"] == 0


def test_remove_trailing_markers_decrements_per_turn_marker() -> None:
    """Multiple consecutive trailing turn markers each decrement the counter.

    Defensive — shouldn't happen in practice but guards the per-iteration
    bookkeeping.
    """
    h = SessionHistoryManager()

    def _turn(idx: int) -> Message:
        m = Message("assistant", [""])
        m.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
        m.additional_properties["_turn"] = idx
        return m

    h.bind(
        {
            "messages": [Message("user", ["hi"]), _turn(1), _turn(2), _turn(3)],
            "turn_counter": 3,
        }
    )
    h.remove_trailing_markers()
    assert [m.role for m in h.messages] == ["user"]
    assert h.state["turn_counter"] == 0


def test_remove_trailing_markers_floors_turn_counter_at_zero() -> None:
    """Decrementing never dips below 0, even if the counter was already stale."""
    h = SessionHistoryManager()
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    h.bind(
        {
            "messages": [Message("user", ["hi"]), turn],
            "turn_counter": 0,  # already 0 — shouldn't go negative
        }
    )
    h.remove_trailing_markers()
    assert h.state["turn_counter"] == 0


def test_ensure_user_message_appends_when_text_matches_earlier_turn() -> None:
    """Regression: ``ensure_user_message`` must not dedup against a
    same-text user message from an earlier turn.

    Real-world repro:

    1. During turn 1 the user typed "what time is it?" while the agent
       was still streaming → routed through ``_turn_bindings.inject()`` →
       persisted as a ``_injected: true`` user message anchored into
       turn 1.
    2. Later (after a rollback to turn 2) the user typed the same text
       again as a fresh turn-3 user message and interrupted ~1 s
       later. CompressibleHistoryProvider hadn't persisted the new user message yet,
       so ``run_and_save`` fell back to
       :meth:`SessionHistoryManager.ensure_user_message`.
    3. The old global text scan found the earlier injection, decided
       "already there", and skipped the append.  ``_post_run`` then
       wrote the ``interrupted`` + ``_turn: 3`` markers — history on
       disk had the markers but **no user message for turn 3**.

    Fix: scope the dedup to the current turn region (messages after
    the last turn marker).  An earlier turn / injection sharing the
    same text must not block the append.
    """
    h = SessionHistoryManager()
    # Turn 1: normal user msg + injection with the SAME text the user
    # will later type as a fresh turn → + assistant response + turn marker.
    u1 = Message("user", ["who are you?"])
    injected = Message("user", ["what time is it?"])
    injected.additional_properties["_injected"] = True
    assistant = Message("assistant", ["I am Chrys"])
    turn1 = Message("assistant", [""])
    turn1.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    turn1.additional_properties["_turn"] = 1
    h.bind({"messages": [u1, injected, assistant, turn1], "turn_counter": 1})

    # New turn begins; framework doesn't persist the user message before
    # the interrupt → ``ensure_user_message`` runs as a fallback.
    h.ensure_user_message("what time is it?")

    messages = h.messages
    # Appended at the end — as the fresh turn's user message.
    assert len(messages) == 5
    assert messages[-1].role == "user"
    assert messages[-1].text == "what time is it?"
    assert not messages[-1].additional_properties.get("_injected")
    # Earlier injection is untouched at its original position.
    assert messages[1].additional_properties.get("_injected") is True


def test_ensure_user_message_dedups_within_current_turn() -> None:
    """Normal path: framework already persisted the user message for
    this turn → fallback must no-op.  Scope is "after last turn marker".
    """
    h = SessionHistoryManager()
    old_user = Message("user", ["old"])
    prev_turn = Message("assistant", [""])
    prev_turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    prev_turn.additional_properties["_turn"] = 1
    current_user = Message("user", ["hi"])
    h.bind({"messages": [old_user, prev_turn, current_user], "turn_counter": 1})

    h.ensure_user_message("hi")

    messages = h.messages
    assert len(messages) == 3, "should not double-append when current turn already has the message"
    assert [m.text for m in messages if m.role == "user"] == ["old", "hi"]


def test_ensure_user_message_keeps_guidance_sent_twice_as_two_items() -> None:
    """Each retry announced its own item for its note, and frontends remove notes by that item."""
    first, second = "a" * 32, "b" * 32
    h = _new_history(Message("user", ["original"]), Message("assistant", ["partial answer"]))

    h.ensure_user_message("same note", kind="injected", item_id=first)
    h.ensure_user_message("same note", kind="injected", item_id=second)
    h.ensure_user_message("same note", kind="injected", item_id=second)

    notes = [m for m in h.messages if m.text == "same note"]
    assert [read_analytics_item_id(m.additional_properties) for m in notes] == [first, second]


def test_ensure_user_message_names_an_unstamped_match_with_the_given_item() -> None:
    h = _new_history(Message("user", ["original"]))

    h.ensure_user_message("original", item_id="a" * 32)

    assert len(h.messages) == 1
    assert read_analytics_item_id(h.messages[0].additional_properties) == "a" * 32


def test_ensure_user_message_finds_the_named_item_before_an_unstamped_same_text_message() -> None:
    """An unstamped look-alike earlier in the turn is not this input: it must not take the input's item."""
    item = "a" * 32
    legacy = Message("user", ["same note"], additional_properties={HistoryMarkerKind.INJECTED_KEY: True})
    stored = Message(
        "user", ["same note"], additional_properties={HistoryMarkerKind.INJECTED_KEY: True, ANALYTICS_ITEM_ID_KEY: item}
    )
    h = _new_history(Message("user", ["original"]), legacy, stored)

    h.ensure_user_message("same note", kind="injected", item_id=item)

    assert len(h.messages) == 3 and h.messages[1] is legacy and h.messages[2] is stored
    assert read_analytics_item_id(legacy.additional_properties) is None


def test_ensure_user_message_preserves_multimodal_contents_on_append() -> None:
    h = _new_history()
    contents = ["describe @shot.png", Content.from_data(data=b"abc", media_type="image/png")]

    h.ensure_user_message("describe @shot.png", contents=contents)

    assert len(h.messages) == 1
    assert h.messages[0].text == "describe @shot.png"
    assert any(content.media_type == "image/png" for content in h.messages[0].contents)


def test_ensure_user_message_restores_missing_multimodal_contents_on_dedup() -> None:
    h = _new_history(Message("user", ["describe @shot.png"]))
    contents = ["describe @shot.png", Content.from_data(data=b"abc", media_type="image/png")]

    h.ensure_user_message("describe @shot.png", contents=contents)

    assert len(h.messages) == 1
    assert h.messages[0].text == "describe @shot.png"
    assert any(content.media_type == "image/png" for content in h.messages[0].contents)


def test_ensure_user_message_with_no_turn_markers_scans_whole_history() -> None:
    """First turn (no turn markers yet): the whole history is the
    current turn, so a matching user message anywhere dedups.
    """
    h = SessionHistoryManager()
    h.bind({"messages": [Message("user", ["hi"])]})

    h.ensure_user_message("hi")

    assert [m.text for m in h.messages] == ["hi"]


def test_ensure_user_message_appends_when_current_turn_empty() -> None:
    """After a turn marker with no user message yet, a call appends."""
    h = SessionHistoryManager()
    u1 = Message("user", ["old"])
    turn1 = Message("assistant", [""])
    turn1.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    turn1.additional_properties["_turn"] = 1
    h.bind({"messages": [u1, turn1], "turn_counter": 1})

    h.ensure_user_message("new")

    assert [m.text for m in h.messages if m.role == "user"] == ["old", "new"]


def test_remove_continuation_message_scopes_to_current_turn() -> None:
    """Regression: ``remove_continuation_message`` must not pop an
    earlier turn's user prompt that happens to read literally
    ``"continue"``.

    Scenario (same shape as the ``ensure_user_message`` regression):

    1. Turn 1: user asks a question, assistant gives a partial response,
       user replies literally ``"continue"`` to nudge, assistant finishes
       → ``_turn=1`` marker.  The ``"continue"`` nudge sits *after* the
       assistant's partial response, so ``seen_work`` is already True
       when the forward scan reaches it — the trip-wire for the bug.
    2. Turn 2: user asks something → tool work happens → interrupted.
    3. ``UserRetry`` → :meth:`TurnResumePolicy.retry_request` injects
       ``Message("user", ["continue"])`` → run completes → engine
       calls :meth:`SessionHistoryManager.remove_continuation_message`.

    The old forward scan walked from index 0, flipped ``seen_work``
    at turn 1's first assistant message, and popped turn 1's
    ``"continue"`` nudge (wrong turn!).  Turn 2's injected
    ``"continue"`` survived in history forever, and turn 1 lost its
    nudge message.

    Fix: scope the scan to the current turn region — messages after
    the last turn marker.  Earlier turns' prompts are
    invisible to this helper.
    """
    h = SessionHistoryManager()
    # Turn 1: "continue" appears AFTER some assistant work so it would
    # trip ``seen_work`` under the old global scan.
    u1 = Message("user", ["explain X"])
    a1_partial = Message("assistant", ["partial..."])
    u1_nudge = Message("user", ["continue"])  # literal user nudge
    a1_final = Message("assistant", ["...complete"])
    turn1 = Message("assistant", [""])
    turn1.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    turn1.additional_properties["_turn"] = 1
    # Turn 2: user asks a real question, assistant starts work, then
    # the retry flow injects a ``continue`` user message.
    u2 = Message("user", ["do x"])
    a2 = Message("assistant", ["working..."])
    injected_continue = Message("user", ["continue"])
    h.bind(
        {
            "messages": [u1, a1_partial, u1_nudge, a1_final, turn1, u2, a2, injected_continue],
            "turn_counter": 1,
        }
    )

    h.remove_continuation_message()

    # Only the turn-2 injected continuation is popped; turn 1's literal
    # ``continue`` nudge stays put.
    texts = [(m.role, m.text) for m in h.messages]
    assert texts == [
        ("user", "explain X"),
        ("assistant", "partial..."),
        ("user", "continue"),  # turn 1 nudge — preserved
        ("assistant", "...complete"),
        ("assistant", ""),  # turn marker
        ("user", "do x"),
        ("assistant", "working..."),
    ], f"Wrong message popped: {texts}"


def test_remove_continuation_message_pops_in_current_turn_only() -> None:
    """Normal case: the injected ``continue`` sits after turn-1 work
    in the live current-turn region → popped correctly.
    """
    h = SessionHistoryManager()
    u1 = Message("user", ["do x"])
    a1 = Message("assistant", ["partial"])
    injected_continue = Message("user", ["continue"])
    h.bind({"messages": [u1, a1, injected_continue]})

    h.remove_continuation_message()

    texts = [(m.role, m.text) for m in h.messages]
    assert texts == [("user", "do x"), ("assistant", "partial")]


def test_remove_continuation_message_noop_without_work() -> None:
    """No assistant/tool messages yet → nothing to pop, even if a
    ``continue`` user message exists.

    Guards the ``seen_work`` precondition: bare user messages at the
    start of a turn should never be mistaken for injected
    continuations.
    """
    h = SessionHistoryManager()
    u1 = Message("user", ["continue"])
    h.bind({"messages": [u1]})

    h.remove_continuation_message()

    assert [m.text for m in h.messages] == ["continue"]


class TestRemoveContinuationMessage:
    """Test SessionHistoryManager.remove_continuation_message in isolation."""

    def _setup_history(self, messages: list[Message]) -> SessionHistoryManager:
        """Create a SessionHistoryManager bound to a state with given messages."""
        history = SessionHistoryManager()
        history.bind({"messages": messages})
        return history

    def test_removes_continue_after_work(self):
        """'continue' user message after assistant/tool messages is removed."""
        msgs = [
            Message("user", ["original prompt"]),
            Message("assistant", ["tool calls"]),
            Message("tool", ["results"]),
            Message("user", ["continue"]),
            Message("assistant", ["continuation response"]),
        ]
        history = self._setup_history(msgs)
        history.remove_continuation_message()

        assert len(msgs) == 4
        roles = [m.role for m in msgs]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert msgs[0].text == "original prompt"

    def test_does_not_remove_first_user_message(self):
        """'continue' as the first user message is not removed (no preceding work)."""
        msgs = [
            Message("user", ["continue"]),
            Message("assistant", ["response"]),
        ]
        history = self._setup_history(msgs)
        history.remove_continuation_message()

        assert len(msgs) == 2  # unchanged

    def test_does_not_remove_non_continue_message(self):
        """User messages that aren't 'continue' are not removed."""
        msgs = [
            Message("user", ["original"]),
            Message("assistant", ["tools"]),
            Message("tool", ["results"]),
            Message("user", ["something else"]),
        ]
        history = self._setup_history(msgs)
        history.remove_continuation_message()

        assert len(msgs) == 4  # unchanged

    def test_removes_only_first_continue(self):
        """Only the first 'continue' after work is removed."""
        msgs = [
            Message("user", ["original"]),
            Message("assistant", ["batch 1"]),
            Message("tool", ["result 1"]),
            Message("user", ["continue"]),  # ← this one removed
            Message("assistant", ["batch 2"]),
            Message("tool", ["result 2"]),
            Message("user", ["continue"]),  # ← this one stays
        ]
        history = self._setup_history(msgs)
        history.remove_continuation_message()

        assert len(msgs) == 6
        user_msgs = [m for m in msgs if m.role == "user"]
        assert len(user_msgs) == 2
        assert user_msgs[0].text == "original"
        assert user_msgs[1].text == "continue"

    def test_no_messages_no_crash(self):
        """Empty message list doesn't crash."""
        msgs: list[Message] = []
        history = self._setup_history(msgs)
        history.remove_continuation_message()
        assert msgs == []

    def test_no_executor_no_crash(self):
        """Unbound history manager doesn't crash."""
        history = SessionHistoryManager()
        history.remove_continuation_message()  # no-op when unbound


def _turn_marker(turn: int) -> Message:
    m = Message("assistant", [""])
    m.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    m.additional_properties["_turn"] = turn
    return m


def _nudge(text: str = "continue") -> Message:
    m = Message("user", [text])
    m.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    return m


def _injected_user(text: str) -> Message:
    m = Message("user", [text])
    m.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    return m


def test_remove_continuation_flagged_nudge_removed_without_preceding_work() -> None:
    """Pass 1 is flag-authoritative (§2.5): the no-prior-user-message
    synthesis leaves a flagged nudge with NOTHING before it in its region —
    still removed, no ``seen_work`` precondition."""
    h = _new_history(Message("assistant", ["prior turn work"]), _turn_marker(1), _nudge())

    h.remove_continuation_message()

    assert [(m.role, m.text) for m in h.messages] == [
        ("assistant", "prior turn work"),
        ("assistant", ""),  # turn marker
    ]


def test_remove_continuation_removes_all_flagged_nudges_in_region() -> None:
    """A crash-leftover older nudge (persisted, cleanup never ran) and the
    current nudge are BOTH removed in one call."""
    h = _new_history(
        Message("user", ["do x"]),
        Message("assistant", ["work 1"]),
        _nudge(),  # crash-leftover from a pass that died before cleanup
        Message("assistant", ["work 2"]),
        _nudge(),  # current pass's nudge
    )

    h.remove_continuation_message()

    assert [m.text for m in h.messages] == ["do x", "work 1", "work 2"]


def test_remove_continuation_keeps_injected_literal_continue_with_flagged_nudges() -> None:
    """A user-injected literal ``continue`` (``_injected``) is NEVER removed:
    pass 1 removes only ``_continuation``-flagged messages and pass 2 skips
    injected ones."""
    h = _new_history(
        Message("user", ["do x"]),
        Message("assistant", ["work"]),
        _injected_user("continue"),  # the user really typed this mid-turn
        Message("assistant", ["more work"]),
        _nudge(),
    )

    h.remove_continuation_message()

    users = [m for m in h.messages if m.role == "user"]
    assert [m.text for m in users] == ["do x", "continue"]
    assert users[1].additional_properties.get(HistoryMarkerKind.INJECTED_KEY) is True


def test_remove_continuation_pass2_pops_legacy_not_injected() -> None:
    """With no flagged nudge present, the legacy text-only fallback still
    skips an ``_injected`` literal ``continue`` and pops the unflagged
    leftover instead."""
    h = _new_history(
        Message("user", ["do x"]),
        Message("assistant", ["work"]),
        _injected_user("continue"),
        Message("assistant", ["more work"]),
        Message("user", ["continue"]),  # pre-flag legacy leftover
    )

    h.remove_continuation_message()

    users = [m for m in h.messages if m.role == "user"]
    assert [m.text for m in users] == ["do x", "continue"]
    assert users[1].additional_properties.get(HistoryMarkerKind.INJECTED_KEY) is True


def test_remove_continuation_pass2_alone_never_pops_injected() -> None:
    """An injected literal ``continue`` with nothing else in the region
    survives — pass 2 must not treat it as a synthetic nudge."""
    h = _new_history(
        Message("user", ["do x"]),
        Message("assistant", ["work"]),
        _injected_user("continue"),
    )

    h.remove_continuation_message()

    assert [(m.role, m.text) for m in h.messages] == [
        ("user", "do x"),
        ("assistant", "work"),
        ("user", "continue"),
    ]


def test_remove_continuation_mixed_region_removes_legacy_and_flagged() -> None:
    """MIXED region (§2.5): a pre-upgrade unflagged leftover plus a new
    flagged nudge — BOTH removed because pass 2 runs unconditionally after
    pass 1."""
    h = _new_history(
        Message("user", ["do x"]),
        Message("assistant", ["pre-upgrade work"]),
        Message("user", ["continue"]),  # legacy unflagged leftover
        Message("assistant", ["post-upgrade work"]),
        _nudge(),  # new flagged nudge
    )

    h.remove_continuation_message()

    assert [m.text for m in h.messages] == ["do x", "pre-upgrade work", "post-upgrade work"]


def test_remove_continuation_opener_named_continue_survives_pass2() -> None:
    """An opener whose text is literally ``continue`` is untouchable:
    ``seen_work`` requires work BEFORE the match inside the region."""
    h = _new_history(
        Message("assistant", ["prior turn work"]),
        _turn_marker(1),
        Message("user", ["continue"]),  # real opener typed by the user
        Message("assistant", ["work"]),
    )

    h.remove_continuation_message()

    assert [(m.role, m.text) for m in h.messages] == [
        ("assistant", "prior turn work"),
        ("assistant", ""),  # turn marker
        ("user", "continue"),
        ("assistant", "work"),
    ]


def test_remove_continuation_no_work_legacy_leftover_survives_as_opener() -> None:
    """A NO-work legacy leftover survives as an opener — the accepted
    degradation (§6) for pre-flag histories."""
    h = _new_history(
        Message("assistant", ["prior turn work"]),
        _turn_marker(1),
        Message("user", ["continue"]),  # unflagged leftover, nothing after
    )

    h.remove_continuation_message()

    assert [(m.role, m.text) for m in h.messages] == [
        ("assistant", "prior turn work"),
        ("assistant", ""),  # turn marker
        ("user", "continue"),
    ]


def test_remove_awaiting_is_idempotent_without_marker() -> None:
    """Calling remove on a history that never had an awaiting marker is a no-op.

    This is the "stale-marker-but-no-records, or records-but-no-marker"
    session-restore path where the engine unconditionally calls
    :meth:`remove_awaiting_sub_agents_marker`.
    """
    h = _new_history(Message("user", ["hi"]))
    before = list(h.messages)
    h.remove_awaiting_sub_agents_marker()
    assert h.messages == before


def test_remove_awaiting_with_unbound_manager_is_noop() -> None:
    """Defensive: manager before ``bind()`` shouldn't raise."""
    h = SessionHistoryManager()
    # No exception expected.
    h.remove_awaiting_sub_agents_marker()


def test_has_trailing_error_markers_with_only_turn_markers_is_false() -> None:
    """A clean history tail (only structural turn markers) is NOT errored."""
    h = _new_history(Message("user", ["hi"]))
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    h.messages.append(turn)
    assert h.has_trailing_error_markers() is False


def test_remove_all_status_markers_strips_awaiting() -> None:
    """Terminal-state cleanup should clear awaiting markers too."""
    interrupted = Message("assistant", ["boom"])
    interrupted.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED

    h = _new_history(Message("user", ["hi"]), interrupted)
    h.upsert_awaiting_sub_agents_marker(["a"])

    h.remove_all_status_markers()
    kinds = [m.additional_properties.get(HistoryMarkerKind.KEY) for m in h.messages]
    assert HistoryMarkerKind.INTERRUPTED not in kinds
    assert HistoryMarkerKind.AWAITING_SUB_AGENTS not in kinds


def test_tag_last_user_message_skips_flagged_legacy_nudge() -> None:
    """The tag must land on the message that was actually on the wire.

    A crash-persisted flagged nudge is filtered off the wire by the
    provider and hidden by replay before metadata is read — a profile
    switch tagged onto it would silently lose the profile boundary.
    """
    h = SessionHistoryManager()
    opener = Message("user", ["original request"])
    work = Message("assistant", ["completed work"])
    leftover = _nudge()
    h.bind({"messages": [opener, work, leftover]})

    h.tag_last_user_message(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY, "explore")

    assert opener.additional_properties[HistoryMarkerKind.PROFILE_SWITCH_TO_KEY] == "explore"
    assert HistoryMarkerKind.PROFILE_SWITCH_TO_KEY not in leftover.additional_properties
    assert leftover in h.messages


def test_tag_last_user_message_still_targets_last_injected_user() -> None:
    """Mid-turn injections stay the literal-last tag target (they ride the wire)."""
    h = SessionHistoryManager()
    opener = Message("user", ["original request"])
    injected = _injected_user("mid-turn note")
    h.bind({"messages": [opener, Message("assistant", ["partial"]), injected]})

    h.tag_last_user_message(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY, "docs")

    assert injected.additional_properties[HistoryMarkerKind.PROFILE_SWITCH_TO_KEY] == "docs"
    assert HistoryMarkerKind.PROFILE_SWITCH_TO_KEY not in opener.additional_properties
