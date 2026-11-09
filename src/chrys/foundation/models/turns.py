# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Canonical turn-boundary resolution for session message histories.

A **real turn** starts at a user message the user actually submitted as a new
prompt (the *opener*).  Two other kinds of user message appear mid-turn and
must never open a turn:

- **Injected** (``HistoryMarkerKind.INJECTED_KEY``): user-authored input that
  arrived while the agent was working — live injections and resume guidance.
  Permanent user content.
- **Continuation** (``HistoryMarkerKind.CONTINUATION_KEY``): the synthetic
  ``"continue"`` nudge the orchestration sends to resume an interrupted run.  Never
  user-authored; normally removed post-run.

Design rule: turn-boundary resolution and injected/continue classification go
through this module and the ``HistoryMarkerKind`` constants — no ad-hoc
``role == "user"`` turn splits and no raw ``"_injected"``/``"_continuation"``
literals elsewhere.  Sanctioned exceptions (documented at their sites): the
"literal last user input" anchors (reminder attach point, trailing-cleanup
scans) and the legacy ``text == "continue"`` fallback in
``remove_continuation_message``, which serves pre-flag histories.

This module is foundation tier: structural typing only, no kernel import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from chrys.foundation.models.history_markers import HistoryMarkerKind

if TYPE_CHECKING:
    from collections.abc import Sequence

UserMessageKind = Literal["opener", "injected", "continuation"]


class SupportsTurnMessage(Protocol):
    """Structural shape of a history message (kernel ``Message`` compatible)."""

    @property
    def role(self) -> str: ...

    @property
    def text(self) -> str | None: ...

    @property
    def additional_properties(self) -> dict[str, Any]: ...


def is_mid_turn_user_message(msg: SupportsTurnMessage) -> bool:
    """User message that continues the current task instead of opening a turn."""
    return msg.role == "user" and any(
        msg.additional_properties.get(key, False) for key in HistoryMarkerKind.MID_TURN_USER_KEYS
    )


def opens_turn(msg: SupportsTurnMessage) -> bool:
    """User message that opens a real turn (unflagged user input)."""
    return msg.role == "user" and not is_mid_turn_user_message(msg)


def is_injected_message(msg: SupportsTurnMessage) -> bool:
    """User-authored mid-turn message (live injection or resume guidance)."""
    return msg.role == "user" and bool(msg.additional_properties.get(HistoryMarkerKind.INJECTED_KEY, False))


def is_continuation_message(msg: SupportsTurnMessage) -> bool:
    """Synthetic ``"continue"`` nudge — never user-authored content."""
    return msg.role == "user" and bool(msg.additional_properties.get(HistoryMarkerKind.CONTINUATION_KEY, False))


def user_text_matches(msg: SupportsTurnMessage, text: str, *, kind: UserMessageKind) -> bool:
    """Kind-compatible user text match: role, text equality, AND flag compatibility.

    ``kind="opener"`` matches unflagged user messages only;
    ``"injected"``/``"continuation"`` match messages carrying their flag.
    Shared by every same-text dedup that must not confuse message kinds —
    a retry-guidance note worded identically to the turn opener is a
    *different* message and must never dedup against it (and vice versa).
    """
    if msg.role != "user" or (msg.text or "") != text:
        return False
    props = msg.additional_properties
    if kind == "opener":
        return not any(props.get(key, False) for key in HistoryMarkerKind.MID_TURN_USER_KEYS)
    if kind == "injected":
        return bool(props.get(HistoryMarkerKind.INJECTED_KEY, False))
    if kind == "continuation":
        return bool(props.get(HistoryMarkerKind.CONTINUATION_KEY, False))
    raise ValueError(f"unknown user message kind: {kind!r}")


def turn_slices(messages: Sequence[SupportsTurnMessage]) -> list[tuple[int, int]]:
    """Return ``(start, end)`` index pairs, one per real turn.

    Splits on :func:`opens_turn` only: injected and continuation user
    messages stay inside the turn they arrived in.  Messages before the
    first opener (compressed summaries, provider context, or malformed
    flagged-first histories) belong to no turn.
    """
    slices: list[tuple[int, int]] = []
    current_start: int | None = None
    for i, msg in enumerate(messages):
        if opens_turn(msg):
            if current_start is not None:
                slices.append((current_start, i))
            current_start = i
    if current_start is not None:
        slices.append((current_start, len(messages)))
    return slices


def current_turn_start(messages: Sequence[SupportsTurnMessage]) -> int:
    """Return the index after the last completed-turn marker or summary.

    A summary replaces completed turns, including their markers, so it is
    the same boundary. ``0`` when neither exists: the whole history is the
    current (first) turn region. Shared by dedup, continuation cleanup, and
    current-run metadata logic.
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].additional_properties.get(HistoryMarkerKind.KEY) in (
            HistoryMarkerKind.TURN,
            HistoryMarkerKind.SUMMARY,
        ):
            return i + 1
    return 0
