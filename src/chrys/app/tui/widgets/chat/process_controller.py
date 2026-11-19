# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply one process plan to live and restored transcripts without copying widget state."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Protocol
from weakref import WeakKeyDictionary, WeakSet, ref

from textual.widget import Widget

from chrys.app.tui.util.removal import remove_all_shielded
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.fold_rows import ToolTally
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessMember, LiveProcessRow
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
from chrys.app.tui.widgets.chat.transcript_panel import note_fold

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


class ProcessHost(Protocol):
    def current_turn_children(self) -> list[Widget]: ...

    async def mount_transcript_widget(self, widget: Widget) -> bool: ...

    async def place_transcript_entry(self, widget: Widget, anchor: Widget, *, before: bool = False) -> bool: ...

    def transcript_occurrence(self, widget: Widget) -> int | None: ...

    def apply_transcript_order(self, widgets: list[Widget]) -> None: ...

    def process_members_changed(self) -> None: ...

    def forget_transcript_widget(self, widget: Widget) -> None: ...


def _display_order(
    occurrences: list[Widget],
    *,
    headers: dict[Widget, Widget] | None = None,
    answers: dict[Widget, Widget] | None = None,
) -> list[Widget]:
    """Keep occurrence order except for process titles and provisional or final answers."""
    if not headers and not answers:
        return occurrences
    headers = headers or {}
    after_member: dict[Widget, Widget] = {}
    if answers:
        positions = {entry: index for index, entry in enumerate(occurrences)}
        after_member = {member: answer for answer, member in answers.items() if positions[answer] < positions[member]}
    displaced = set(after_member.values())
    result: list[Widget] = []
    for entry in occurrences:
        if entry not in displaced:
            result.append(entry)
        if header := headers.get(entry):
            result.append(header)
        if answer := after_member.get(entry):
            result.append(answer)
    return result


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
    """Own live segment rows and completed process headers."""

    def __init__(self, host: ProcessHost) -> None:
        self._host = host
        self._completed: WeakSet[Widget] = WeakSet()
        self._blocks: WeakSet[ProcessBlock] = WeakSet()
        self._owners: WeakKeyDictionary[ProcessMember, ref[ProcessBlock | LiveProcessRow]] = WeakKeyDictionary()
        self._replay_failures_after: WeakSet[Widget] = WeakSet()
        self._live_rows: list[LiveProcessRow] = []
        self._active_row: LiveProcessRow | None = None
        self._segment_start: Widget | None = None
        self._released_rows: list[LiveProcessRow] = []

    def clear(self) -> None:
        # The panel removes all children itself.
        self._release_live_rows(reveal_members=False)
        self._released_rows.clear()
        self._completed.clear()
        self._blocks.clear()
        self._owners.clear()
        self._replay_failures_after.clear()

    @property
    def new_members_collapsed(self) -> bool:
        return self._active_row is None or self._active_row.collapsed

    def folded_live_rows(self) -> Iterator[LiveProcessRow]:
        """Yield the previews that can represent hidden work while waiting."""
        return (row for row in self._live_rows if row.collapsed and row.display and row.parent is self._host)

    def begin_segment(self, start: Widget) -> None:
        self.seal_segment()
        self._segment_start = start

    async def _mount_live_header(self, row: LiveProcessRow, *, before: AgentMessage | None = None) -> bool:
        if self._segment_start is not None:
            return await self._host.place_transcript_entry(row, self._segment_start)
        # A continued run starts after the steps already left in the transcript.
        if before is not None:
            return await self._host.place_transcript_entry(row, before, before=True)
        return await self._host.mount_transcript_widget(row)

    async def mount_live_member(self, member: LiveProcessMember, *, answer: AgentMessage | None = None) -> bool:
        """Keep the current answer below newly mounted activity in its segment."""
        if answer is not None and not answer.is_attached:
            answer = None
        row, new = self._register_live_member(member, answer=answer)
        if new and (not await self._mount_live_header(row, before=answer) or self._active_row is not row):
            await self.remove_live_members((member,))
            return False
        mounted = await self._host.mount_transcript_widget(member)
        if not mounted:
            await self.remove_live_members((member,))
        elif self._active_row is row:
            if new or answer is not None:
                self._arrange_live_segment(row)
            self._host.process_members_changed()
        return mounted

    def _turn_occurrences(self) -> list[Widget]:
        return sorted(
            (
                entry
                for entry in self._host.current_turn_children()
                if not isinstance(entry, (LiveProcessRow, ProcessBlock))
            ),
            key=lambda entry: self._host.transcript_occurrence(entry) or 0,
        )

    def _arrange_live_segment(self, row: LiveProcessRow) -> None:
        """Project the active segment from arrival order, including intervening notices."""
        answer, latest = row.answering, row.latest
        anchor = self._segment_start
        starts = [
            stamp
            for entry in (anchor, row.members[0] if row.members else None, answer)
            if entry is not None and (stamp := self._host.transcript_occurrence(entry)) is not None
        ]
        if not starts:
            return
        start = min(starts)
        entries: list[Widget] = []
        for entry in self._turn_occurrences():
            if (self._host.transcript_occurrence(entry) or 0) < start:
                continue
            if entry is not anchor and isinstance(entry, (UserMessage, CompactionCard, ContextFoldWidget)):
                break
            entries.append(entry)
        self._host.apply_transcript_order(
            _display_order(
                entries,
                headers={anchor: row} if anchor in entries and anchor is not None else None,
                answers={answer: latest}
                if answer is not None and latest is not None and answer in entries and latest in entries
                else None,
            )
        )

    async def add_intermediate(self, message: AgentMessage) -> None:
        """An already mounted answer became part of this segment."""
        if not message.is_attached:
            return
        row, new = self._register_live_member(message)
        if new and not await self._mount_live_header(row, before=message):
            await self.remove_live_members((message,))
            return
        self._arrange_live_segment(row)
        self._host.process_members_changed()

    def _register_live_member(
        self, member: LiveProcessMember, *, answer: AgentMessage | None = None
    ) -> tuple[LiveProcessRow, bool]:
        row = self._active_row
        new = row is None
        if row is None:
            row = self._active_row = LiveProcessRow()
            self._live_rows.append(row)
        if self.owner_of(member) is row:
            return row, False
        row.answering = answer
        if member.live_status_animates:
            row.running_members.add(member)
        # A streamed answer can settle after a later member has already mounted.
        # New detached members append, while mounted ones use their occurrence stamp.
        seq = self._host.transcript_occurrence(member)
        index = len(row.members)
        if seq is not None:
            for i, existing in enumerate(row.members):
                existing_seq = self._host.transcript_occurrence(existing)
                if existing_seq is None or existing_seq > seq:
                    index = i
                    break
        row.members.insert(index, member)
        self._owners[member] = ref(row)
        member.set_live_status_reporting(True)
        self._sync_member_visibility(row, member)
        self._update_live_row(row)
        return row, new

    def seal_segment(self) -> None:
        """Keep this segment interactive with a frozen activity preview."""
        row, self._active_row = self._active_row, None
        self._segment_start = None
        if row is not None:
            self.set_live_row_collapsed(row, True)
            row.tally = process_tally(row.members)
            self._stop_live_preview(row)
            row.settle()

    @staticmethod
    def _stop_live_preview(row: LiveProcessRow) -> None:
        row.set_running(False)
        for member in row.members:
            member.set_live_status_reporting(False)

    def _release_live_rows(self, *, reveal_members: bool) -> None:
        self._active_row = None
        self._segment_start = None
        rows, self._live_rows = self._live_rows, []
        if reveal_members and rows:
            note_fold(rows[-1])
        for row in rows:
            self._stop_live_preview(row)
            self._host.forget_transcript_widget(row)
            row.display = False
            for member in row.members:
                self._owners.pop(member, None)
                if reveal_members:
                    member.set_process_hidden(False)
        if reveal_members:
            if rows:
                self._host.apply_transcript_order(_display_order(self._turn_occurrences()))
            self._released_rows.extend(rows)

    def release_live_rows(self) -> None:
        """Release visible members synchronously when the run stops."""
        self._release_live_rows(reveal_members=True)

    async def end_live(self) -> None:
        """Release and remove every segment before planning completed rows."""
        self.release_live_rows()
        await self.remove_released_rows()

    async def remove_released_rows(self) -> None:
        """Remove rows already released from the transcript."""
        rows, self._released_rows = self._released_rows, []
        if rows:
            await remove_all_shielded(rows)

    async def remove_live_members(self, members: Iterable[LiveProcessMember]) -> None:
        """Forget abandoned thinking or a group whose mount was invalidated."""
        changed: dict[LiveProcessRow, LiveProcessMember | None] = {}
        for member in members:
            row = self.owner_of(member)
            if isinstance(row, LiveProcessRow):
                changed.setdefault(row, row.latest)
                self._owners.pop(member, None)
                row.members.remove(member)
                row.running_members.discard(member)
                member.set_live_status_reporting(False)
                member.set_process_hidden(False)
        emptied: list[LiveProcessRow] = []
        for row, old_latest in changed.items():
            if row is self._active_row and row.answering is not None:
                self._arrange_live_segment(row)
            if row.members:
                if row.settled:
                    row.tally = process_tally(row.members)
                    row.update_status(resample=row.latest is not old_latest)
                else:
                    self._update_live_row(row)
                continue
            if row is self._active_row:
                self._active_row = None
            self._live_rows.remove(row)
            row.set_running(False)
            self._host.forget_transcript_widget(row)
            note_fold(row)
            emptied.append(row)
        if emptied:
            await remove_all_shielded(emptied)
        if changed:
            self._host.process_members_changed()

    async def withdraw_live_answers(self, segment_ids: frozenset[str]) -> None:
        """Drop ownership and stop summarizing before the transcript removes a withdrawn answer."""
        row = self._active_row
        if row is not None and row.answering is not None and row.answering.segment_ids & segment_ids:
            row.answering = None
            self._update_live_row(row)
        await self.remove_live_members(
            [
                member
                for row in self._live_rows
                for member in row.members
                if isinstance(member, AgentMessage) and member.segment_ids & segment_ids
            ]
        )

    def owner_of(self, member: Widget) -> ProcessBlock | LiveProcessRow | None:
        reference = self._owners.get(member) if isinstance(member, ProcessMember) else None
        return reference() if reference is not None else None

    def live_status_changed(self, member: ProcessMember) -> None:
        row = self._active_row
        if row is None or self.owner_of(member) is not row:
            return
        if not isinstance(member, (AgentMessage, ThinkingMessage, ToolGroup)):
            return
        if member.live_status_animates:
            row.running_members.add(member)
        else:
            row.running_members.discard(member)
        if row.collapsed:
            was_summarized = row.summarized
            self._sync_live_summary(row)
            if row.latest is member or row.summarized != was_summarized:
                row.update_status()

    def answer_streaming(self, answer: AgentMessage) -> None:
        """Summarize a folded segment once its work ends under the answer."""
        row = self._active_row
        if row is not None:
            row.answering = answer
            # Status messages from work that just started may still be queued.
            row.running_members = {member for member in row.members if member.live_status_animates}
            self._update_live_row(row)

    @staticmethod
    def _sync_live_summary(row: LiveProcessRow) -> None:
        summarized = row.collapsed and row.answering is not None and not row.running_members
        if summarized and not row.summarized:
            row.tally = process_tally(row.members)
        row.summarized = summarized

    def refresh_localization(self) -> None:
        for row in self._live_rows:
            row.refresh(layout=False)

    def group_lock_changed(self, group: ToolGroup) -> None:
        row = self.owner_of(group)
        if isinstance(row, LiveProcessRow):
            self._sync_member_visibility(row, group)

    @staticmethod
    def _sync_member_visibility(row: LiveProcessRow, member: LiveProcessMember) -> None:
        protected = (isinstance(member, ToolGroup) and member.collapse_locked) or (
            isinstance(member, ThinkingMessage) and not member.foldable and not member.collapsed
        )
        hidden = row.collapsed and not protected
        if hidden:
            if member.is_attached and not member.process_hidden:
                focused = member.app.focused
                if focused is not None and (focused is member or member in focused.ancestors):
                    member.screen.set_focus(None, scroll_visible=False)
            member.collapsed = True
        if hidden and member.is_attached and member.display:
            note_fold(member)
        member.set_process_hidden(hidden)

    def _update_live_row(self, row: LiveProcessRow) -> None:
        self._sync_live_summary(row)
        if not row.collapsed and not row.settled:
            row.tally = process_tally(row.members)
        row.update_status()

    def thinking_finished(self, member: ThinkingMessage) -> None:
        if self.owner_of(member) is not None:
            self._host.process_members_changed()

    def set_live_row_collapsed(self, row: LiveProcessRow, collapsed: bool) -> None:
        if row not in self._live_rows or row.collapsed == collapsed:
            return
        row.collapsed = collapsed
        for member in row.members:
            self._sync_member_visibility(row, member)
        self._update_live_row(row)
        self._host.process_members_changed()

    def remember_replay_failures(self, preceding_widgets: tuple[Widget, ...]) -> None:
        """Keep omitted statuses in document order without mounting status controls."""
        self._replay_failures_after.update(preceding_widgets)

    def _plan(
        self, widgets: list[Widget], *, protect_last_turn: bool = False
    ) -> tuple[dict[Widget, ProcessBlock], dict[Widget, tuple[Widget, Widget]]]:
        headers: dict[Widget, ProcessBlock] = {}
        answers: dict[Widget, tuple[Widget, Widget]] = {}
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
            if segment.answer is not None and segment.members:
                answers[anchor] = (widgets[segment.answer], widgets[segment.members[-1]])
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
            headers[anchor] = ProcessBlock(
                typed_members,
                segment_index=segment.index,
                tally=process_tally(typed_members),
            )
        return headers, answers

    def _activate(self, anchor: Widget, block: ProcessBlock) -> None:
        """Hide members only once their reachable header owns the segment."""
        self._completed.add(anchor)
        self._blocks.add(block)
        for member in block.members:
            self._owners[member] = ref(block)
        block.try_finalize_fold()

    async def complete_live(self) -> None:
        widgets = self._turn_occurrences()
        if not widgets or not isinstance(widgets[0], UserMessage):
            return
        headers, answers = self._plan(widgets)
        mounted: dict[Widget, Widget] = {}
        for anchor, block in headers.items():
            if await self._host.place_transcript_entry(block, anchor):
                mounted[anchor] = block
                self._activate(anchor, block)
            else:
                answers.pop(anchor, None)
        self._host.apply_transcript_order(_display_order(widgets, headers=mounted, answers=dict(answers.values())))

    def prepare_replay(self, widgets: list[Widget], *, protect_last_turn: bool) -> list[Widget]:
        headers, answers = self._plan(widgets, protect_last_turn=protect_last_turn)
        for anchor, block in headers.items():
            # Detached replay settles visibility before row budgeting.
            self._activate(anchor, block)
        return _display_order(widgets, headers=dict(headers), answers=dict(answers.values()))

    def member_changed(self, group: ToolGroup) -> None:
        block = self.owner_of(group)
        if isinstance(block, LiveProcessRow):
            if block.settled or block.summarized or not block.collapsed:
                tally = process_tally(block.members)
                if block.tally != tally:
                    block.tally = tally
                    block.refresh(layout=False)
            return
        if block is None:
            return
        tally = process_tally(block.members)
        if block.tally != tally:
            block.tally = tally
            block.refresh(layout=False)
        block.try_finalize_fold()

    def folds_collapsed(self) -> bool | None:
        blocks = [*(block for block in self._blocks if not block.blocked), *self._live_rows]
        return all(block.collapsed for block in blocks) if blocks else None

    def set_fold_state(self, collapsed: bool) -> None:
        """Honor a global keyboard choice for mounted and pending process sections."""
        for block in self._blocks:
            block.requested_collapsed = collapsed
            block.try_finalize_fold()
        for row in self._live_rows:
            self.set_live_row_collapsed(row, collapsed)
