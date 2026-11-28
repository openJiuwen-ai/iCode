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

- Every top-level transcript widget gets an order stamp. New entries can
  append or take a stamp between their neighbors. A leaf's *document key* is the
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
from dataclasses import dataclass, replace
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

    from chrys.app.tui.widgets.chat.history_viewport import HistorySelectionRange
    from chrys.app.tui.widgets.chat.panel import ChatPanel

DocKey = tuple[Fraction | int, ...]
_GapEdge = tuple[Widget, DocKey, bool]


@dataclass(frozen=True, slots=True)
class FallbackHistorySelection:
    state: SelectState
    excluded_leaf: Widget | None
    anchors: _AnchoredFallback | None = None
    bounds: HistorySelectionRange | None = None

    @property
    def selection_range(self) -> HistorySelectionRange | None:
        return self.anchors.selection_range if self.anchors is not None else self.bounds


@dataclass(frozen=True, slots=True)
class _FallbackGapStart:
    source: SelectStart
    first: _GapEdge | None
    last: _GapEdge | None
    logical: _GapAnchor | None
    row_reference: tuple[Widget, int] | None


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


@dataclass(frozen=True, slots=True)
class _AnchoredFallback:
    first: _Anchor | None
    last: _Anchor | None
    excluded_leaf: Widget | None
    external: tuple[dict[Widget, Selection], dict[Widget, Selection]] | None = None
    last_stamp: Fraction | None = None

    @property
    def selection_range(self) -> HistorySelectionRange:
        return (
            self.first.key[0] if self.first is not None else None,
            self.last.key[0] if self.last is not None else self.last_stamp,
        )

    @property
    def is_attached(self) -> bool:
        return all(
            anchor is None or anchor.leaf is self.excluded_leaf or anchor.leaf.is_attached
            for anchor in (self.first, self.last)
        )


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
        self._fallback_gap_start: _FallbackGapStart | None = None
        # Resolved, document-ordered range used for projection and copy.
        self._first: _Anchor | None = None
        self._last: _Anchor | None = None
        self._reproject_scheduled = False

    @property
    def active(self) -> bool:
        """Whether a chat logical selection currently exists."""
        return self._first is not None and self._last is not None

    def pending_history(self) -> tuple[ChatPanel, object, HistorySelectionRange] | None:
        """Return the panel, selection token, and range when copy needs offscreen entries."""
        panel, first, last = self._panel, self._first, self._last
        if panel is None or first is None or last is None or first.key[0] == last.key[0]:
            return None
        selection = (first.key[0], last.key[0])
        if panel.selection_needs_history_layout(*selection):
            return panel, (panel, first, last), selection
        return None

    def matches_history_selection(self, token: object) -> bool:
        panel, first, last = self._panel, self._first, self._last
        return (
            token == (panel, first, last)
            and first is not None
            and last is not None
            and first.leaf.is_attached
            and last.leaf.is_attached
        )

    def remember_fallback_gap_start(self, state: SelectState | None) -> None:
        """Capture a gap's boundaries while its neighboring rows have geometry."""
        if state is None or state.start.content_widget is not None:
            self._fallback_gap_start = None
            return
        start = state.start
        if self._fallback_gap_start is not None and self._fallback_gap_start.source is start:
            return
        self._fallback_gap_start = None
        panel = chat_panel_ancestor(start.container)
        if panel is not None and start.container.region:
            point = start.pointer_start_offset
            first = self._gap_edge(panel, point.y, first=True)
            last = self._gap_edge(panel, point.y, first=False)
            # A neighboring leaf keeps the row anchored through cold-page
            # remeasurement, which can move the index opposite to scrolling.
            reference = min(
                (edge[0] for edge in (first, last) if edge is not None),
                key=lambda leaf: abs(leaf.content_region.y - point.y),
                default=None,
            )
            self._fallback_gap_start = _FallbackGapStart(
                start,
                first,
                last,
                self._gap_anchor(panel, point),
                (reference, point.y - reference.content_region.y) if reference is not None else None,
            )

    def normalize_fallback_start(self, state: SelectState) -> FallbackHistorySelection:
        """Recover a cross-container drag's start after scrolling moves it out of view."""
        start = state.start
        leaf = start.content_widget
        gap = self._fallback_gap_start if leaf is None else None
        if gap is not None and gap.source is not start:
            gap = None
        unmodified = FallbackHistorySelection(state, None)
        if not start.container.is_attached or (leaf is not None and not leaf.is_attached):
            return unmodified
        panel = chat_panel_ancestor(leaf or start.container)
        if panel is None or not panel.history_layout_is_windowed() or not (region := panel.region):
            return unmodified
        destination = state.screen_offset
        if gap is not None and start.container is panel:
            point = self._fallback_gap_point(panel, gap, destination)
            if point is None:
                return unmodified
        elif start.container.region:
            point = start.pointer_start_offset
            if region.y <= point.y < region.bottom or region.contains(*destination):
                return unmodified
            point = self._clamp_drag_start(point, region, destination)
        else:
            point = self._windowed_out_drag_point(panel, self._doc_key(panel, leaf or start.container), destination)
            if point is None:
                return unmodified
        excluded_leaf = None
        if leaf is None:
            if gap is None:
                return unmodified
            edge = gap.first if point.transpose <= state.screen_offset.transpose else gap.last
            if edge is None or not edge[0].is_attached:
                return unmodified
            leaf, _key, included = edge
            if not included:
                excluded_leaf = leaf
        normalized = state._replace(
            start=start._replace(
                container=panel,
                container_pointer_delta=point - region.offset,
                container_initial_offset=region.offset,
                container_initial_scroll_offset=panel.scroll_offset,
                content_widget=leaf,
            )
        )
        lateral = self._lateral_fallback(panel, normalized)
        return FallbackHistorySelection(normalized, excluded_leaf, lateral)

    def _lateral_fallback(self, panel: ChatPanel, state: SelectState) -> _AnchoredFallback | None:
        """Bind the transcript portion when a recovered drag crosses a side edge."""
        point = state.screen_offset
        region = panel.region
        end = state.end
        if (
            end is None
            or not region.y <= point.y < region.bottom
            or region.x <= point.x < region.right
            or chat_panel_ancestor(end.content_widget or end.container) is panel
        ):
            return None
        origin = self._leaf_anchor(panel, state.start.content_widget, state.start.content_offset)
        if origin is None:
            return None
        origin_first = state.start.pointer_start_offset.transpose <= point.transpose
        # A pointer on the right is after the chat's last cell on this row.
        boundary_y = point.y + (point.x >= region.right)
        edge, excluded = self._lateral_edge(panel, boundary_y, first=not origin_first)
        if edge is None:
            return None
        first, last = (origin, edge) if origin_first else (edge, origin)
        if first.key > last.key:
            return None
        return _AnchoredFallback(first, last, excluded)

    def _lateral_edge(self, panel: ChatPanel, row: int, *, first: bool) -> tuple[_Anchor | None, Widget | None]:
        for leaf, _region in self._visible_leaves(panel):
            content = leaf.content_region
            if content.y < row < content.bottom:
                widget, offset = self._screen.get_widget_and_offset_at(content.x, row)
                if widget is leaf and offset is not None:
                    return self._leaf_anchor(panel, leaf, offset), None
        if (edge := self._gap_edge(panel, row if first else row - 1, first=first)) is not None:
            leaf, key, included = edge
            return _Anchor(leaf, key, None), None if included else leaf
        return None, None

    def _fallback_gap_point(self, panel: ChatPanel, gap: _FallbackGapStart, destination: Offset) -> Offset | None:
        if gap.row_reference is not None:
            leaf, delta = gap.row_reference
            if leaf.is_attached and leaf.region:
                region = panel.region
                point = Offset(region.x + gap.source.container_pointer_delta.x, leaf.content_region.y + delta)
                if point == gap.source.pointer_start_offset and region.y <= point.y < region.bottom:
                    return None
                return self._clamp_drag_start(point, region, destination)
        # A page outside the arrangement no longer has coordinates. Its
        # transcript position still tells us which edge it crossed.
        key = gap.logical.rep_key if gap.logical is not None and gap.logical.before is not None else None
        if key is None and gap.first is not None:
            key = gap.first[1]
        return self._windowed_out_drag_point(panel, key, destination)

    def _windowed_out_drag_point(self, panel: ChatPanel, key: DocKey | None, destination: Offset) -> Offset | None:
        if key is None:
            return None
        visible_keys = [
            other_key
            for widget, _region in self._visible_leaves(panel)
            if (other_key := self._doc_key(panel, widget)) is not None
        ]
        if not visible_keys:
            return None
        above = key < min(visible_keys)
        if not above and key <= max(visible_keys):
            return None
        # Cross one vertical edge to keep the selection bounded by the leaf.
        region = panel.region
        edge = region.offset if above else region.bottom_right_inclusive
        return self._order_sidebar_edge(edge, region, destination, above=above)

    def _clamp_drag_start(self, point: Offset, region: Region, destination: Offset) -> Offset:
        edge = self._clamp_into(point, region)
        if edge.y == point.y:
            return edge
        return self._order_sidebar_edge(edge, region, destination, above=point.y < region.y)

    @staticmethod
    def _order_sidebar_edge(edge: Offset, region: Region, destination: Offset, *, above: bool) -> Offset:
        if destination.y != edge.y or region.x <= destination.x < region.right:
            return edge
        # A sidebar can share this row. Keep the document direction when
        # Textual breaks the tie with the horizontal coordinate.
        x = min(edge.x, destination.x - 1) if above else max(edge.x, destination.x + 1)
        return Offset(x, edge.y)

    def fallback_history_range(
        self, panel: ChatPanel, state: SelectState, *, anchors: _AnchoredFallback | None = None
    ) -> HistorySelectionRange | None:
        """Resolve the transcript boundaries of a cross-container drag."""
        if anchors is None:
            normalized = self.normalize_fallback_start(state)
            state, anchors = normalized.state, normalized.anchors
        if anchors is not None:
            return anchors.selection_range
        end = state.end
        if end is None or not state.is_attached_to_dom() or state.select_container not in panel.ancestors:
            return None
        if not state.selection_bounds.overlaps(panel.region):
            return None
        first_offset, last_offset = state.ordered_offsets
        extends_above = first_offset.y < panel.region.y
        extends_below = last_offset.y >= panel.region.bottom
        if not extends_above and not extends_below:
            return None
        first_widget, last_widget = state.start.content_widget, end.content_widget
        if state.start.pointer_start_offset.transpose > state.screen_offset.transpose:
            first_widget, last_widget = last_widget, first_widget

        if extends_above and last_widget is not None and panel not in last_widget.ancestors:
            # Textual walks from the top until it finds the ending content
            # widget. An endpoint in a sibling leaves the whole chat selected.
            return None, None

        # Textual enters from the first chat leaf when the upper pointer is
        # outside. It walks to the last leaf when the lower pointer is outside.
        first = None if extends_above else self._fallback_boundary(panel, first_widget, first_offset.y, first=True)
        last = None if extends_below else self._fallback_boundary(panel, last_widget, last_offset.y, first=False)
        if (not extends_above and first is None) or (not extends_below and last is None):
            return None
        return first, last

    def pending_fallback_history(self, panel: ChatPanel, state: SelectState) -> HistorySelectionRange | None:
        selection = self.fallback_history_range(panel, state)
        return selection if selection is not None and panel.selection_needs_history_layout(*selection) else None

    def _fallback_boundary(
        self, panel: ChatPanel, widget: Widget | None, row: int, *, first: bool
    ) -> Fraction | int | None:
        if widget is not None:
            key = self._doc_key(panel, widget)
            return None if key is None else key[0]
        edge = self._gap_edge(panel, row, first=first)
        return edge[1][0] if edge is not None else None

    def _gap_edge(self, panel: ChatPanel, row: int, *, first: bool) -> _GapEdge | None:
        """Resolve a gap to an inclusive leaf or a leaf just outside the selected side."""
        inside: list[tuple[Widget, DocKey]] = []
        outside: list[tuple[Widget, DocKey]] = []
        for leaf, _region in self._visible_leaves(panel):
            if (key := self._doc_key(panel, leaf)) is not None:
                y = leaf.content_region.y
                side = inside if (y >= row if first else y <= row) else outside
                side.append((leaf, key))
        if inside:
            leaf, key = (min if first else max)(inside, key=lambda entry: entry[1])
            return leaf, key, True
        # A gap beside a cold page may have no visible leaf on the selected
        # side. Keep the opposite edge as an exclusive document boundary.
        if outside:
            leaf, key = (max if first else min)(outside, key=lambda entry: entry[1])
            return leaf, key, False
        return None

    def capture_fallback_selection(
        self, panel: ChatPanel, state: SelectState, selection_range: HistorySelectionRange
    ) -> FallbackHistorySelection:
        """Bind a gap endpoint to the first or last selected chat leaf."""
        normalized = self.normalize_fallback_start(state)
        state = normalized.state
        anchors = normalized.anchors
        start, end = state.start, state.end
        if (
            anchors is None
            and end is not None
            and all(chat_panel_ancestor(endpoint.container) is not panel for endpoint in (start, end))
        ):
            anchors = self._capture_external_range(panel, state, selection_range)
        if anchors is not None:
            return FallbackHistorySelection(
                state,
                normalized.excluded_leaf,
                replace(anchors, external=self._external_fallback_selections(panel, state)),
            )
        point = start.pointer_start_offset
        region = panel.region
        downwards = point.transpose <= state.screen_offset.transpose
        excluded_leaf = normalized.excluded_leaf
        if chat_panel_ancestor(start.container) is panel:
            leaf = start.content_widget
            if (
                leaf is None
                and region.y <= point.y < region.bottom
                and (edge := self._gap_edge(panel, point.y, first=downwards)) is not None
            ):
                leaf, _key, included = edge
                if not included:
                    excluded_leaf = leaf
            start = start._replace(
                container=panel,
                container_pointer_delta=point - region.offset,
                container_initial_scroll_offset=panel.scroll_offset,
                content_widget=leaf,
            )
        if end is not None and chat_panel_ancestor(end.container) is panel:
            leaf = end.content_widget
            if leaf is None and (edge := self._gap_edge(panel, state.screen_offset.y, first=not downwards)) is not None:
                leaf, _key, included = edge
                if not included:
                    excluded_leaf = leaf
            end = end._replace(container=panel, content_widget=leaf)
        first_stamp, last_stamp = selection_range
        if last_stamp is None:
            last_stamp = self._last_transcript_stamp(panel)
        return FallbackHistorySelection(
            state._replace(start=start, end=end), excluded_leaf, bounds=(first_stamp, last_stamp)
        )

    def _capture_external_range(
        self, panel: ChatPanel, state: SelectState, selection_range: HistorySelectionRange
    ) -> _AnchoredFallback:
        """Keep sibling pointer rows from changing the chat range after release."""
        upper, lower = state.ordered_offsets
        first_key, last_key = selection_range
        first = last = None
        excluded_leaf = None
        if first_key is not None and (edge := self._gap_edge(panel, upper.y, first=True)) is not None:
            leaf, key, included = edge
            first = _Anchor(leaf, key, None)
            if not included:
                excluded_leaf = leaf
        if last_key is not None and (edge := self._gap_edge(panel, lower.y, first=False)) is not None:
            leaf, key, included = edge
            last = _Anchor(leaf, key, None)
            if not included:
                excluded_leaf = leaf
        last_stamp = self._last_transcript_stamp(panel) if last_key is None else None
        return _AnchoredFallback(first, last, excluded_leaf, last_stamp=last_stamp)

    @staticmethod
    def _last_transcript_stamp(panel: ChatPanel) -> Fraction | None:
        # The restored tail is mounted even when its prefix is still cold.
        # A retry notice can vanish before copy. Its order stamp still
        # excludes messages appended after the pointer was released.
        return next(
            (
                stamp
                for item in reversed(panel.children)
                if not panel.is_infrastructure(item) and (stamp := panel.transcript_seq(item)) is not None
            ),
            None,
        )

    def restore_fallback_selection(self, panel: ChatPanel, state: SelectState) -> SelectState:
        """Keep the bound chat endpoint inside the resized selection container."""
        start, end = state.start, state.end
        region = panel.region
        if start.container is panel:
            point = region.offset + start.container_pointer_delta
            if start.content_widget is not None:
                point = self._clamp_into(point, region)
            # The saved point already accounts for the scroll at drag end.
            start = start._replace(
                container_pointer_delta=point - region.offset, container_initial_scroll_offset=panel.scroll_offset
            )
        offset = state.screen_offset
        if end is not None and end.container is panel and end.content_widget is not None:
            offset = self._clamp_into(offset, region)
        return state._replace(start=start, screen_offset=offset)

    def fallback_selections(
        self,
        panel: ChatPanel,
        state: SelectState,
        selection_range: HistorySelectionRange,
        *,
        excluded_leaf: Widget | None = None,
        anchors: _AnchoredFallback | None = None,
        complete: bool = False,
    ) -> dict[Widget, Selection]:
        """Rebuild the stock selection with the captured transcript boundaries."""
        if anchors is not None:
            return self._anchored_selections(panel, state, anchors, excluded_leaf, complete=complete)
        origin = state.start.content_widget
        visual_state = state
        if origin is not None and not origin.region:
            # During the drag an omitted origin sorts at row zero. Use its
            # viewport edge for highlights until the exact range is laid out.
            visual_state = state._replace(start=state.start._replace(content_widget=None))
        selections = dict.fromkeys(visual_state._walk_selected_widgets(), SELECT_ALL)
        state._apply_content_selections(selections)
        first, last = selection_range
        for widget in list(selections):
            key = self._doc_key(panel, widget)
            if key is not None and ((first is not None and key[0] < first) or (last is not None and key[0] > last)):
                # A windowed-out widget has no region and can sort before
                # the selected content in Textual's geometry-based walk.
                del selections[widget]
        if excluded_leaf is not None:
            selections.pop(excluded_leaf, None)
        return selections

    def finish_ready_lateral_selection(self, state: SelectState) -> None:
        """Commit the complete range when its pages were already laid out at release."""
        normalized = self.normalize_fallback_start(state)
        anchors = normalized.anchors
        if anchors is not None and (panel := chat_panel_ancestor(normalized.state.start.container)) is not None:
            self._screen.selections = self._anchored_selections(
                panel, normalized.state, anchors, normalized.excluded_leaf, complete=True
            )

    def _anchored_selections(
        self,
        panel: ChatPanel,
        state: SelectState,
        anchors: _AnchoredFallback,
        excluded_leaf: Widget | None,
        *,
        complete: bool,
    ) -> dict[Widget, Selection]:
        first, last = anchors.first, anchors.last
        if complete:
            chat = dict(self._iter_range(panel, first, last, last_stamp=anchors.last_stamp))
        else:
            # Live lateral drags bind both ends to visible leaves.
            if first is None or last is None:
                raise RuntimeError("Projecting a lateral selection requires both anchors.")
            chat = self._project_range(panel, first, last)
        for leaf in (excluded_leaf, anchors.excluded_leaf):
            if leaf is not None:
                chat.pop(leaf, None)
        external = anchors.external
        if external is None:
            external = self._external_fallback_selections(panel, state)
        before, after = external
        return before | chat | after

    @staticmethod
    def _external_fallback_selections(
        panel: ChatPanel, state: SelectState
    ) -> tuple[dict[Widget, Selection], dict[Widget, Selection]]:
        selections = dict.fromkeys(state._walk_selected_widgets(), SELECT_ALL)
        state._apply_content_selections(selections)
        before: dict[Widget, Selection] = {}
        after: dict[Widget, Selection] = {}
        outside = before
        for widget, selection in selections.items():
            if panel in widget.ancestors:
                outside = after
            else:
                outside[widget] = selection
        return before, after

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
        self._reproject_scheduled = True

    def on_screen_layout_refreshed(self) -> None:
        """Consume a pending re-projection right after a compositor reflow."""
        if not self._reproject_scheduled or self._screen._selecting:
            return
        self._reproject_scheduled = False
        panel = self._panel
        if panel is not None:
            self.reproject(panel)

    def finish_drag(self) -> None:
        """Refresh the anchored range before its text is offered as a quote."""
        if self._reproject_scheduled and (panel := self._panel) is not None:
            # Keep the request for the next layout in case release beat the reflow.
            self.reproject(panel)

    def reproject(self, panel: ChatPanel) -> None:
        """Refresh visible highlights after the panel scrolled or relaid out.

        Must not touch ``screen._selecting``: the drag is over, and marking
        the screen as selecting again would make plain mouse moves resume it
        (``_select_state`` survives until the next click).
        """
        if not self.active or panel is not self._panel or self._screen._selecting:
            return
        if not self._refresh_range_keys(panel):
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
        gap = self._fallback_gap_start
        if gap is not None and gap.source is select_state.start and gap.logical is not None:
            before = self._rekey(panel, gap.logical.before)
            after = self._rekey(panel, gap.logical.after)
            if before is not None or after is not None:
                return _GapAnchor(before, after)
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
        if isinstance(start, _GapAnchor):
            start = _GapAnchor(self._rekey(panel, start.before), self._rekey(panel, start.after))
        else:
            start = self._rekey(panel, start)
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
        return self._project_range(panel, first, last)

    def _project_range(self, panel: ChatPanel, first: _Anchor, last: _Anchor) -> dict[Widget, Selection]:
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
        if panel is None:
            raise RuntimeError("An active chat selection requires its panel.")
        if not self._refresh_range_keys(panel):
            self.clear()
            return None
        first, last = self._first, self._last
        if first is None or last is None:
            raise RuntimeError("An active chat selection requires both anchors.")

        widget_text: list[str] = []
        for leaf, selection in self._iter_range(panel, first, last):
            extracted = leaf.get_selection(selection)
            if extracted is not None:
                widget_text.extend(extracted)
        if not widget_text:
            return None
        return "".join(widget_text).rstrip("\n")

    def _iter_range(
        self, panel: ChatPanel, first: _Anchor | None, last: _Anchor | None, *, last_stamp: Fraction | None = None
    ) -> Iterator[tuple[Widget, Selection]]:
        """Yield (leaf, selection) pairs across the anchored range."""
        if first is not None and last is not None and first.leaf is last.leaf:
            yield first.leaf, self._merged_selection(first, last)
            return
        first_top = first.key[0] if first is not None else None
        last_top = last.key[0] if last is not None else last_stamp
        missing_first = first is not None and not first.leaf.is_attached
        missing_last = last is not None and not last.leaf.is_attached
        started = first is None or missing_first
        for item in panel.children:
            seq = panel.transcript_seq(item)
            if seq is None or (first_top is not None and (seq < first_top or (missing_first and seq == first_top))):
                continue
            if last_top is not None and (seq > last_top or (missing_last and seq == last_top)):
                break
            if not item.display or not item.visible:
                continue
            for leaf in self._iter_leaves(item):
                if not started and first is not None:
                    if leaf is first.leaf:
                        started = True
                        yield leaf, self._partial(first.offset, None)
                    continue
                if last is not None and leaf is last.leaf:
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

    def _rekey(self, panel: ChatPanel, anchor: _Anchor | None) -> _Anchor | None:
        if anchor is None or (key := self._doc_key(panel, anchor.leaf)) is None:
            return None
        return anchor if key == anchor.key else _Anchor(anchor.leaf, key, anchor.offset)

    def _refresh_range_keys(self, panel: ChatPanel) -> bool:
        """Keep a selection bound to its leaves if their transcript order changes."""
        first, last = self._rekey(panel, self._first), self._rekey(panel, self._last)
        if first is None or last is None:
            return False
        self._first, self._last = (first, last) if first.key <= last.key else (last, first)
        return True

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
