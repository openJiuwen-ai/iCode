# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tail-first replay mounting for the chat transcript.

A restored transcript mounts its newest entries (about three viewports)
before replay returns, so the restore dialog closes as soon as the part the
user sees is ready. A panel-owned task then prepends the older entries above
them, newest batch first, in bounded batches.

Windowed replay uses the same foreground mount for the tail. Older roots
are mounted in document order within the requested window, with previously
visited roots left attached. A newer viewport replaces queued work at the
next batch boundary.

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
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

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

REPLAY_FOREGROUND_ENTRY_LIMIT = 4 * REPLAY_MOUNT_BATCH_SIZE
"""Most transcript roots that may be mounted before restore returns."""

_TAIL_VIEWPORTS = 3
_MIN_VIEWPORT_ROWS = 40
_MIN_TAIL_ENTRIES = 2
_PREPEND_BATCH_ROWS = 300
_MIN_WRAP_WIDTH = 20
_ENTRY_CHROME_ROWS = 3


@dataclass(frozen=True, slots=True)
class _WindowRequest:
    remaining: deque[Widget]
    ready: Callable[[], None]
    failed: Callable[[], None] | None


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
    """Mount replayed entries tail first, then fill history eagerly or by window."""

    def __init__(self, host: ReplayMountHost) -> None:
        self._host = host
        self._pending: list[Widget] = []
        self._unplaced: list[Widget] = []
        """Prepended entries from registration until the refresh after their batch mounted: children without geometry."""
        self._task: asyncio.Task[Exception | None] | None = None
        self._foreground_mounts: set[asyncio.Future[None]] = set()
        self._stop_requested = False
        self._stopping = False
        self._gc_pauses: set[ChatGcPauseClaim] = set()
        self._fold_collapsed: bool | None = None
        self._fold_generation = 0
        self._pending_jump: UserMessage | None = None
        """The turn a held jump lands on once its batch lays out."""
        self._windowed = False
        self._cold: dict[Widget, int] = {}
        """Unmounted archived roots and their original document positions."""
        self._mounting: set[Widget] = set()
        self._window_request: _WindowRequest | None = None
        self._drain_waiters: dict[asyncio.Future[Exception | None], deque[Widget] | None] = {}

    @property
    def in_progress(self) -> bool:
        """Whether the panel is currently mounting older entries."""
        return self._task is not None

    @property
    def windowed(self) -> bool:
        """Whether older entries are mounted on demand."""
        return self._windowed

    @property
    def has_pending(self) -> bool:
        """Whether any replay roots are still cold or mounting."""
        return bool(self._cold or self._pending or self._task is not None)

    @property
    def can_start_windowed(self) -> bool:
        """Whether a new replay can own the archive and mount only its tail."""
        return not (self._stopping or self._windowed or self._task is not None or self._foreground_mounts)

    def pending_widgets(self) -> list[Widget]:
        """Entries not mounted yet, in document order."""
        if self._windowed:
            return [widget for widget in self._cold if widget not in self._mounting or not widget.is_attached]
        return list(self._pending)

    def last_pending_entry(self) -> Widget | None:
        """Return the newest pending entry in document order."""
        if self._windowed:
            return next(reversed(self._cold), None)
        return self._pending[-1] if self._pending else None

    @property
    def unregistered_cold_count(self) -> int:
        """Cold roots still absent from the transcript's children."""
        # Roots in the active batch can already be in the DOM while their
        # descendants initialize. Only that bounded batch needs inspection.
        return len(self._cold) - sum(widget.is_attached for widget in self._mounting)

    def needs_mount(self, widgets: Sequence[Widget]) -> bool:
        """Whether one of the requested roots is still cold or mounting."""
        return any(widget in self._cold for widget in widgets)

    def has_cold_entry(self, predicate: Callable[[Widget], bool]) -> bool:
        """Check pending and still-initializing roots without copying the archive."""
        return any(predicate(widget) for widget in self._cold)

    def request_window(
        self, widgets: Sequence[Widget], ready: Callable[[], None], *, failed: Callable[[], None] | None = None
    ) -> None:
        """Mount the requested roots and notify the latest request when they are initialized."""
        if self._windowed and not self._stop_requested:
            self._window_request = _WindowRequest(self._ordered_cold(widgets), ready, failed)
            self._start_window_task()

    def _ordered_cold(self, widgets: Sequence[Widget]) -> deque[Widget]:
        needed = {widget for widget in widgets if widget in self._cold}
        return deque(sorted(needed, key=self._cold.__getitem__, reverse=True))

    def cancel_window_request(self) -> None:
        """Withdraw a request while allowing its active batch to finish safely."""
        self._window_request = None

    def _start_window_task(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._mount_windows(),
                name="chat-replay-window",
                # The worker must be registered before it can finish, even
                # under the production Textual eager task factory.
                eager_start=False,
            )

    async def wait_complete(self, *, widgets: Sequence[Widget] | None = None) -> None:
        """Mount the requested history, or the entire archive when no roots are specified."""
        failure = None
        if self._windowed and self._cold and not self._stop_requested:
            remaining = None if widgets is None else self._ordered_cold(widgets)
            if remaining is not None and not remaining:
                return
            waiter = asyncio.get_running_loop().create_future()
            self._drain_waiters[waiter] = remaining
            self._start_window_task()
            try:
                failure = await waiter
            finally:
                self._drain_waiters.pop(waiter, None)
        elif (widgets is None or not self._windowed) and (task := self._task) is not None:
            # Leave the active batch and any other requests intact.
            await asyncio.wait((task,))
            if not task.cancelled():
                failure = task.result()
        if failure is not None:
            entries = "all archived transcript entries" if widgets is None else "selected archived transcript entries"
            raise RuntimeError(f"Could not mount {entries}") from failure

    async def mount(
        self, widgets: list[Widget], *, windowed_tail_start: int | None = None, windowed_prefix_count: int = 0
    ) -> bool:
        """Mount the newest widgets and report whether this replay installed a cold archive."""
        if self._stopping:
            return False
        if not self.can_start_windowed:
            # Planned after an unfinished prepend, so newer than all of it:
            # these append below the mounted entries in the foreground.
            await self._mount_foreground(widgets)
            return False
        self._stop_requested = False
        if windowed_tail_start is not None:
            return await self._mount_windowed_tail(widgets, windowed_prefix_count, windowed_tail_start)
        viewport = self._host.size
        tail_start = self._batch_start(
            widgets,
            len(widgets),
            row_budget=_TAIL_VIEWPORTS * max(_MIN_VIEWPORT_ROWS, viewport.height),
            min_entries=_MIN_TAIL_ENTRIES,
            width=viewport.width,
            max_entries=REPLAY_FOREGROUND_ENTRY_LIMIT,
            max_visible_entries=REPLAY_MOUNT_BATCH_SIZE,
        )
        if tail_start == 0:
            await self._mount_foreground(widgets)
            return False
        self._host.stamp_transcript_order(widgets)
        gc_pause = self._claim_gc_pause()
        handed_off = False
        mounted = False
        try:
            mounted = await self._mount_batches(widgets[tail_start:], gc_pause)
            if mounted:
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
        return False

    async def _mount_windowed_tail(self, widgets: list[Widget], prefix_count: int, tail_start: int) -> bool:
        if not 0 <= prefix_count <= tail_start < len(widgets):
            raise ValueError("Windowed replay requires a valid prefix and an initial tail")
        self._host.stamp_transcript_order(widgets)
        if not await self._mount_foreground([*widgets[:prefix_count], *widgets[tail_start:]]):
            return False
        self._windowed = True
        self._cold = {
            widget: index for index, widget in enumerate(widgets[prefix_count:tail_start], start=prefix_count)
        }
        return True

    def request_stop(self) -> None:
        """Prevent another batch from starting while a clear waits for the replay lock."""
        self._stop_requested = True
        self.forget_jump()
        self.cancel_window_request()

    async def stop(self) -> None:
        """End replay at its next batch boundary and wait for the active mounts.

        A jump waiting for the next refresh is dropped too, even once the prepend
        has ended: the transcript that follows can reuse its turn ids.
        """
        self._stopping = True
        try:
            self.request_stop()
            foreground = tuple(self._foreground_mounts)
            task = self._task
            if task is not None:
                await asyncio.wait((task,))
            if foreground:
                await asyncio.wait(foreground)
            self._reset_window_state()
            # The caller removes the transcript; a batch still waiting for its layout goes with it.
            self._unplaced = []
            self._end_view_hold_once_laid_out()
        finally:
            self._stopping = False

    def abandon(self) -> None:
        """Cancel the prepend, drop its jumps and release its GC pause at once (panel unmount)."""
        task = self._task
        self._stop_requested = True
        self.forget_jump()
        self._unplaced = []
        self._reset_prepend_state()
        self._reset_window_state()
        for gc_pause in tuple(self._gc_pauses):
            self._release_gc_pause(gc_pause, collect_first=False)
        if task is not None and not task.done():
            task.cancel()

    def pending_folds_collapsed(self) -> bool | None:
        """Collapsed state pending groups, thinking and summaries will mount with; None without any."""
        if not any(
            (isinstance(widget, ContextFoldWidget) and widget.has_summary())
            or ((isinstance(widget, ToolGroup) or folds_with_turn(widget)) and not widget.process_hidden)
            for widget in self.pending_widgets()
        ):
            return None
        # Replay builds tool groups, thinking and compaction summaries collapsed.
        return True if self._fold_collapsed is None else self._fold_collapsed

    def fold_choice(self) -> bool | None:
        """Return the fold-all choice for a continuation whose prompt is still prepending."""
        # Windowed replay mounts the whole latest turn up front. Its fold
        # choice belongs to archived pages, which can stay cold indefinitely.
        return None if self._windowed else self._fold_collapsed

    @property
    def expanding_replay(self) -> bool:
        return self._fold_collapsed is False

    @property
    def fold_generation(self) -> int:
        return self._fold_generation

    def set_fold_state(self, collapsed: bool) -> None:
        """Apply a fold-all choice to groups, thinking and summaries that mount after it."""
        # The whole prepend, not just ``pending``: the last batch has left it while it mounts.
        if self.has_pending and self._fold_collapsed != collapsed:
            self._fold_collapsed = collapsed
            self._fold_generation += 1

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

    async def _mount_foreground(self, widgets: list[Widget]) -> bool:
        gc_pause = self._claim_gc_pause()
        mounted = False
        try:
            mounted = await self._mount_batches(widgets, gc_pause)
            return mounted
        finally:
            self._release_gc_pause(gc_pause, collect_first=mounted)

    async def _mount_batches(self, widgets: list[Widget], gc_pause: ChatGcPauseClaim) -> bool:
        finished = asyncio.get_running_loop().create_future()
        self._foreground_mounts.add(finished)
        try:
            total = len(widgets)
            self._host.report_replay_progress(0, total)
            await asyncio.sleep(0)
            for start in range(0, total, REPLAY_MOUNT_BATCH_SIZE):
                if self._stop_requested or not self._host.can_mount_replay():
                    return False
                batch = widgets[start : start + REPLAY_MOUNT_BATCH_SIZE]
                await self._host.mount_replay_batch(batch, before=None)
                done = start + len(batch)
                if done < total:
                    gc_pause.collect_young()
                self._host.report_replay_progress(done, total)
                # Give the loading overlay and input timers a chance to paint between batches.
                await asyncio.sleep(0)
            return not self._stop_requested and self._host.can_mount_replay()
        finally:
            self._foreground_mounts.remove(finished)
            finished.set_result(None)

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

    def _next_window_batch(self) -> list[Widget]:
        for waiter, remaining in self._drain_waiters.items():
            if remaining is not None and not waiter.done():
                self._prune_mounted(remaining)
                if not remaining:
                    waiter.set_result(None)
        request = self._window_request
        if request is not None:
            if batch := self._requested_batch(request.remaining):
                return batch
            self._window_request = None
            request.ready()
            if self._window_request is not None:
                return []
        for waiter, remaining in self._drain_waiters.items():
            if remaining is not None and not waiter.done() and (batch := self._requested_batch(remaining)):
                return batch
        if any(remaining is None and not waiter.done() for waiter, remaining in self._drain_waiters.items()):
            return self._contiguous_batch(reversed(self._cold))
        return []

    def _prune_mounted(self, remaining: deque[Widget]) -> None:
        # An in-flight batch can have satisfied part of this request.
        while remaining and remaining[0] not in self._cold:
            remaining.popleft()

    def _requested_batch(self, remaining: deque[Widget]) -> list[Widget]:
        self._prune_mounted(remaining)
        # Keep the in-flight roots in the request until their outcome is known.
        return self._contiguous_batch(remaining)

    def _contiguous_batch(self, newest_first: Iterable[Widget]) -> list[Widget]:
        """Take a contiguous run so mounting before one anchor preserves order."""
        batch: list[Widget] = []
        for widget in newest_first:
            index = self._cold.get(widget)
            if index is None or (batch and self._cold[batch[-1]] - index != 1):
                break
            batch.append(widget)
            if len(batch) == REPLAY_MOUNT_BATCH_SIZE:
                break
        batch.reverse()
        return batch

    async def _mount_windows(self) -> Exception | None:
        gc_pause = self._claim_gc_pause()
        failure = None
        try:
            while not self._stop_requested and self._host.can_mount_replay():
                batch = self._next_window_batch()
                if not batch:
                    # A ready callback can request another window.
                    if self._window_request is not None:
                        continue
                    return None
                try:
                    await self._mount_window_batch(batch)
                except Exception as exc:
                    self._fail_window_batch(batch, exc)
                    if self._window_request is None and all(waiter.done() for waiter in self._drain_waiters):
                        return exc
                    continue
                if self._cold and any(not waiter.done() for waiter in self._drain_waiters):
                    gc_pause.collect_young()
                await asyncio.sleep(0)
        except Exception as exc:
            failure = exc
            request = self._window_request
            self.cancel_window_request()
            logger.exception("Mounting an archived transcript window failed")
            if request is not None and request.failed is not None:
                request.failed()
            return exc
        finally:
            if self._task is asyncio.current_task():
                self._task = None
                self._finish_drain_waiters(failure)
                if not self._cold:
                    self._fold_collapsed = None
            self._release_gc_pause(gc_pause, collect_first=False)
        return None

    def _fail_window_batch(self, batch: Sequence[Widget], failure: Exception) -> None:
        logger.exception("Mounting an archived transcript batch failed")
        roots = set(batch)
        for waiter, remaining in self._drain_waiters.items():
            if not waiter.done() and (remaining is None or not roots.isdisjoint(remaining)):
                waiter.set_result(failure)
        request = self._window_request
        if request is not None and not roots.isdisjoint(request.remaining):
            self.cancel_window_request()
            if request.failed is not None:
                request.failed()

    def _finish_drain_waiters(self, failure: Exception | None = None) -> None:
        for waiter in self._drain_waiters:
            if not waiter.done():
                waiter.set_result(failure)
        self._drain_waiters.clear()

    async def _mount_window_batch(self, batch: list[Widget]) -> None:
        self._mounting.update(batch)
        try:
            await self._host.mount_replay_batch(batch, before=self._host.transcript_entry_after(batch[-1]))
            self._apply_fold_state(batch)
            await self.wait_for_visible_content(batch)
        except Exception:
            # Preserve unregistered roots for raw transcript reads after failure.
            for widget in batch:
                if widget.is_attached:
                    self._cold.pop(widget, None)
            raise
        else:
            for widget in batch:
                self._cold.pop(widget, None)
        finally:
            self._mounting.difference_update(batch)

    async def wait_for_visible_content(self, widgets: Sequence[Widget]) -> None:
        """Wait until visible content reflects the latest replay fold state."""
        while True:
            fold_generation = self._fold_generation
            await self._wait_for_visible_archive_content(widgets)
            # Fold-all may reopen an earlier root while this pass waits
            # for a later one. Check the reopened content before publishing.
            if self._stop_requested or fold_generation == self._fold_generation:
                return

    @staticmethod
    async def _wait_for_visible_archive_content(batch: Sequence[Widget]) -> None:
        # Opening a restored fold creates descendants after the initial
        # mount barrier. Hold this batch out of layout until those are ready.
        nodes = list(reversed(batch))
        while nodes:
            node = nodes.pop()
            if not node.is_attached or not node.display or not node.visible:
                continue
            if isinstance(node, StreamedMarkdownMessage):
                await node.wait_for_replay_body()
            elif isinstance(node, ToolGroup):
                await node.wait_for_replay_content()
            else:
                nodes.extend(reversed(node.children))

    def _reset_window_state(self) -> None:
        self._windowed = False
        self._cold.clear()
        self._mounting.clear()
        self._finish_drain_waiters()
        self._fold_collapsed = None
        self.cancel_window_request()

    def _after_batch_mounted(self, batch: list[Widget]) -> None:
        self._apply_fold_state(batch)
        # Mounted is not laid out: the batch gets its geometry on the next refresh.
        if not self._host.call_after_refresh(self._laid_out, batch):
            self._place(batch)

    def _apply_fold_state(self, batch: list[Widget]) -> None:
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
