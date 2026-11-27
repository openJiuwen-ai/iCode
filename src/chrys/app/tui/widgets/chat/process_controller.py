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
    from collections.abc import Callable, Iterable, Iterator


class ProcessHost(Protocol):
    def current_turn_children(self) -> list[Widget]: ...

    def transcript_content_children(self) -> list[Widget]: ...

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
    """Keep occurrence order except for process titles and final answers."""
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
        if widget.is_structured_completion:
            return ProcessEntry("completion")
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
        self._resume_anchor: ProcessBlock | LiveProcessMember | None = None
        # A stop before the prompt mounts releases the row without rebuilding its title yet.
        self._resumed_title_members: WeakSet[LiveProcessMember] = WeakSet()
        self._continued = False
        self._pending_turn_end: tuple[ref[Widget], bool] | None = None

    def clear(self) -> None:
        # The panel removes all children itself.
        self._release_live_rows(reveal_members=False)
        self._released_rows.clear()
        self._resume_anchor = None
        self._resumed_title_members.clear()
        self._continued = False
        self._pending_turn_end = None
        self._completed.clear()
        self._blocks.clear()
        self._owners.clear()
        self._replay_failures_after.clear()

    @property
    def new_members_collapsed(self) -> bool:
        return self._active_row is None or self._active_row.collapsed

    def waiting_live_rows(self) -> Iterator[LiveProcessRow]:
        """Yield the rows that cover a wait, including an open title before its first new step."""
        return (
            row
            for row in self._live_rows
            if (row.collapsed or row.resumed) and row.display and row.parent is self._host
        )

    def begin_segment(self, start: Widget) -> None:
        self.seal_segment()
        self._segment_start = start

    async def _mount_live_header(self, row: LiveProcessRow, *, before: AgentMessage | None = None) -> bool:
        anchor, self._resume_anchor = self._resume_anchor, None
        if anchor is not None:
            placed = await self._host.place_transcript_entry(row, anchor, before=True)
            if isinstance(anchor, ProcessBlock):
                self._host.forget_transcript_widget(anchor)
                await remove_all_shielded([anchor])
            if placed or row.is_attached:
                return placed
        if self._segment_start is not None:
            return await self._host.place_transcript_entry(row, self._segment_start)
        # A continued run starts after the steps already left in the transcript.
        if before is not None:
            return await self._host.place_transcript_entry(row, before, before=True)
        return await self._host.mount_transcript_widget(row)

    async def mount_live_member(self, member: LiveProcessMember, *, answer: AgentMessage | None = None) -> bool:
        """Mount activity in occurrence order while keeping the pending preview visible."""
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

    async def add_intermediate(self, message: AgentMessage) -> None:
        """An already mounted answer became part of this segment."""
        if not message.is_attached:
            return
        row, new = self._register_live_member(message)
        if new and not await self._mount_live_header(row, before=message):
            await self.remove_live_members((message,))
            return
        self._host.process_members_changed()

    def _register_live_member(
        self, member: LiveProcessMember, *, answer: AgentMessage | None = None
    ) -> tuple[LiveProcessRow, bool]:
        row = self._active_row
        new = row is None
        if row is None:
            row = self._active_row = LiveProcessRow()
            self._live_rows.append(row)
            if self._segment_start is None:
                self._resume_stopped_segment(row)
        if self.owner_of(member) is row:
            return row, False
        row.answering = answer
        row.summarize_answer_preview = True
        self._join_row(row, member)
        self._update_live_row(row)
        return row, new

    def continue_stopped_turn(self) -> None:
        """The next work belongs to the turn that just stopped.

        An end it deferred until its prompt loads is no longer its end: it ends again later and is planned then.
        """
        self._continued = True
        if (pending := self._pending_turn_end) is not None and pending[0]() in self._host.current_turn_children():
            self._pending_turn_end = None

    async def resume_stopped_title(self) -> None:
        """Take over the stopped title when Continue starts or the title finishes replaying."""
        if not self._continued or self._segment_start is not None:
            return
        row = self._active_row
        if row is None:
            row = LiveProcessRow()
        elif row.stopped_members or not row.is_attached or row._pruning:
            return
        self._resume_stopped_segment(row)
        if self._resume_anchor is None:
            return
        if self._active_row is None:
            self._active_row = row
            self._live_rows.append(row)
        if not await self._mount_live_header(row):
            self._release_live_rows(reveal_members=True)
            await self.remove_released_rows()
            return
        self._update_live_row(row)
        self._host.process_members_changed()

    async def resume_replayed_title(self) -> None:
        """Merge a late title into the continuation's first row even after guidance sealed it."""
        if not self._live_rows or not (row := self._live_rows[0]).settled:
            await self.resume_stopped_title()
            return
        if row.stopped_members or not row.is_attached or row._pruning:
            return
        self._resume_stopped_segment(row)
        if self._resume_anchor is not None and await self._mount_live_header(row):
            self._stop_live_preview(row)
            row.settle()
            self._host.process_members_changed()

    def _resume_stopped_segment(self, row: LiveProcessRow) -> None:
        """A continued run carries on in the segment it stopped in: that segment's title becomes *row* again."""
        if not self._continued:
            return
        widgets = self._turn_occurrences()
        if row.settled:
            if row.latest not in widgets:
                return
            # Keep the search above the boundary that sealed this row.
            widgets = widgets[: widgets.index(row.latest) + 1]
        tail: list[LiveProcessMember] = []
        block: ProcessBlock | None = None
        answering: AgentMessage | None = None
        for entry in reversed(widgets):
            if isinstance(entry, (UserMessage, CompactionCard, ContextFoldWidget)):
                break
            owner = self.owner_of(entry)
            if isinstance(owner, ProcessBlock) and owner in self._blocks and owner.is_attached:
                block = owner
                break
            if isinstance(entry, AgentMessage) and entry.is_provisional:
                if answering is None:
                    answering = entry
                continue
            # An earlier Continue may have stopped while its prompt still replayed.
            # Its steps are mounted after the old title but have not been folded into it yet.
            if owner is None and (
                isinstance(entry, (ThinkingMessage, ToolGroup))
                or (isinstance(entry, AgentMessage) and process_entry(entry).kind == "intermediate")
            ):
                tail.append(entry)

        if block is None:
            if self._resumed_title_members.isdisjoint(tail):
                return
            # Another Continue can start before replay has rebuilt the title we took over.
            self._resume_anchor = tail[-1]
            members: Iterable[ProcessMember] = reversed(tail)
        else:
            self._blocks.discard(block)
            self._resume_anchor = block
            self._resumed_title_members.clear()
            if not row.is_attached:
                row.collapsed = block.collapsed
            members = (*block.members, *reversed(tail))
        if row.answering is None:
            row.answering = answering
        for member in members:
            self._owners.pop(member, None)
            if not member.is_attached:
                continue
            if isinstance(member, (AgentMessage, ThinkingMessage, ToolGroup)):
                opened = not member.collapsed
                self._join_row(row, member)
                row.stopped_members.add(member)
                # A step the user opened under the folded title stays open inside it.
                member.collapsed = not opened
            else:
                # An earlier notice a replay folded shows again, as notices do while a turn runs.
                member.set_process_hidden(False)
        self._resumed_title_members.update(row.stopped_members)

    def _join_row(self, row: LiveProcessRow, member: LiveProcessMember) -> None:
        if member.live_status_animates:
            row.running_members.add(member)
        # A streamed answer can settle after a later member has already mounted.
        # New detached members append, while mounted ones use their occurrence stamp.
        seq = self._host.transcript_occurrence(member)
        index = len(row.members)
        if seq is not None:
            while index:
                previous = self._host.transcript_occurrence(row.members[index - 1])
                if previous is not None and previous <= seq:
                    break
                index -= 1
        row.members.insert(index, member)
        self._owners[member] = ref(row)
        if isinstance(member, ToolGroup):
            member.live_process_state_changed = self.group_state_changed
            row.set_group_tally(member, member.tool_tally)
        member.set_live_status_reporting(True)
        self._sync_member_visibility(row, member)

    def seal_segment(self) -> None:
        """Keep this segment interactive with a frozen activity preview."""
        row, self._active_row = self._active_row, None
        self._segment_start = None
        if row is not None:
            self.set_live_row_collapsed(row, True)
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
                if isinstance(member, ToolGroup):
                    member.live_process_state_changed = None
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
                row.stopped_members.discard(member)
                row.running_members.discard(member)
                if isinstance(member, ToolGroup):
                    member.live_process_state_changed = None
                    row.set_group_tally(member, None)
                member.set_live_status_reporting(False)
                member.set_process_hidden(False)
        emptied: list[LiveProcessRow] = []
        for row, old_latest in changed.items():
            if row.members:
                if row.settled:
                    row.summarized = row.resumed
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
            self.set_answer_preview(None)
        await self.remove_live_members(
            [
                member
                for row in self._live_rows
                for member in row.members
                if isinstance(member, AgentMessage) and member.segment_ids & segment_ids
            ]
        )

    async def forget_removed_members(self, removed: Iterable[Widget]) -> None:
        """Drop members a withdrawal or a turn cleanup removed; a title left with none goes too."""
        members = set(removed)
        blocks = {owner for member in members if isinstance(owner := self.owner_of(member), ProcessBlock)}
        emptied: list[ProcessBlock] = []
        for block in blocks:
            for member in block.members:
                if member in members and self.owner_of(member) is block:
                    self._owners.pop(member, None)
            block.members = [member for member in block.members if member not in members]
            if block.members:
                block.tally = process_tally(block.members)
                block.refresh(layout=False)
            else:
                self._blocks.discard(block)
                self._host.forget_transcript_widget(block)
                emptied.append(block)
        if emptied:
            await remove_all_shielded(emptied)
        if blocks:
            self._host.process_members_changed()

    def owner_of(self, member: Widget) -> ProcessBlock | LiveProcessRow | None:
        reference = self._owners.get(member) if isinstance(member, ProcessMember) else None
        return reference() if reference is not None else None

    def guide_owner_of(self, member: Widget) -> ProcessBlock | LiveProcessRow | None:
        row = self._active_row
        if isinstance(member, AgentMessage) and member.is_provisional and row is not None and row.answering is member:
            return row
        return self.owner_of(member)

    def live_status_changed(self, member: ProcessMember) -> None:
        row = self._active_row
        if row is None or self.owner_of(member) is not row:
            return
        if isinstance(member, ThinkingMessage):
            if member.live_status_animates:
                row.running_members.add(member)
            else:
                row.running_members.discard(member)
        if row.collapsed:
            was_summarized = row.summarized
            self._sync_live_summary(row)
            if row.latest is member or row.summarized != was_summarized:
                row.update_status()

    def set_answer_preview(self, answer: AgentMessage | None, *, summarize: bool = True) -> None:
        """Attach the preview to the process guide and allow the folded status to summarize."""
        row = self._active_row
        if row is not None:
            row.answering = answer
            row.summarize_answer_preview = summarize
            self._update_live_row(row)
            self._host.process_members_changed()

    @staticmethod
    def _sync_live_summary(row: LiveProcessRow) -> None:
        row.summarized = (
            row.collapsed
            and (row.resumed or (row.answering is not None and row.summarize_answer_preview))
            and not row.running_members
        )

    def refresh_localization(self) -> None:
        for row in self._live_rows:
            row.refresh(layout=False)

    def group_state_changed(self, group: ToolGroup, tally: ToolTally) -> None:
        """Record tool changes before a new answer can overtake their queued UI messages."""
        row = self.owner_of(group)
        if isinstance(row, LiveProcessRow):
            row.set_group_tally(group, tally)
            if not row.settled:
                if group.live_status_animates:
                    row.running_members.add(group)
                else:
                    row.running_members.discard(group)

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
            if isinstance(member, ToolGroup) or member.foldable:
                member.collapsed = True
        if hidden and member.is_attached and member.display:
            note_fold(member)
        member.set_process_hidden(hidden)

    def _update_live_row(self, row: LiveProcessRow) -> None:
        self._sync_live_summary(row)
        row.update_status()

    def thinking_finished(self, member: ThinkingMessage) -> None:
        owner = self.owner_of(member)
        if isinstance(owner, LiveProcessRow):
            owner.running_members.discard(member)
            if owner.collapsed and not member.process_hidden:
                self._sync_member_visibility(owner, member)
        if owner is not None:
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
        self, widgets: list[Widget], *, protect_last_turn: bool = False, stopped_after: Widget | None = None
    ) -> tuple[dict[Widget, ProcessBlock], dict[Widget, tuple[Widget, Widget]]]:
        headers: dict[Widget, ProcessBlock] = {}
        answers: dict[Widget, tuple[Widget, Widget]] = {}
        segments = plan_process_segments(
            [
                replace(process_entry(widget), failure_after=True)
                if widget is stopped_after or widget in self._replay_failures_after
                else process_entry(widget)
                for widget in widgets
            ],
            protect_last_turn=protect_last_turn,
        )
        for segment in segments:
            if not segment.fold:
                continue
            anchor = widgets[segment.anchor]
            if segment.answer is not None and segment.answer_after is not None:
                answers[anchor] = (widgets[segment.answer], widgets[segment.answer_after])
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

    async def complete_live(
        self, fold_choice: Callable[[], bool | None] | None = None, *, late_injection: bool = False
    ) -> None:
        widgets = self._turn_occurrences()
        if late_injection and (pending := self._pending_turn_end) is not None:
            end, stopped = pending
            # A consumed note can arrive after the final reply but before the original prompt.
            # Include it in the deferred plan so the reply moves below it when the prompt mounts.
            if not stopped and end() in widgets and isinstance(note := widgets[-1], UserMessage) and note.is_injection:
                self._pending_turn_end = (ref(note), False)
                return
        if self._continued:
            self._continued = False
            if not widgets or not isinstance(widgets[0], UserMessage) or widgets[0].is_injection:
                self._defer_turn_end(widgets)
                return
            await self._replan_turn(widgets, fold_choice)
            return
        if not widgets or not isinstance(widgets[0], UserMessage) or widgets[0].is_injection:
            return
        await self._mount_plan(widgets)

    def _defer_turn_end(self, widgets: list[Widget]) -> None:
        """Plan the turn once its prompt has mounted, ending at its last step.

        Its ending notice can go before then (a new prompt removes it), so the step it followed
        remembers that the turn stopped, as a replay remembers an omitted notice.
        """
        steps = [widget for widget in widgets if process_entry(widget).kind != "failure"]
        if not steps:
            self._pending_turn_end = None
            return
        end = steps[-1]
        self._pending_turn_end = (ref(end), widgets[-1] is not end)

    async def complete_replayed_turn(self, fold_choice: Callable[[], bool | None]) -> bool:
        """Replan a continuation that finished before its original prompt mounted."""
        if self._pending_turn_end is None:
            return False
        pending, stopped = self._pending_turn_end
        end = pending()
        if end is None or not end.is_attached or (end_occurrence := self._host.transcript_occurrence(end)) is None:
            self._pending_turn_end = None
            return False
        # Display order can move an answer within a turn but keeps its prompt first.
        occurrences: list[tuple[int, Widget]] = []
        for entry in reversed(self._host.transcript_content_children()):
            if not entry.is_attached or isinstance(entry, (LiveProcessRow, ProcessBlock)):
                continue
            occurrence = self._host.transcript_occurrence(entry)
            if occurrence is None or occurrence > end_occurrence:
                continue
            occurrences.append((occurrence, entry))
            if isinstance(entry, UserMessage) and not entry.is_injection:
                self._pending_turn_end = None
                occurrences.sort(key=lambda item: item[0])
                widgets = [widget for _, widget in occurrences]
                await self._replan_turn(widgets, fold_choice, stopped_after=end if stopped else None)
                return True
        return False

    async def _replan_turn(
        self,
        widgets: list[Widget],
        fold_choice: Callable[[], bool | None] | None,
        *,
        stopped_after: Widget | None = None,
    ) -> None:
        blocks: set[ProcessBlock] = set()
        for widget in widgets:
            self._completed.discard(widget)
            if isinstance(owner := self.owner_of(widget), ProcessBlock) and owner in self._blocks:
                blocks.add(owner)
        for block in blocks:
            self._blocks.discard(block)
            self._host.forget_transcript_widget(block)
            for member in block.members:
                if self.owner_of(member) is block:
                    self._owners.pop(member, None)
                    member.set_process_hidden(False)
        if blocks:
            await remove_all_shielded(blocks)
        await self._mount_plan(widgets, stopped_after=stopped_after)
        # Mounting the new titles can yield to another /fold; both continuation paths use its latest choice.
        collapsed = fold_choice() if fold_choice is not None else None
        if collapsed is not None:
            blocks = {owner for widget in widgets if isinstance(owner := self.owner_of(widget), ProcessBlock)}
            for block in blocks:
                block.requested_collapsed = collapsed
                block.try_finalize_fold()
            for widget in widgets:
                if (isinstance(widget, ToolGroup) and not widget.collapse_locked) or (
                    folds_with_turn(widget) and widget.foldable
                ):
                    widget.collapsed = collapsed

    async def _mount_plan(self, widgets: list[Widget], *, stopped_after: Widget | None = None) -> None:
        headers, answers = self._plan(widgets, stopped_after=stopped_after)
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
        if not isinstance(block, ProcessBlock):
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
