# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scroll-follow and Textual anchor policy for chat transcripts."""

from __future__ import annotations

import gc
from typing import Any

from textual._arrange import DockArrangeResult
from textual.geometry import Offset, Region, Size, Spacing
from textual.timer import Timer
from textual.widget import Widget

from chrys.app.tui.util.visibility import set_widget_visibility_without_layout
from chrys.app.tui.widgets.chat.ports import ChatScrollHost

_MANUAL_SCROLL_GC_RESUME_SECONDS = 0.25
_SCROLL_GC_PAUSE_OWNERS = 0
_SCROLL_GC_WAS_ENABLED = False


def scroll_gc_paused() -> bool:
    """Return whether any chat panel currently owns the shared GC pause."""
    return _SCROLL_GC_PAUSE_OWNERS > 0


def _claim_scroll_gc_pause() -> None:
    """Disable cyclic GC while at least one chat panel scrolls or replays."""
    global _SCROLL_GC_PAUSE_OWNERS, _SCROLL_GC_WAS_ENABLED
    if _SCROLL_GC_PAUSE_OWNERS == 0:
        _SCROLL_GC_WAS_ENABLED = gc.isenabled()
        if _SCROLL_GC_WAS_ENABLED:
            gc.disable()
    _SCROLL_GC_PAUSE_OWNERS += 1


def _release_scroll_gc_pause(*, collect_first: bool = False) -> None:
    """Restore cyclic GC when the last chat-panel pause ends.

    ``collect_first`` runs one full collection before this owner lets go, but
    only when the pause is hiding an enabled collector. An owner that
    allocated a whole transcript would otherwise hand that backlog to the
    first automatic passes after the pause (or to another owner's resume),
    and the survivors would be traversed again by later gen1/gen2 passes; one
    full pass moves them to the oldest generation at once.
    """
    global _SCROLL_GC_PAUSE_OWNERS, _SCROLL_GC_WAS_ENABLED
    if _SCROLL_GC_PAUSE_OWNERS <= 0:
        return
    if collect_first and _SCROLL_GC_WAS_ENABLED:
        gc.collect()
    _SCROLL_GC_PAUSE_OWNERS -= 1
    if _SCROLL_GC_PAUSE_OWNERS == 0:
        was_enabled = _SCROLL_GC_WAS_ENABLED
        _SCROLL_GC_WAS_ENABLED = False
        if was_enabled:
            gc.enable()


class ChatGcPauseClaim:
    """One owner's idempotent hold on the shared chat-panel GC pause."""

    __slots__ = ("_held",)

    def __init__(self) -> None:
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    def claim(self) -> None:
        """Join the shared pause; a claim already held stays one owner."""
        if not self._held:
            _claim_scroll_gc_pause()
            self._held = True

    def collect_young(self) -> None:
        """Free the cycles allocated since the last pass, keeping older objects.

        Only while this claim hides an enabled collector. A long pause would
        otherwise keep every cycle it allocates: the heap grows by that
        garbage, and the survivors stay scattered between its blocks once it
        is freed, which slows every later full pass. A young pass costs only
        what was allocated since the previous one.
        """
        if self._held and _SCROLL_GC_WAS_ENABLED:
            gc.collect(0)

    def release(self, *, collect_first: bool = False) -> None:
        """Leave the shared pause once, however many exit paths call this."""
        if self._held:
            self._held = False
            _release_scroll_gc_pause(collect_first=collect_first)


class ManualScrollGcGuard:
    """Gesture-scoped GC pause for transcript surfaces without follow state."""

    def __init__(self, host: Widget) -> None:
        self._host = host
        self._timer: Timer | None = None
        self._paused = False
        self._scrollbar_grabbed = False

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """Hold cyclic GC until the current scroll cadence settles."""
        if not self._paused:
            _claim_scroll_gc_pause()
            self._paused = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if not self._scrollbar_grabbed:
            self._timer = self._host.set_timer(_MANUAL_SCROLL_GC_RESUME_SECONDS, self.resume)

    def set_scrollbar_grabbed(self, grabbed: bool) -> None:
        """Hold the pause continuously across a scrollbar-thumb drag."""
        if grabbed:
            self._scrollbar_grabbed = True
            self.pause()
            return
        if not self._scrollbar_grabbed:
            return
        self._scrollbar_grabbed = False
        if self._paused:
            self.pause()

    def resume(self, *, schedule_collect: bool = True) -> None:
        """Release the shared pause after a quiet cadence interval."""
        self._timer = None
        if self._scrollbar_grabbed or not self._paused:
            return
        self._paused = False
        _release_scroll_gc_pause()
        if schedule_collect and gc.isenabled():
            gc.collect(0)

    def stop(self) -> None:
        """Release timers and shared ownership during surface teardown."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self._scrollbar_grabbed = False
        self.resume(schedule_collect=False)


class ChatScrollController:
    """Own chat scroll-follow state, timers, and bottom-anchor policy."""

    def __init__(self, host: ChatScrollHost) -> None:
        self._host = host
        self.held_shrink_height: int = -1
        self.manual_scroll_gc_timer: Timer | None = None
        self.manual_scroll_gc_paused: bool = False
        self.scrollbar_grabbed: bool = False
        self.auto_scroll_paused_by_user: bool = False
        self.final_response_started: bool = False
        self.programmatic_scroll: bool = False
        self.anchor_sync_scheduled: bool = False
        self.agent_running: bool = False
        self.view_hold_active: bool = False
        self._view_hold_child: tuple[Widget, int] | None = None
        self._view_hold_arrangement: DockArrangeResult | None = None
        self._view_hold_rows = 0
        """How far the view hold moved the view since the last layout's size update: rows landed above it.

        A geometry read can arrange before that layout, which then gets the same arrangement again.
        """
        self._settle_reanchor_generation = 0
        """Advanced by a TOC jump or a new message's reveal: a re-pin or replay end queued before it does nothing."""
        self._spacer_rows = 0
        """Rows the bottom spacer took in the last arrange: 0 when it was hidden or left out."""
        self._entries_bottom = 0
        """Where the last arrange ended the last transcript entry: the chrome below it (the waiting row and the spacer)
        adds no reply."""
        self._sized_entries_bottom = 0
        """``_entries_bottom`` as the last layout's size update had it."""
        self._unwatched_virtual_size: Size | None = None
        """The virtual size the watcher last saw, while a scroll-only reflow applied a newer one without it."""
        self._arrangement: DockArrangeResult | None = None
        """The last arrangement that placed the transcript."""
        self._reveal: Widget | None = None
        """A new user message to bring into view once a layout places it."""
        self._reveal_top = -1
        """Where that layout put the top of ``_reveal``, margin included; -1 until one does."""
        self._fold_shrink = False
        """An entry folded, or gave rows up, since the last layout: the transcript shrinks in that layout."""

    def entry_folded(self) -> None:
        """Show an entry that folded, or gave rows up, in the layout that places it.

        A view below the transcript's new bottom comes up to it in the arrangement that places the shrink
        (:meth:`_show_fold_shrink`), and the size update after it holds no frame (:meth:`size_updated`): a held frame
        would show the rows blank under the last line. That goes for rows a streamed answer may take again as more
        text arrives, too: until it does, they would stand blank.
        """
        self._fold_shrink = True

    def set_agent_running(self, running: bool) -> None:
        """Mirror current run state and reset the final gate only on run start."""
        if running and not self.agent_running:
            self.final_response_started = False
        self.agent_running = running

    def on_user_turn_started(self) -> None:
        """Reset live-turn scroll gates."""
        self.auto_scroll_paused_by_user = False
        self.final_response_started = False

    def on_replay_started(self) -> None:
        """Reset replay scroll gates after welcome dismissal."""
        self.auto_scroll_paused_by_user = False
        self.final_response_started = False

    def scroll_user_message_to_top(self, widget: Widget) -> None:
        """Scroll a newly mounted user message to the viewport top, as far as the content allows.

        The layout that places the message decides it: :meth:`arrange` reads
        where the message landed and :meth:`size_updated` scrolls there once
        that layout's height is in, never a callback. Textual runs
        ``call_after_refresh`` callbacks whenever the screen idles while a
        ``batch_update`` (a footer recompose as a run starts) holds layout
        back, so a deferred ``scroll_visible`` could find the message
        unplaced and do nothing; and when it replaced a failed turn, the
        content did not grow enough to bring a scrolled-up view down to it.
        """
        self._reveal = widget
        self._reveal_top = -1
        # ``outer_size`` is set by a layout only: ``size`` reads the region, which a
        # lazily rebuilt widget map gives before any layout applies the new height.
        if widget.outer_size and self._arrangement is not None:
            # A layout placed it while it mounted.
            self._place_reveal(self._arrangement)
            # A compositor resync for a visibility flip (the bottom-jump button a scroll
            # shows) sizes the widgets it adds, not the transcript they grew: until a
            # layout's size update takes that height in, the reveal would stop at the old
            # bottom. ``size_updated`` reveals once it does.
            if self._reveal_top >= 0 and self._sized_entries_bottom == self._entries_bottom:
                self._reveal_placed()

    def _place_reveal(self, result: DockArrangeResult) -> None:
        widget = self._reveal
        if widget is None:
            return
        if not widget.is_attached:
            self._reveal = None
            return
        placements = result.placements
        for index in range(len(placements) - 1, -1, -1):
            placement = placements[index]
            if placement.widget is widget:
                self._reveal_top = placement.region.y - placement.margin.top
                return

    def _reveal_placed(self) -> None:
        """Put the message's top at the viewport top, or follow the bottom when it is there.

        As ``scroll_to`` and Textual's anchor check do: a message the content
        lets reach the top holds the view there, until the reply's growth
        resumes the following; one that ends the content follows the bottom.
        """
        host = self._host
        # A scroll animation still running would carry the view away again. Stopping
        # it puts the view at the animation's end, which is no scroll of the user's,
        # and comes before the anchor is decided: an end at the bottom re-engages it.
        programmatic = self.programmatic_scroll
        self.programmatic_scroll = True
        try:
            host.stop_scroll_y_animation()
        finally:
            self.programmatic_scroll = programmatic
        max_y = host.max_scroll_y
        y = min(self._reveal_top, max_y)
        self._reveal = None
        self._reveal_top = -1
        if y < max_y:
            # A settle re-pin queued by the shrink before it (a failed turn's
            # cleanup) would take the view on to the bottom.
            self._settle_reanchor_generation += 1
        if host.is_anchored:
            host.set_anchor_released(y < max_y)
        if host.scroll_y != y:
            self.set_scroll_y_programmatically(y)
        self.sync_scroll_to_bottom_button()

    def on_final_response_started(self) -> None:
        """Resume bottom-follow once when the agent starts its final answer."""
        if self.final_response_started:
            return
        self.final_response_started = True
        self.auto_scroll_paused_by_user = False
        if self._host.is_anchored:
            self._host.set_anchor_released(False)
        self.schedule_anchor_sync()

    def on_status_message_mounted(self) -> None:
        """Force a mounted status message to the bottom."""
        self.auto_scroll_paused_by_user = False
        if self._host.is_anchored:
            self._host.set_anchor_released(False)
        self._host.scroll_end(animate=False)

    def scroll_inline_prompt_to_top_after_refresh(self, widget: Widget) -> None:
        """Schedule inline ask_user prompt scroll-to-top after refresh."""
        self.auto_scroll_paused_by_user = False
        self._host.call_after_refresh(self._host.scroll_to_widget_top, widget)

    def after_replay_history_mounted(self) -> None:
        """Schedule final replay scroll-to-bottom, unless a TOC jump or a new message's reveal lands first."""
        self._host.call_after_refresh(self._scroll_end_unless_jumped, self._settle_reanchor_generation)

    def _scroll_end_unless_jumped(self, generation: int) -> None:
        if generation == self._settle_reanchor_generation:
            # Laid out by now: a deferred scroll would wait for another refresh, which a jump can land before.
            self._host.scroll_end(animate=False, immediate=True)

    def scroll_to_region(self, region: Region, **kwargs: Any) -> Offset:
        """Ignore focus-restoration center scrolls inside the transcript."""
        if kwargs.get("center") and not kwargs.get("top"):
            on_complete = kwargs.get("on_complete")
            if callable(on_complete):
                self._host.call_after_refresh(on_complete)
            return Offset()
        return self._host.call_super_scroll_to_region(region, **kwargs)

    def check_anchor(self) -> None:
        """Re-engage bottom-follow only at exact bottom."""
        if (
            self._host.is_anchored
            and self._host.is_anchor_released()
            and self._host.scroll_y >= self._host.max_scroll_y
        ):
            self._host.set_anchor_released(False)
            self.auto_scroll_paused_by_user = False

    def arrange(self, size: Size, *, optimal: bool = False) -> DockArrangeResult:
        """Defuse Textual anchor pins while chat layout is settling."""
        if self._host.is_anchored and not self._host.is_anchor_released():
            new_container_h = size.height + self._host.scrollbar_size_horizontal
            container_size = self._host.get_container_size()
            if container_size.height != new_container_h:
                self._host.set_container_size(Size(container_size.width, new_container_h))
        result = self._host.call_super_arrange(size, optimal=optimal)
        self._host.place_fixed_scroll_button(result, size)
        if not optimal:
            self._arrangement = result
            self._spacer_rows = self._arrange_bottom_spacer(result, size)
            self._drop_bottom_margin(result)
            self._entries_bottom = self._last_entry_bottom(result)
            if self._reveal is not None:
                self._place_reveal(result)
        virtual_h = result.spatial_map.total_region.bottom
        if virtual_h < size.height and self._host.scroll_y < 0:
            if self._host.is_anchored and not self._host.is_anchor_released():
                self._host.set_anchor_released(True)
            self._host.set_scroll_y_reactive(0.0)
            self._host.set_scroll_target_y_reactive(0.0)
        elif (
            virtual_h < self._host.virtual_size.height
            and self._host.is_anchored
            and not self._host.is_anchor_released()
        ):
            self._host.set_anchor_released(True)
            self._host.call_after_refresh(self._reanchor_after_settle_unless_jumped, self._settle_reanchor_generation)
        if not optimal and self._fold_shrink:
            self._show_fold_shrink(virtual_h - size.height)
        if not optimal and (self.view_hold_active or self._view_hold_child is not None):
            self._hold_view(result)
        return result

    def _show_fold_shrink(self, max_scroll_y: int) -> None:
        """Bring a view below the transcript's new bottom, *max_scroll_y*, up to it in the arrangement a fold shrank.

        The compositor places the transcript by the scroll offset it has as this arrangement returns: clamped only in
        the size update after it, as Textual does, the offset would paint one frame with the content below the fold
        moved up, and the view at the new bottom only in the next.
        """
        scroll_y = max(0, max_scroll_y)
        if self._host.scroll_y > scroll_y:
            self._host.set_scroll_y_reactive(scroll_y)
            self._host.set_scroll_target_y_reactive(scroll_y)
            self._host.set_vertical_scrollbar_position(scroll_y)

    def _arrange_bottom_spacer(self, result: DockArrangeResult, size: Size) -> int:
        """Return the rows the bottom spacer takes, leaving it out once the content fills the viewport.

        Textual gives ``1fr`` one row when no space remains, so content that
        grows to the viewport height would lay out one row past it until
        :meth:`watch_virtual_size` hides the spacer: for that frame a scrollbar
        shows, and a following view scrolls by the row and back.
        """
        spacer = self._host.bottom_spacer()
        if spacer is None or not spacer.display:
            return 0
        placements = result.placements
        for index in range(len(placements) - 1, -1, -1):
            placement = placements[index]
            if placement.widget is spacer:
                if placement.region.y < size.height:
                    return placement.region.height
                del placements[index]
                result.widgets.discard(spacer)
                result._spatial_map = None
                return 0
            if not placement.fixed:
                # The spacer ends the transcript: this arrangement left it out already.
                break
        return 0

    def _last_entry_bottom(self, result: DockArrangeResult) -> int:
        """Where *result* ends the last transcript entry."""
        for placement in reversed(result.placements):
            if not placement.fixed and not self._host.is_infrastructure(placement.widget):
                return placement.region.bottom
        return 0

    def _drop_bottom_margin(self, result: DockArrangeResult) -> None:
        """Drop the bottom margin of what ends the transcript: its last line sits on the panel's bottom border.

        A margin parts an entry from the one after it; below the last, it would stand blank under the transcript a
        view that follows the bottom shows. The bottom spacer, filling a viewport the transcript is shorter than,
        follows it there instead.

        Runs on every arrangement that places the transcript, a cached one it dropped already included: that one is
        left as it is, so a scroll's arrange does not build its spatial map again.
        """
        spacer = self._host.bottom_spacer()
        placements = result.placements
        for index in range(len(placements) - 1, -1, -1):
            placement = placements[index]
            if placement.fixed:
                continue
            margin = placement.margin
            if placement.widget is not spacer and margin.bottom:
                placements[index] = placement._replace(margin=Spacing(margin.top, margin.right, 0, margin.left))
                result._spatial_map = None
            return

    def begin_view_hold(self) -> None:
        """Keep a scrolled-up view on its content while entries land above it."""
        self.view_hold_active = True

    def end_view_hold(self) -> None:
        """Stop tracking after the next arrange applies the last pending shift."""
        self.view_hold_active = False
        self._view_hold_arrangement = None

    def _hold_view(self, result: DockArrangeResult) -> None:
        """Shift a released view by the height that landed above its first child.

        Textual keeps ``scroll_y`` numeric, so rows inserted above the viewport
        push the content being read down and out of view. Track the child at
        the top of the viewport by its arranged y and, when an arrange moves it,
        move ``scroll_y`` by the same amount before the compositor reads the
        offset (the compositor's own anchor pin writes it the same way). A
        bottom-following view needs nothing: the compositor re-pins it.

        The child is re-picked from the last layout at the current offset: a
        jump or a scroll since that arrange chose its offset in that layout,
        and a view that has not moved picks the same child again. Once the
        hold ends, the child picked last applies the final shift.
        """
        host = self._host
        if not (host.is_anchored and host.is_anchor_released()):
            self._view_hold_child = None
            self._view_hold_arrangement = result if self.view_hold_active else None
            return
        scroll_y = host.scroll_y
        tracked = self._view_hold_child
        if self._view_hold_arrangement is not None:
            tracked = self._first_visible_child(self._view_hold_arrangement, scroll_y)
        if tracked is not None:
            child, old_y = tracked
            for placement in result.placements:
                if placement.widget is child:
                    delta = placement.region.y - old_y
                    if delta:
                        self._view_hold_rows += delta
                        scroll_y = max(0.0, scroll_y + delta)
                        host.set_scroll_y_reactive(scroll_y)
                        host.set_scroll_target_y_reactive(scroll_y)
                        host.set_vertical_scrollbar_position(scroll_y)
                    break
        if not self.view_hold_active:
            self._view_hold_child = None
            self._view_hold_arrangement = None
            return
        self._view_hold_arrangement = result
        self._view_hold_child = self._first_visible_child(result, scroll_y)

    @staticmethod
    def _first_visible_child(result: DockArrangeResult, scroll_y: float) -> tuple[Widget, int] | None:
        for placement in result.placements:
            if not placement.fixed and placement.region.bottom > scroll_y:
                return placement.widget, placement.region.y
        return None

    def note_toc_jump(self) -> None:
        """A TOC jump moved the view; a settle re-pin or a reveal already queued must not move it again."""
        self._settle_reanchor_generation += 1
        self._reveal = None
        self._reveal_top = -1

    def _reanchor_after_settle_unless_jumped(self, generation: int) -> None:
        if generation == self._settle_reanchor_generation:
            self.reanchor_after_settle()

    def reanchor_after_settle(self) -> None:
        """Re-engage the anchor and pin to settled ``max_scroll_y``."""
        if not self._host.is_anchored:
            return
        self._host.set_anchor_released(False)
        new_y = self._host.max_scroll_y
        if self._host.scroll_y != new_y:
            self.set_scroll_y_programmatically(new_y)
        self.sync_scroll_to_bottom_button()

    def set_scroll_y_programmatically(self, y: float) -> None:
        """Set vertical scroll without treating the movement as a user pause."""
        self.programmatic_scroll = True
        try:
            self._host.scroll_y = y
            self._host.set_scroll_target_y(y)
        finally:
            self.programmatic_scroll = False

    def jump_to_bottom(self) -> None:
        """Scroll to the current bottom and resume bottom-follow."""
        self.auto_scroll_paused_by_user = False
        if self._host.is_anchored:
            self._host.set_anchor_released(False)
        self.set_scroll_y_programmatically(self._host.max_scroll_y)
        self.schedule_anchor_sync()
        self.sync_scroll_to_bottom_button()

    def on_resize(self) -> None:
        """Refresh the bottom-jump affordance when viewport height changes."""
        self._host.call_after_refresh(self.sync_scroll_to_bottom_button)

    def size_updated(self, size: Size, virtual_size: Size, container_size: Size, layout: bool = True) -> bool:
        """Hold one anchored shrink frame before validator clamping, unless a fold made it."""
        fold_shrink = self._fold_shrink
        if layout:
            self._fold_shrink = False
        if (
            not fold_shrink
            and self._host.is_anchored
            and 0 < virtual_size.height < self._host.virtual_size.height
            and self.held_shrink_height != virtual_size.height
        ):
            self.held_shrink_height = virtual_size.height
            changed = self._host.apply_held_shrink_size_update(size, container_size)
            self._host.call_after_refresh(self.release_size_hold)
            return changed
        self.held_shrink_height = -1
        # Textual clamps the scroll offset to the new size in this call: content
        # that shrank moved the view, not the user.
        programmatic = self.programmatic_scroll
        self.programmatic_scroll = True
        try:
            # Textual would set a changed virtual size as a reactive, whose refresh
            # lays the whole transcript out a second time; the layout calling this
            # placed it already (a scrollbar that appears asks for its own), so only
            # the watcher runs.
            old_virtual_size = self._host.virtual_size
            changed = self._host.call_super_size_updated(size, virtual_size, container_size, False)
            if layout:
                # A scroll-only reflow (a geometry read) applies a new size without the watcher, as Textual does, and
                # the layout after it gets the same size: the watcher still sees the change, from the size it last saw.
                if self._unwatched_virtual_size is not None:
                    old_virtual_size, self._unwatched_virtual_size = self._unwatched_virtual_size, None
                if self._host.virtual_size != old_virtual_size:
                    self.watch_virtual_size(old_virtual_size, self._host.virtual_size)
                elif self._resume_following_reply(self._host.virtual_size.height):
                    # The entries took rows the chrome below them gave back (a compaction in the waiting row's
                    # place): the size stays, but the reply came.
                    self.set_scroll_y_programmatically(self._host.max_scroll_y)
                self._view_hold_rows = 0
                self._sized_entries_bottom = self._entries_bottom
            elif self._unwatched_virtual_size is None and self._host.virtual_size != old_virtual_size:
                self._unwatched_virtual_size = old_virtual_size
            if self._reveal_top >= 0:
                self._reveal_placed()
            return changed
        finally:
            self.programmatic_scroll = programmatic

    def release_size_hold(self) -> None:
        """Force a layout pass that either recovers or accepts the held shrink."""
        if self.held_shrink_height < 0:
            return
        self._host.refresh(layout=True)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Track manual scroll ticks: GC pause, follow-state, bottom button."""
        old_line = round(old_value)
        new_line = round(new_value)
        self._host.call_super_watch_scroll_y(old_value, new_value)
        if self._host.is_anchored and not self._host.is_anchor_released() and new_value >= self._host.max_scroll_y:
            self.auto_scroll_paused_by_user = False
        if (
            self.agent_running
            and not self.programmatic_scroll
            and self._host.is_anchored
            and self._host.is_anchor_released()
            and new_line < old_line
        ):
            self.auto_scroll_paused_by_user = True
        manual_scroll = old_line != new_line and not self.programmatic_scroll
        if manual_scroll:
            self.pause_gc_for_manual_scroll()
        self.sync_scroll_to_bottom_button()

    def sync_scroll_to_bottom_button(self) -> None:
        """Show the bottom-jump affordance only when there is unseen content below.

        Toggles ``visibility``, never ``display``: a display flip marks the
        screen layout-required and invalidates the compositor full map, which
        turns every bottom-boundary crossing during a scroll into an
        O(all widgets) reflow hitch on large transcripts.

        The rule is written directly instead of assigning ``button.visible``:
        Textual declares the ``visibility`` style property ``layout=True``, so
        the setter escalates every flip to that same full reflow. The
        compositor applies visibility at map-build time (``add_widget`` reads
        the current rule), and a flip can only happen because ``scroll_y`` or
        ``max_scroll_y`` changed — both of which already schedule the cheap
        visible-only re-arrange that refreshes map membership. The explicit
        ``refresh()`` covers repaint dirtiness for the button's region.
        """
        button = self._host.scroll_to_bottom_button()
        if button is None:
            return
        visible = self._host.max_scroll_y > 0 and round(self._host.scroll_y) < self._host.max_scroll_y
        set_widget_visibility_without_layout(button, visible)

    def pause_gc_for_manual_scroll(self) -> None:
        """Temporarily disable cyclic GC while user scrolling is active.

        While the scrollbar thumb is grabbed the pause is held open by the
        gesture itself: no resume debounce is armed, so a slow drag with long
        gaps between mouse moves can never re-enable GC (and eat the backlog
        collection) in the middle of the gesture.
        """
        if not self.manual_scroll_gc_paused:
            _claim_scroll_gc_pause()
            self.manual_scroll_gc_paused = True
        if self.manual_scroll_gc_timer is not None:
            self.manual_scroll_gc_timer.stop()
            self.manual_scroll_gc_timer = None
        if self.scrollbar_grabbed:
            return
        self.manual_scroll_gc_timer = self._host.set_timer(
            _MANUAL_SCROLL_GC_RESUME_SECONDS,
            self.resume_gc_after_manual_scroll,
        )

    def on_scrollbar_grab(self) -> None:
        """Hold the GC pause for the whole scrollbar drag gesture."""
        self.scrollbar_grabbed = True
        self.pause_gc_for_manual_scroll()

    def on_scrollbar_release(self) -> None:
        """Return to the cadence debounce once the drag gesture ends.

        Trailing scroll animation frames keep restarting the debounce, so
        resume still happens a quiet interval after the last movement.
        """
        if not self.scrollbar_grabbed:
            return
        self.scrollbar_grabbed = False
        if self.manual_scroll_gc_paused:
            self.pause_gc_for_manual_scroll()

    def resume_gc_after_manual_scroll(self, *, schedule_collect: bool = True) -> None:
        """Restore cyclic GC after manual scrolling has settled.

        Immediately drains the paused-scroll allocation backlog (gen0) at
        this known-quiet moment: the gen0 counter keeps growing while GC is
        disabled, so the next organic trigger would otherwise fire on
        whatever allocation comes first — historically the middle of the
        next drag movement.  Skipped while another panel still holds the
        pause (GC still disabled) or during teardown paths that pass
        ``schedule_collect=False``.
        """
        self.manual_scroll_gc_timer = None
        if self.scrollbar_grabbed:
            return
        if not self.manual_scroll_gc_paused:
            return
        self.manual_scroll_gc_paused = False
        _release_scroll_gc_pause()
        if schedule_collect and gc.isenabled():
            gc.collect(0)

    def schedule_anchor_sync(self) -> None:
        """Sync scroll position to the bottom after the next refresh."""
        if (
            self._host.is_anchored
            and not self._host.is_anchor_released()
            and not self.auto_scroll_paused_by_user
            and not self.anchor_sync_scheduled
            and self._host.call_after_refresh(self.sync_anchor_bottom)
        ):
            self.anchor_sync_scheduled = True

    def sync_anchor_bottom(self) -> None:
        """Push ``scroll_y`` to the current ``max_scroll_y`` via setter."""
        self.anchor_sync_scheduled = False
        if not (self._host.is_anchored and not self._host.is_anchor_released() and not self.auto_scroll_paused_by_user):
            self.sync_scroll_to_bottom_button()
            return
        new_y = self._host.max_scroll_y
        if self._host.scroll_y != new_y:
            self.set_scroll_y_programmatically(new_y)
        self.sync_scroll_to_bottom_button()

    def _resume_following_reply(self, height: int) -> bool:
        """Take back a view the user message's soft release let go once the reply reaches over two rows below it.

        Only entries growing below are a reply: not rows a replay lands above the view, which the view hold moved
        down with, nor the chrome under the last entry (the waiting row, the spacer).
        """
        if (
            self.agent_running
            and self._entries_bottom - self._view_hold_rows > self._sized_entries_bottom
            and self._host.is_anchored
            and self._host.is_anchor_released()
            and not self.auto_scroll_paused_by_user
            and max(0, height - self._host.size.height) > self._host.scroll_y + 2
        ):
            self._host.set_anchor_released(False)
            return True
        return False

    def watch_virtual_size(self, old: Size, new: Size) -> None:
        """Post-layout hook: re-engage anchor, sync scrollbar, toggle spacer."""
        old_h = old.height
        new_h = new.height
        reanchored = self._resume_following_reply(new_h)
        if (reanchored or new_h > old_h) and self._host.is_anchored and not self._host.is_anchor_released():
            new_y = self._host.max_scroll_y
            if self._host.scroll_y != new_y:
                self.set_scroll_y_programmatically(new_y)
        spacer = self._host.bottom_spacer()
        if spacer is not None:
            viewport_h = self._host.size.height
            if spacer.display:
                if new_h - self._spacer_rows >= viewport_h:
                    spacer.display = False
            elif new_h < viewport_h:
                spacer.display = True
        self.sync_scroll_to_bottom_button()

    def reset_for_clear(self) -> None:
        """Reset transcript scroll state and release the GC pause without collecting."""
        if self.manual_scroll_gc_timer is not None:
            self.manual_scroll_gc_timer.stop()
            self.manual_scroll_gc_timer = None
        self.scrollbar_grabbed = False
        self.resume_gc_after_manual_scroll(schedule_collect=False)
        self.held_shrink_height = -1
        self._arrangement = None
        self._reveal = None
        self._reveal_top = -1
        self._fold_shrink = False
        self._entries_bottom = 0
        self._sized_entries_bottom = 0
        self._unwatched_virtual_size = None
        self.agent_running = False
        self.auto_scroll_paused_by_user = False
        self.final_response_started = False
        self.programmatic_scroll = False
        self.anchor_sync_scheduled = False
        self.view_hold_active = False
        self._view_hold_child = None
        self._view_hold_arrangement = None
        self._view_hold_rows = 0
        self._host.anchor()

    def stop(self) -> None:
        """Shutdown timer/GC state for widget unmount."""
        if self.manual_scroll_gc_timer is not None:
            self.manual_scroll_gc_timer.stop()
            self.manual_scroll_gc_timer = None
        self.scrollbar_grabbed = False
        self.resume_gc_after_manual_scroll(schedule_collect=False)
        self.end_view_hold()
        self._view_hold_child = None
        self._view_hold_rows = 0
        self._arrangement = None
        self._reveal = None
        self._reveal_top = -1
