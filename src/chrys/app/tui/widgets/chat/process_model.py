# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared live/replay process boundaries, independent of Textual widgets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type EntryKind = Literal[
    "user", "injection", "compaction", "answer", "thinking", "intermediate", "tool", "failure", "retry", "other"
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


def plan_process_segments(entries: list[ProcessEntry], *, protect_last_turn: bool = False) -> list[ProcessSegment]:
    """Only persisted user boundaries number segments; notices never become members.

    A message sent mid-turn and a compaction summary each start a new segment
    of their turn: what came before them folds above them, what came after below.
    """
    openers = [index for index, entry in enumerate(entries) if entry.kind == "user"]
    result: list[ProcessSegment] = []
    for turn, opener in enumerate(openers):
        end = openers[turn + 1] if turn + 1 < len(openers) else len(entries)
        anchors = [
            opener,
            *(index for index in range(opener + 1, end) if entries[index].kind in {"injection", "compaction"}),
        ]
        terminals = [
            index
            for index in range(opener + 1, end)
            if entries[index].kind in {"answer", "failure"} or entries[index].failure_after
        ]
        final = (
            terminals[-1]
            if terminals and entries[terminals[-1]].kind == "answer" and not entries[terminals[-1]].failure_after
            else None
        )
        success = final is not None
        protected = protect_last_turn and turn == len(openers) - 1
        for segment, anchor in enumerate(anchors):
            stop = anchors[segment + 1] if segment + 1 < len(anchors) else end
            members = tuple(
                index
                for index in range(anchor + 1, stop)
                if index != final
                and entries[index].kind in {"answer", "thinking", "intermediate", "tool", "failure", "retry"}
            )
            result.append(
                ProcessSegment(
                    opener,
                    anchor,
                    segment,
                    members,
                    success and not protected,
                )
            )
    return result
