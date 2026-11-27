# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared live/replay process boundaries, independent of Textual widgets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type EntryKind = Literal[
    "user",
    "injection",
    "compaction",
    "answer",
    "thinking",
    "intermediate",
    "completion",
    "tool",
    "failure",
    "retry",
    "other",
]


@dataclass(frozen=True)
class ProcessEntry:
    kind: EntryKind
    failure_after: bool = False
    """An omitted replay status follows this entry, without becoming process content."""


@dataclass(frozen=True)
class ProcessSegment:
    opener: int
    anchor: int
    index: int
    members: tuple[int, ...]
    fold: bool
    answer: int | None
    answer_after: int | None


_MEMBERS: frozenset[EntryKind] = frozenset(
    {"answer", "thinking", "intermediate", "completion", "tool", "failure", "retry"}
)
# What a turn produced; thinking after its answer leaves that answer the answer.
_OUTPUT: frozenset[EntryKind] = frozenset({"answer", "intermediate", "completion", "tool"})


def plan_process_segments(entries: list[ProcessEntry], *, protect_last_turn: bool = False) -> list[ProcessSegment]:
    """Only persisted user boundaries number segments; the notice that ends a turn never becomes a member.

    A message sent mid-turn and a compaction summary each start a new segment
    of their turn: what came before them folds above them, what came after below.

    A turn folds once it has ended: with its answer, with the failure or
    interruption that stopped it, or with a later turn starting after it (a new
    prompt may drop a stopped turn's notice from history). Its last answer stays
    outside the process even when only that ending notice follows it, and the
    notice stays outside too, so a stopped turn keeps the shape it had while
    running. A turn that completed with structured output keeps its steps open,
    even when a notice follows it (a rejected next prompt shows below it).
    """
    openers = [index for index, entry in enumerate(entries) if entry.kind == "user"]
    result: list[ProcessSegment] = []
    for turn, opener in enumerate(openers):
        end = openers[turn + 1] if turn + 1 < len(openers) else len(entries)
        anchors = [
            opener,
            *(index for index in range(opener + 1, end) if entries[index].kind in {"injection", "compaction"}),
        ]
        last = next(
            (index for index in reversed(range(opener + 1, end)) if entries[index].kind in _OUTPUT),
            None,
        )
        tail = range(opener + 1 if last is None else last, end)
        ending = {index for index in tail if entries[index].kind == "failure"}
        stopped = bool(ending) or any(entries[index].failure_after for index in tail)
        final = last if last is not None and entries[last].kind == "answer" else None
        completed = last is not None and entries[last].kind == "completion"
        ended = not completed and (final is not None or stopped or turn + 1 < len(openers))
        protected = protect_last_turn and turn == len(openers) - 1
        # Late injections belong before the reply, within the same stop or compaction boundary.
        answer_stop = next(
            (
                index
                for index in range(final + 1 if final is not None else end, end)
                if index in ending or entries[index - 1].failure_after or entries[index].kind == "compaction"
            ),
            end,
        )
        for segment, anchor in enumerate(anchors):
            stop = anchors[segment + 1] if segment + 1 < len(anchors) else end
            members = tuple(
                index
                for index in range(anchor + 1, stop)
                if index != final and index not in ending and entries[index].kind in _MEMBERS
            )
            answer = final if anchor < answer_stop <= stop else None
            answer_after = (
                next((member for member in reversed(members) if member < answer_stop), anchor)
                if answer is not None
                else None
            )
            result.append(
                ProcessSegment(
                    opener,
                    anchor,
                    segment,
                    members,
                    ended and not protected,
                    answer,
                    answer_after,
                )
            )
    return result
