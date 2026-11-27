# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tail-first replay mounting for the chat transcript.

A restored transcript mounts its newest entries (about three viewports)
before replay returns, so the restore dialog closes as soon as the part the
user sees is ready. A panel-owned task then prepends the older entries above
them, newest batch first, in bounded batches.

Invariants while a prepend runs:

- Pending entries sit, in document order, directly above the oldest mounted
  entry stamped after them, so the transcript is whole at every moment: a
  batch leaves ``pending`` in the same synchronous step that registers it as
  children, and every entry is stamped with its document order before the
  first mount. Entries mounted before the replay (a restore warning) keep
  their place above all replayed history.
- Entries mounted meanwhile (a live turn, another replayed block) are newer
  than all pending ones and append below.
- ``stop`` ends the prepend at a batch boundary and never interrupts a mount;
  only ``abandon`` (panel unmount) cancels the task.
- A prepended batch is a child from the moment it registers, but has no
  geometry until the refresh after it mounted. A TOC jump to a pending turn,
  or to one in such a batch, waits for that refresh; a newer jump, a user
  scroll, ``stop`` or ``abandon`` before then drops it.
- A view that leaves the bottom keeps its content while batches land above
  it, until the last batch has laid out, which can be after the prepend
  ended.
- One GC pause spans the whole replay: a young pass between batches frees
  the cycles each batch left behind, and one full collection ends the pause
  when every entry has mounted.
"""

from __future__ import annotations

import asyncio
import logging

from textual.widget import Widget

from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    StreamedMarkdownMessage,
    UserMessage,
    folds_with_turn,
)
from chrys.app.tui.widgets.chat.ports import ReplayMountHost
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.scroll_controller import ChatGcPauseClaim
from chrys.app.tui.widgets.chat.tool_call import ToolGroup

logger = logging.getLogger(__name__)

REPLAY_MOUNT_BATCH_SIZE = 32
"""Most top-level entries registered with Textual in one mount call."""

_TAIL_VIEWPORTS = 3
_MIN_VIEWPORT_ROWS = 40
_MIN_TAIL_ENTRIES = 2
_PREPEND_BATCH_ROWS = 300
_MIN_WRAP_WIDTH = 20
_ENTRY_CHROME_ROWS = 3
_MAX_FOREGROUND_ENTRIES = 4 * REPLAY_MOUNT_BATCH_SIZE


def estimated_rows(widget: Widget, width: int) -> int:
    """Rough rendered height of an unmounted transcript entry.

    Only sizes batches, so it favors being cheap over exact: message text
    counts its lines plus soft wraps at ``width``; folded thinking and tool
    groups are one row with no margin, and other entries replay collapsed
    and count as their header and margins.
    """
    if not widget.display:
        return 0
    if isinstance(widget, ProcessBlock) or (
        isinstance(widget, (StreamedMarkdownMessage, ToolGroup)) and widget.collapsed
    ):
        return 1
    if not isinstance(widget, (AgentMessage, UserMessage)):
        return _ENTRY_CHROME_ROWS
    text = widget.text
    return _ENTRY_CHROME_ROWS + text.count("\n") + len(text) // max(_MIN_WRAP_WIDTH, width)


class ReplayMountController:
    """Mount replayed entries tail first and prepend older history afterwards."""

    def __init__(self, host: ReplayMountHost) -> None:
        self._host = host
        self._pending: list[Widget] = []
        self._unplaced: list[Widget] = []
        """Prepended entries from registration until the refresh after their batch mounted: children without geometry."""
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._gc_pauses: set[ChatGcPauseClaim] = set()
        self._fold_collapsed: bool | None = None
        self._pending_jump: UserMessage | None = None
        """The turn a held jump lands on once its batch lays out."""

    @property
    def in_progress(self) -> bool:
        """Whether older entries are still being prepended."""
        return self._task is not None

    def pending_widgets(self) -> list[Widget]:
        """Entries not mounted yet, in document order."""
        return list(self._pending)

    def last_pending_entry(self) -> Widget | None:
        """The pending entry just above the mounted replay tail."""
        return self._pending[-1] if self._pending else None

    async def wait_complete(self) -> None:
        """Wait until the running prepend has mounted everything or stopped."""
        task = self._task
        if task is not None:
            # ``wait`` neither cancels the task when this waiter is cancelled
            # nor raises the task's own outcome here.
            await asyncio.wait((task,))

    async def mount(self, widgets: list[Widget]) -> None:
        """Mount ``widgets`` and return once the newest of them are mounted."""
        if self._task is not None:
            # Planned after an unfinished prepend, so newer than all of it:
            # these append below the mounted entries in the foreground.
            await self._mount_foreground(widgets)
            return
        viewport = self._host.size
        tail_start = self._batch_start(
            widgets,
            len(widgets),
            row_budget=_TAIL_VIEWPORTS * max(_MIN_VIEWPORT_ROWS, viewport.height),
            min_entries=_MIN_TAIL_ENTRIES,
            width=viewport.width,
            max_entries=_MAX_FOREGROUND_ENTRIES,
            max_visible_entries=REPLAY_MOUNT_BATCH_SIZE,
        )
        if tail_start == 0:
            await self._mount_foreground(widgets)
            return
        self._host.stamp_transcript_order(widgets)
        gc_pause = self._claim_gc_pause()
        handed_off = False
        mounted = False
        try:
            await self._mount_batches(widgets[tail_start:], gc_pause)
            mounted = True
            if self._host.can_mount_replay():
                self._pending = widgets[:tail_start]
                self._stop_requested = False
                self._host.begin_view_hold()
                self._task = asyncio.get_running_loop().create_task(
                    self._prepend(gc_pause),
                    name="chat-replay-prepend",
                )
                handed_off = True
        finally:
            if not handed_off:
                self._release_gc_pause(gc_pause, collect_first=mounted)

    async def stop(self) -> None:
        """End the prepend at its next batch boundary and wait for it.

        A jump waiting for the next refresh is dropped too, even once the prepend
        has ended: the transcript that follows can reuse its turn ids.
        """
        self.forget_jump()
        task = self._task
        if task is not None:
            self._stop_requested = True
            await asyncio.wait((task,))
        # The caller removes the transcript; a batch still waiting for its layout goes with it.
        self._unplaced = []
        self._end_view_hold_once_laid_out()

    def abandon(self) -> None:
        """Cancel the prepend, drop its jumps and release its GC pause at once (panel unmount)."""
        task = self._task
        self._stop_requested = True
        self.forget_jump()
        self._unplaced = []
        self._reset_prepend_state()
        for gc_pause in tuple(self._gc_pauses):
            self._release_gc_pause(gc_pause, collect_first=False)
        if task is not None and not task.done():
            task.cancel()

    def pending_folds_collapsed(self) -> bool | None:
        """Collapsed state pending groups, thinking and summaries will mount with; None without any."""
        if not any(
            (isinstance(widget, ContextFoldWidget) and widget.has_summary())
            or ((isinstance(widget, ToolGroup) or folds_with_turn(widget)) and not widget.process_hidden)
            for widget in self._pending
        ):
            return None
        # Replay builds tool groups, thinking and compaction summaries collapsed.
        return True if self._fold_collapsed is None else self._fold_collapsed

    def fold_choice(self) -> bool | None:
        """The last fold-all choice during this prepend, including while its final batch mounts."""
        return self._fold_collapsed

    def set_fold_state(self, collapsed: bool) -> None:
        """Apply a fold-all choice to groups, thinking and summaries that mount after it."""
        # The whole prepend, not just ``pending``: the last batch has left it while it mounts.
        if self._task is not None:
            self._fold_collapsed = collapsed

    def jump_when_laid_out(self, turn_id: str) -> bool:
        """Hold a jump to a turn that has no geometry yet until its batch lays out; return whether it is held.

        Every jump, held or not, replaces the one held before it.
        """
        self._pending_jump = next(
            (
                widget
                for widget in self._unplaced + self._pending
                if isinstance(widget, UserMessage) and widget.id == turn_id
            ),
            None,
        )
        return self._pending_jump is not None

    def forget_jump(self) -> None:
        """Drop the held jump."""
        self._pending_jump = None

    async def _mount_foreground(self, widgets: list[Widget]) -> None:
        gc_pause = self._claim_gc_pause()
        mounted = False
        try:
            await self._mount_batches(widgets, gc_pause)
            mounted = True
        finally:
            self._release_gc_pause(gc_pause, collect_first=mounted)

    async def _mount_batches(self, widgets: list[Widget], gc_pause: ChatGcPauseClaim) -> None:
        total = len(widgets)
        self._host.report_replay_progress(0, total)
        await asyncio.sleep(0)
        for start in range(0, total, REPLAY_MOUNT_BATCH_SIZE):
            batch = widgets[start : start + REPLAY_MOUNT_BATCH_SIZE]
            await self._host.mount_replay_batch(batch, before=None)
            done = min(start + len(batch), total)
            if done < total:
                gc_pause.collect_young()
            self._host.report_replay_progress(done, total)
            # Each batch is still large enough to retain the recursive-register
            # speedup, while this yield lets the loading overlay and input
            # timers paint instead of sitting behind the entire transcript.
            await asyncio.sleep(0)

    async def _prepend(self, gc_pause: ChatGcPauseClaim) -> None:
        completed = False
        try:
            while self._pending:
                await asyncio.sleep(0)
                if self._stop_requested or not self._host.can_mount_replay():
                    return
                start = self._batch_start(
                    self._pending,
                    len(self._pending),
                    row_budget=_PREPEND_BATCH_ROWS,
                    min_entries=1,
                    width=self._host.size.width,
                )
                batch = self._pending[start:]
                # Leave ``pending`` in the same synchronous step that registers
                # the batch as children, so readers never miss or repeat it.
                del self._pending[start:]
                self._unplaced.extend(batch)
                try:
                    await self._host.mount_replay_batch(batch, before=self._host.transcript_entry_after(batch[-1]))
                except BaseException:
                    self._place(batch)
                    raise
                self._after_batch_mounted(batch)
                if self._pending:
                    gc_pause.collect_young()
            completed = True
        except Exception:
            logger.exception("Prepending older replayed transcript entries failed")
        finally:
            if self._task is asyncio.current_task():
                self._task = None
                self._reset_prepend_state()
            self._release_gc_pause(gc_pause, collect_first=completed)

    def _after_batch_mounted(self, batch: list[Widget]) -> None:
        collapsed = self._fold_collapsed
        if collapsed is not None:
            for root in batch:
                if not root.is_attached:
                    continue
                if isinstance(root, ContextFoldWidget):
                    root.collapsed = collapsed
                    continue
                if isinstance(root, StreamedMarkdownMessage):
                    if root.foldable and not root.process_hidden:
                        root.collapsed = collapsed
                    continue
                groups = [root] if isinstance(root, ToolGroup) else []
                groups.extend(root.query(ToolGroup))
                for group in groups:
                    if not group.collapse_locked and not group.process_hidden:
                        group.collapsed = collapsed
        # Mounted is not laid out: the batch gets its geometry on the next refresh.
        if not self._host.call_after_refresh(self._laid_out, batch):
            self._place(batch)

    def _laid_out(self, batch: list[Widget]) -> None:
        self._place(batch)
        target = self._pending_jump
        if target is not None and any(widget is target for widget in batch):
            self._pending_jump = None
            if target.id is not None:
                self._host.scroll_to_turn(target.id)

    def _place(self, batch: list[Widget]) -> None:
        placed = set(batch)
        self._unplaced = [widget for widget in self._unplaced if widget not in placed]
        self._end_view_hold_once_laid_out()

    def _reset_prepend_state(self) -> None:
        self._task = None
        self._pending = []
        self._fold_collapsed = None
        # A jump into a batch still waiting for its layout outlives the prepend.
        if not any(widget is self._pending_jump for widget in self._unplaced):
            self._pending_jump = None
        self._end_view_hold_once_laid_out()

    def _end_view_hold_once_laid_out(self) -> None:
        # The last batch lands above the view when it lays out, after the prepend ended.
        if self._task is None and not self._unplaced:
            self._host.end_view_hold()

    def _claim_gc_pause(self) -> ChatGcPauseClaim:
        gc_pause = ChatGcPauseClaim()
        gc_pause.claim()
        self._gc_pauses.add(gc_pause)
        return gc_pause

    def _release_gc_pause(self, gc_pause: ChatGcPauseClaim, *, collect_first: bool) -> None:
        self._gc_pauses.discard(gc_pause)
        gc_pause.release(collect_first=collect_first)

    @staticmethod
    def _batch_start(
        widgets: list[Widget],
        end: int,
        *,
        row_budget: int,
        min_entries: int,
        width: int,
        max_entries: int = REPLAY_MOUNT_BATCH_SIZE,
        max_visible_entries: int = REPLAY_MOUNT_BATCH_SIZE,
    ) -> int:
        """First index of the entries before ``end`` that fill ``row_budget`` rows."""
        start = end
        rows = 0
        visible_entries = 0
        while (
            start > 0
            and end - start < max_entries
            and visible_entries < max_visible_entries
            and (rows < row_budget or end - start < min_entries)
        ):
            start -= 1
            visible_entries += int(widgets[start].display)
            rows += estimated_rows(widgets[start], width)
        return start
