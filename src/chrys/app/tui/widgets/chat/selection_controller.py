# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""O(visible) drag selection for the chat transcript.

Textual's generic cross-widget selection walker
(:meth:`SelectState._walk_selected_widgets`) re-walks and re-sorts every
transcript widget on every mouse move, and ``Screen._watch_selections``
notifies every previously/currently selected widget even when its selection
did not change. On sessions with thousands of transcript widgets a simple
drag pegs a CPU core.

This module replaces the geometric walk with a *logical* selection model for
drags whose both endpoints live in the same :class:`ChatPanel`:

- Every top-level transcript widget gets a monotonically increasing mount
  sequence (stamped by ``ChatPanel.mount``). A leaf's *document key* is the
  tuple ``(top_seq, *sibling_indices)`` down to the leaf, so document order
  comparisons never touch the compositor.
- Selection anchors are (leaf, content offset) pairs. Membership of any leaf
  in the selected range is a pure key comparison, so per-mouse-move work is
  bounded by the widgets in the compositor's visible map — never the whole
  transcript — even while the drag auto-scrolls or the anchors sit offscreen.
- ``screen.selections`` only ever contains the visible slice plus the two
  anchor leaves. Copy walks the anchored range once, at copy time.
- After the drag ends the anchors are content-addressed, so scrolling only
  needs a cheap re-projection (hooked from ``ChatPanel.watch_scroll_y``) to
  keep highlights correct as selected content scrolls in and out of view.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, TypeGuard

from textual.geometry import Offset, Region
from textual.selection import SELECT_ALL, Selection
from textual.widget import Widget

from chrys.app.tui.widgets.chat.tool_call import is_tool_copy_excluded
from chrys.app.tui.widgets.chat.transcript_panel import ChatTranscriptPanelMarker

if TYPE_CHECKING:
    from textual.screen import Screen
    from textual.selection import SelectStart, SelectState

    from chrys.app.tui.widgets.chat.panel import ChatPanel

DocKey = tuple[Fraction | int, ...]

# Orders a gap representative key after every real key that shares its
# ``before`` prefix; leaf keys never extend below a leaf, so any suffix works.
_AFTER_LEAF = 1 << 60


def _is_chat_panel(widget: Widget) -> TypeGuard[ChatPanel]:
    """Narrow the nominal marker to its sole concrete implementer."""
    return isinstance(widget, ChatTranscriptPanelMarker)


def chat_panel_ancestor(widget: Widget | None) -> ChatPanel | None:
    """Return the owning chat panel for *widget*, if it lives in chat."""
    if widget is None:
        return None
    for ancestor in widget.ancestors_with_self:
        # Nominal marker check instead of the concrete class: chat helper
        # modules must not import chat.panel (architecture boundary).
        if isinstance(ancestor, Widget) and _is_chat_panel(ancestor):
            return ancestor
    return None


def _is_selectable_leaf(widget: Widget) -> bool:
    """Mirror the leaf filter of Textual's selection walker, minus tool UI."""
    return not widget.is_container and widget.allow_select and not is_tool_copy_excluded(widget)


@dataclass(frozen=True)
class _Anchor:
    """A resolved selection endpoint on a selectable transcript leaf."""

    leaf: Widget
    key: DocKey
    offset: Offset | None  # None selects up to the leaf's own boundary


@dataclass(frozen=True)
class _GapAnchor:
    """A selection endpoint that fell between leaves (message gap, padding)."""

    before: _Anchor | None
    after: _Anchor | None

    @property
    def rep_key(self) -> DocKey:
        """A key strictly between the neighbouring leaves, for ordering."""
        if self.before is not None:
            return (*self.before.key, _AFTER_LEAF)
        return (-1,)


class ChatSelectionController:
    """Logical drag-selection state for the chat transcript."""

    def __init__(self, screen: Screen) -> None:
        self._screen = screen
        self._panel: ChatPanel | None = None
        # Raw drag endpoints; the start is frozen per drag, the end tracks the
        # pointer. After the drag they collapse into the resolved pair below.
        self._drag_source: SelectStart | None = None
        self._drag_start: _Anchor | _GapAnchor | None = None
        # Resolved, document-ordered range used for projection and copy.
        self._first: _Anchor | None = None
        self._last: _Anchor | None = None
        self._reproject_scheduled = False

    @property
    def active(self) -> bool:
        """Whether a chat logical selection currently exists."""
        return self._first is not None and self._last is not None

    def clear(self) -> None:
        """Drop the logical selection (highlights are owned by the screen)."""
        self._panel = None
        self._drag_source = None
        self._drag_start = None
        self._first = None
        self._last = None
        self._reproject_scheduled = False

    # -- drag handling ---------------------------------------------------

    def handle_select_state(self, select_state: SelectState) -> bool:
        """Apply *select_state* as a chat selection; ``False`` falls back.

        The caller guarantees ``select_state.end`` is set and the
        single-content-widget case was already delegated to Textual.
        """
        end = select_state.end
        if end is None:
            return False
        start_panel = chat_panel_ancestor(select_state.start.content_widget or select_state.start.container)
        end_panel = chat_panel_ancestor(end.content_widget or end.container)
        if start_panel is None or start_panel is not end_panel:
            return False
        panel = start_panel

        if self._drag_source is not select_state.start or self._panel is not panel:
            drag_start = self._resolve_start(panel, select_state)
            if drag_start is None:
                return False
            self._panel = panel
            self._drag_source = select_state.start
            self._drag_start = drag_start

        drag_end = self._resolve_end(panel, select_state)
        if drag_end is None:
            return False
        # Stock parity: an in-progress drag marks the screen as selecting.
        self._screen._selecting = True
        if not self._commit_range(panel, self._drag_start, drag_end):
            # Endpoints collapsed onto nothing (drag inside one gap).
            self._first = None
            self._last = None
            self._screen.selections = {}
            return True
        self._screen.selections = self._project(panel)
        return True

    def schedule_reproject(self, panel: ChatPanel) -> None:
        """Mark highlights stale; the next screen reflow re-projects them.

        Scroll watchers fire before the compositor rebuilds its visible map
        for the new offset, and ``call_after_refresh`` callbacks can still run
        ahead of that rebuild (the screen queue drains before the panel's
        dirty state propagates). Consuming the flag from the screen's
        ``_refresh_layout`` hook is the only point where the visible map is
        fresh by construction.
        """
        if not self.active or panel is not self._panel or self._reproject_scheduled:
            return
        if self._screen._selecting:
            # Mid-drag updates run through handle_select_state with fresh
            # pointer data; stock Textual also leaves wheel scrolls mid-drag
            # to the next mouse move.
            return
        self._reproject_scheduled = True

    def on_screen_layout_refreshed(self) -> None:
        """Consume a pending re-projection right after a compositor reflow."""
        if not self._reproject_scheduled:
            return
        self._reproject_scheduled = False
        panel = self._panel
        if panel is not None:
            self.reproject(panel)

    def reproject(self, panel: ChatPanel) -> None:
        """Refresh visible highlights after the panel scrolled or relaid out.

        Must not touch ``screen._selecting``: the drag is over, and marking
        the screen as selecting again would make plain mouse moves resume it
        (``_select_state`` survives until the next click).
        """
        if not self.active or panel is not self._panel or self._screen._selecting:
            return
        if not self._anchors_alive(panel):
            self.clear()
            return
        self._screen.selections = self._project(panel)

    # -- anchor resolution -------------------------------------------------

    def _resolve_start(self, panel: ChatPanel, select_state: SelectState) -> _Anchor | _GapAnchor | None:
        content = select_state.start.content_widget
        offset = select_state.start.content_offset
        anchor = self._leaf_anchor(panel, content, offset)
        if anchor is not None:
            return anchor
        return self._gap_anchor(panel, select_state.start.pointer_start_offset)

    def _resolve_end(self, panel: ChatPanel, select_state: SelectState) -> _Anchor | _GapAnchor | None:
        """Resolve the pointer end each update.

        Resolution is geometric against the current visible map so the
        selection keeps growing while auto-scroll moves content under a
        stationary pointer, exactly like Textual's screen-space walker.
        """
        end = select_state.end
        if end is None:
            raise RuntimeError("Extending a chat selection requires an end anchor.")
        point = self._clamp_into(select_state.screen_offset, panel.region)
        containing: Widget | None = None
        row_hit: Widget | None = None
        for leaf, region in self._visible_leaves(panel):
            if region.y <= point.y < region.bottom:
                if region.x <= point.x < region.right:
                    containing = leaf
                    break
                if row_hit is None:
                    row_hit = leaf
        target = containing or row_hit
        if target is not None:
            offset = end.content_offset if end.content_widget is target else None
            anchor = self._leaf_anchor(panel, target, offset)
            if anchor is not None:
                return anchor
        return self._gap_anchor(panel, point)

    def _leaf_anchor(self, panel: ChatPanel, widget: Widget | None, offset: Offset | None) -> _Anchor | None:
        if widget is None or not _is_selectable_leaf(widget):
            return None
        key = self._doc_key(panel, widget)
        if key is None:
            return None
        return _Anchor(widget, key, offset)

    def _gap_anchor(self, panel: ChatPanel, point: Offset) -> _GapAnchor | None:
        """Resolve a point that hit no leaf to its document-order neighbours."""
        before: tuple[int, _Anchor] | None = None
        after: tuple[int, _Anchor] | None = None
        for leaf, region in self._visible_leaves(panel):
            anchor = self._leaf_anchor(panel, leaf, None)
            if anchor is None:
                continue
            if region.bottom <= point.y and (before is None or region.bottom > before[0]):
                before = (region.bottom, anchor)
            elif region.y > point.y and (after is None or region.y < after[0]):
                after = (region.y, anchor)
        if before is None and after is None:
            return None
        return _GapAnchor(before[1] if before else None, after[1] if after else None)

    def _commit_range(self, panel: ChatPanel, start: _Anchor | _GapAnchor | None, end: _Anchor | _GapAnchor) -> bool:
        """Order the raw endpoints and collapse gaps into leaf anchors."""
        if start is None:
            return False
        start_key = start.rep_key if isinstance(start, _GapAnchor) else start.key
        end_key = end.rep_key if isinstance(end, _GapAnchor) else end.key
        first, last = (start, end) if start_key <= end_key else (end, start)
        # A range starts after a leading gap and ends before a trailing gap.
        first_anchor = first.after if isinstance(first, _GapAnchor) else first
        last_anchor = last.before if isinstance(last, _GapAnchor) else last
        if first_anchor is None or last_anchor is None or first_anchor.key > last_anchor.key:
            return False
        if first_anchor.leaf is last_anchor.leaf:
            first_anchor, last_anchor = self._order_same_leaf(first_anchor, last_anchor)
        self._first = first_anchor
        self._last = last_anchor
        return True

    @staticmethod
    def _order_same_leaf(first: _Anchor, last: _Anchor) -> tuple[_Anchor, _Anchor]:
        a, b = first.offset, last.offset
        if a is not None and b is not None and b.transpose < a.transpose:
            a, b = b, a
        return _Anchor(first.leaf, first.key, a), _Anchor(last.leaf, last.key, b)

    # -- projection and extraction ----------------------------------------

    def _project(self, panel: ChatPanel) -> dict[Widget, Selection]:
        """Build the bounded selections map: visible slice plus anchors."""
        first, last = self._first, self._last
        if first is None or last is None:
            raise RuntimeError("Projecting a chat selection requires both anchors.")
        entries: list[tuple[DocKey, Widget, Selection]] = []
        if first.leaf is last.leaf:
            entries.append((first.key, first.leaf, self._merged_selection(first, last)))
        else:
            entries.append((first.key, first.leaf, self._partial(first.offset, None)))
            entries.append((last.key, last.leaf, self._partial(None, last.offset)))
            for leaf, _region in self._visible_leaves(panel):
                if leaf is first.leaf or leaf is last.leaf:
                    continue
                key = self._doc_key(panel, leaf)
                if key is not None and first.key < key < last.key:
                    entries.append((key, leaf, SELECT_ALL))
        entries.sort(key=lambda entry: entry[0])
        return {widget: selection for _key, widget, selection in entries}

    def extract_text(self) -> str | None:
        """Extract the full anchored range once, in document order.

        Returns ``None`` when no chat selection is active (callers fall back
        to the screen's selections dict).
        """
        if not self.active:
            return None
        panel = self._panel
        first, last = self._first, self._last
        if panel is None or first is None or last is None:
            raise RuntimeError("An active chat selection requires its panel and both anchors.")
        if not self._anchors_alive(panel):
            self.clear()
            return None

        widget_text: list[str] = []
        for leaf, selection in self._iter_range(panel, first, last):
            extracted = leaf.get_selection(selection)
            if extracted is not None:
                widget_text.extend(extracted)
        if not widget_text:
            return None
        return "".join(widget_text).rstrip("\n")

    def _iter_range(self, panel: ChatPanel, first: _Anchor, last: _Anchor) -> Iterator[tuple[Widget, Selection]]:
        """Yield (leaf, selection) pairs across the anchored range."""
        if first.leaf is last.leaf:
            yield first.leaf, self._merged_selection(first, last)
            return
        first_top, last_top = first.key[0], last.key[0]
        started = False
        for item in panel.children:
            seq = panel.transcript_seq(item)
            if seq is None or seq < first_top:
                continue
            if seq > last_top:
                break
            if not item.display or not item.visible:
                continue
            for leaf in self._iter_leaves(item):
                if not started:
                    if leaf is first.leaf:
                        started = True
                        yield leaf, self._partial(first.offset, None)
                    continue
                if leaf is last.leaf:
                    yield leaf, self._partial(None, last.offset)
                    return
                yield leaf, SELECT_ALL
        # Reaching here means the last anchor vanished mid-walk; the partial
        # yields so far still form a consistent prefix of the selection.

    def _iter_leaves(self, root: Widget) -> Iterator[Widget]:
        """Depth-first selectable leaves, matching the stock walker's pruning."""
        stack: list[Widget] = [root]
        while stack:
            node = stack.pop()
            if node.is_container:
                stack.extend(reversed(node.displayed_and_visible_children))
            elif _is_selectable_leaf(node):
                yield node

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _merged_selection(first: _Anchor, last: _Anchor) -> Selection:
        return Selection(first.offset, last.offset)

    @staticmethod
    def _partial(start: Offset | None, end: Offset | None) -> Selection:
        if start is None and end is None:
            return SELECT_ALL
        return Selection(start, end)

    def _anchors_alive(self, panel: ChatPanel) -> bool:
        first, last = self._first, self._last
        if first is None or last is None:
            return False
        return chat_panel_ancestor(first.leaf) is panel and chat_panel_ancestor(last.leaf) is panel

    def _visible_leaves(self, panel: ChatPanel) -> Iterator[tuple[Widget, Region]]:
        """Selectable chat leaves in the last composited frame, with regions."""
        for widget, (region, _clip) in self._screen._compositor.visible_widgets.items():
            if not _is_selectable_leaf(widget):
                continue
            if chat_panel_ancestor(widget) is not panel:
                continue
            yield widget, region

    def _doc_key(self, panel: ChatPanel, widget: Widget) -> DocKey | None:
        """Document-order key for *widget*: (transcript seq, *sibling path)."""
        path: list[int] = []
        node: Widget = widget
        while True:
            parent = node.parent
            if parent is panel:
                break
            if not isinstance(parent, Widget):
                return None
            try:
                path.append(parent._nodes.index(node))
            except ValueError:
                return None
            node = parent
        seq = panel.transcript_seq(node)
        if seq is None:
            return None
        return (seq, *reversed(path))

    @staticmethod
    def _clamp_into(point: Offset, region: Region) -> Offset:
        x = min(max(point.x, region.x), region.right - 1)
        y = min(max(point.y, region.y), region.bottom - 1)
        return Offset(x, y)
