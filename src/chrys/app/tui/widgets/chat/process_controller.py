# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply one process plan to live and restored transcripts without copying widget state."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol
from weakref import WeakKeyDictionary, WeakSet, ref

from textual.widget import Widget

from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.fold_rows import ToolTally
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    RetryMessage,
    ThinkingMessage,
    UserMessage,
    folds_with_turn,
)
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.process_model import ProcessEntry, plan_process_segments
from chrys.app.tui.widgets.chat.process_visibility import ProcessMember
from chrys.app.tui.widgets.chat.tool_call import ToolGroup

if TYPE_CHECKING:
    from collections.abc import Iterable


class ProcessHost(Protocol):
    def transcript_content_children(self) -> list[Widget]: ...

    async def mount_transcript_after(self, anchor: Widget, widget: Widget) -> bool: ...


def process_entry(widget: Widget) -> ProcessEntry:
    """Explicit membership whitelist; all notices retain their own visibility."""
    if isinstance(widget, UserMessage):
        return ProcessEntry("injection" if widget.is_injection else "user")
    if isinstance(widget, ThinkingMessage):
        return ProcessEntry("thinking")
    if isinstance(widget, AgentMessage):
        if widget.is_status:
            return ProcessEntry("failure")
        return ProcessEntry("answer" if widget.is_process_final else "intermediate")
    if isinstance(widget, ToolGroup):
        return ProcessEntry("tool")
    if isinstance(widget, (ErrorMessage, InterruptedMessage)):
        return ProcessEntry("failure")
    if isinstance(widget, RetryMessage):
        return ProcessEntry("retry")
    if isinstance(widget, (CompactionCard, ContextFoldWidget)):
        return ProcessEntry("compaction")
    return ProcessEntry("other")


def process_tally(members: Iterable[Widget]) -> ToolTally:
    """What the calls of *members*' tool groups were, as their process header counts them."""
    return sum((member.tool_tally for member in members if isinstance(member, ToolGroup)), ToolTally())


class ProcessController:
    """Own completed headers and event-driven retries of blocked folds."""

    def __init__(self, host: ProcessHost) -> None:
        self._host = host
        self._completed: WeakSet[Widget] = WeakSet()
        self._blocks: WeakSet[ProcessBlock] = WeakSet()
        self._groups: WeakKeyDictionary[ToolGroup, ref[ProcessBlock]] = WeakKeyDictionary()
        self._replay_failures_after: WeakSet[Widget] = WeakSet()

    def clear(self) -> None:
        self._completed.clear()
        self._blocks.clear()
        self._groups.clear()
        self._replay_failures_after.clear()

    def remember_replay_failures(self, preceding_widgets: tuple[Widget, ...]) -> None:
        """Keep omitted statuses in document order without mounting status controls."""
        self._replay_failures_after.update(preceding_widgets)

    def _plan(self, widgets: list[Widget], *, protect_last_turn: bool = False) -> dict[int, ProcessBlock]:
        insertions: dict[int, ProcessBlock] = {}
        segments = plan_process_segments(
            [
                replace(process_entry(widget), failure_after=True)
                if widget in self._replay_failures_after
                else process_entry(widget)
                for widget in widgets
            ],
            protect_last_turn=protect_last_turn,
        )
        for segment in segments:
            if not segment.fold:
                continue
            anchor = widgets[segment.anchor]
            if anchor in self._completed:
                continue
            if not segment.members:
                self._completed.add(anchor)
                continue
            members = [widgets[index] for index in segment.members]
            if any(not isinstance(member, ProcessMember) for member in members):
                raise TypeError("A planned process member must support process visibility")
            typed_members = [member for member in members if isinstance(member, ProcessMember)]
            if len(typed_members) == 1 and folds_with_turn(only := typed_members[0]):
                if only.foldable:
                    only.collapsed = True
                self._completed.add(anchor)
                continue
            block = ProcessBlock(
                typed_members,
                segment_index=segment.index,
                tally=process_tally(typed_members),
            )
            insertions[segment.anchor] = block
        return insertions

    def _activate(self, anchor: Widget, block: ProcessBlock) -> None:
        """Hide members only once their reachable header owns the segment."""
        self._completed.add(anchor)
        self._blocks.add(block)
        for member in block.members:
            if isinstance(member, ToolGroup):
                self._groups[member] = ref(block)
        block.try_finalize_fold()

    async def complete_live(self) -> None:
        widgets = self._host.transcript_content_children()
        starts = [
            index for index, widget in enumerate(widgets) if isinstance(widget, UserMessage) and not widget.is_injection
        ]
        if not starts:
            return
        widgets = widgets[starts[-1] :]
        for index, block in self._plan(widgets).items():
            if await self._host.mount_transcript_after(widgets[index], block):
                self._activate(widgets[index], block)

    def prepare_replay(self, widgets: list[Widget], *, protect_last_turn: bool) -> list[Widget]:
        insertions = self._plan(widgets, protect_last_turn=protect_last_turn)
        result: list[Widget] = []
        for index, widget in enumerate(widgets):
            result.append(widget)
            if block := insertions.get(index):
                result.append(block)
                # Detached replay owns both header and members in one pending
                # sequence; their visibility must be settled before row budgeting.
                self._activate(widget, block)
        return result

    def block_for_group(self, group: ToolGroup) -> ProcessBlock | None:
        reference = self._groups.get(group)
        return reference() if reference is not None else None

    def member_changed(self, block: ProcessBlock) -> None:
        tally = process_tally(block.members)
        if block.tally != tally:
            block.tally = tally
            block.refresh(layout=False)
        block.try_finalize_fold()

    def folds_collapsed(self) -> bool | None:
        blocks = [block for block in self._blocks if not block.blocked]
        return all(block.collapsed for block in blocks) if blocks else None

    def set_fold_state(self, collapsed: bool) -> None:
        """Honor a global keyboard choice for mounted and pending process sections."""
        for block in self._blocks:
            block.requested_collapsed = collapsed
            block.try_finalize_fold()
