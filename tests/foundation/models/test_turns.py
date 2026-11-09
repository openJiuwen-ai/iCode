# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the canonical turn-boundary module."""

from __future__ import annotations

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.turns import (
    current_turn_start,
    is_continuation_message,
    is_injected_message,
    is_mid_turn_user_message,
    opens_turn,
    turn_slices,
    user_text_matches,
)


class _Msg:
    """Minimal structural message (no kernel import in foundation tests)."""

    def __init__(self, role: str, text: str | None = "", props: dict[str, object] | None = None) -> None:
        self.role = role
        self.text = text
        self.additional_properties: dict[str, object] = dict(props or {})


def _user(text: str = "hi", **props: object) -> _Msg:
    return _Msg("user", text, props)


def _injected(text: str = "note") -> _Msg:
    return _user(text, **{HistoryMarkerKind.INJECTED_KEY: True})


def _nudge(text: str = "continue") -> _Msg:
    return _user(text, **{HistoryMarkerKind.CONTINUATION_KEY: True})


def _assistant(text: str = "ok") -> _Msg:
    return _Msg("assistant", text)


def _turn_marker(turn: int | None = None) -> _Msg:
    props: dict[str, object] = {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN}
    if turn is not None:
        props["_turn"] = turn
    return _Msg("assistant", "", props)


def test_constants_cover_both_mid_turn_flags() -> None:
    assert HistoryMarkerKind.INJECTED_KEY == "_injected"
    assert HistoryMarkerKind.CONTINUATION_KEY == "_continuation"
    assert frozenset({"_injected", "_continuation"}) == HistoryMarkerKind.MID_TURN_USER_KEYS


@pytest.mark.parametrize(
    ("msg", "mid_turn", "injected", "continuation"),
    [
        (_user("hi"), False, False, False),
        (_user("hi", _injected=False), False, False, False),
        (_user("hi", _continuation=False), False, False, False),
        (_injected(), True, True, False),
        (_nudge(), True, False, True),
        (_Msg("assistant", "hi", {HistoryMarkerKind.INJECTED_KEY: True}), False, False, False),
        (_Msg("tool", "hi", {HistoryMarkerKind.CONTINUATION_KEY: True}), False, False, False),
    ],
)
def test_predicate_truth_table(msg: _Msg, mid_turn: bool, injected: bool, continuation: bool) -> None:
    assert is_mid_turn_user_message(msg) is mid_turn
    assert is_injected_message(msg) is injected
    assert is_continuation_message(msg) is continuation
    assert opens_turn(msg) is (msg.role == "user" and not mid_turn)


def test_turn_slices_empty_and_no_openers() -> None:
    assert turn_slices([]) == []
    assert turn_slices([_assistant(), _Msg("tool", "r")]) == []
    # All-flagged history: strict slicing yields nothing (resolver handles it).
    assert turn_slices([_nudge(), _assistant()]) == []


def test_turn_slices_flagged_before_first_opener_belongs_to_no_turn() -> None:
    messages = [_injected("early"), _assistant(), _user("real opener"), _assistant()]
    assert turn_slices(messages) == [(2, 4)]


def test_turn_slices_interleaved_injections_and_nudges_do_not_split() -> None:
    messages = [
        _user("task"),  # 0 opener, turn 1
        _assistant(),  # 1
        _injected("also check env"),  # 2
        _assistant(),  # 3
        _nudge(),  # 4
        _assistant(),  # 5
        _user("next task"),  # 6 opener, turn 2
        _injected("follow-up"),  # 7
        _assistant(),  # 8
    ]
    assert turn_slices(messages) == [(0, 6), (6, 9)]


def test_turn_slices_legacy_unflagged_continue_still_splits() -> None:
    # Pre-flag histories keep old behavior: an unflagged "continue" opens a turn.
    messages = [_user("task"), _assistant(), _user("continue"), _assistant()]
    assert turn_slices(messages) == [(0, 2), (2, 4)]


def test_current_turn_start_marker_scan_parity() -> None:
    assert current_turn_start([]) == 0
    assert current_turn_start([_user(), _assistant()]) == 0
    messages = [_user(), _assistant(), _turn_marker(), _user("second"), _assistant()]
    assert current_turn_start(messages) == 3
    # Trailing marker: region is empty (index == len).
    messages.append(_turn_marker())
    assert current_turn_start(messages) == 6


def test_user_text_matches_kind_compatibility() -> None:
    opener = _user("same text")
    injected = _injected("same text")
    nudge = _nudge("continue")

    assert user_text_matches(opener, "same text", kind="opener")
    assert not user_text_matches(opener, "same text", kind="injected")
    assert not user_text_matches(injected, "same text", kind="opener")
    assert user_text_matches(injected, "same text", kind="injected")
    assert user_text_matches(nudge, "continue", kind="continuation")
    assert not user_text_matches(nudge, "continue", kind="opener")

    assert not user_text_matches(opener, "different", kind="opener")
    assert not user_text_matches(_assistant("same text"), "same text", kind="opener")
    # None text matches the empty string only.
    assert user_text_matches(_Msg("user", None), "", kind="opener")

    with pytest.raises(ValueError, match="unknown user message kind"):
        user_text_matches(opener, "same text", kind="bogus")  # type: ignore[arg-type]


@pytest.mark.parametrize("boundary", [HistoryMarkerKind.SUMMARY, HistoryMarkerKind.TURN])
def test_current_region_starts_after_the_latest_completed_turn_or_summary(boundary: str) -> None:
    summary = _Msg("assistant", "Completed context", {HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY})
    last = _Msg("assistant", "", {HistoryMarkerKind.KEY: boundary})
    messages = [_user("old"), _turn_marker(), summary, _user("later"), last, _user("current")]
    assert current_turn_start(messages) == 5
    assert current_turn_start([summary]) == 1
