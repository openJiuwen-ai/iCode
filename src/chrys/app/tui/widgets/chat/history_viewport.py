# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep the archived transcript's geometry proportional to the viewport."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from itertools import pairwise

from textual.box_model import BoxModel
from textual.geometry import Region, Size, Spacing
from textual.layout import DockArrangeResult, WidgetPlacement
from textual.widget import Widget

from chrys.app.tui.widgets.chat.history_rows import HistoryRows
from chrys.app.tui.widgets.chat.messages import UserMessage
from chrys.app.tui.widgets.chat.replay_mount import REPLAY_FOREGROUND_ENTRY_LIMIT, estimated_rows

_TURNS_PER_PAGE = 4
_MIN_PAGES = 6
_MIN_COLD_PAGES = 16
_OVERSCAN_VIEWPORTS = 2
_CACHED_WIDTHS = 4

HistorySelectionRange = tuple[Fraction | int | None, Fraction | int | None]


def rebase_history_row(source: DockArrangeResult | None, destination: DockArrangeResult, row: float) -> float | None:
    """Keep the same visible widget and intra-widget row across two layouts."""
    if not row or source is destination:
        return row
    if source is None:
        return None
    anchor = _placement_at_history_row(source, round(row))
    return _rebase_history_placement(anchor, destination, row)


def _placement_at_history_row(source: DockArrangeResult, line: int) -> WidgetPlacement | None:
    margin_above = 0
    for placement in source.placements:
        if placement.fixed or placement.absolute:
            continue
        if placement.region.bottom <= line:
            margin_above = placement.margin.bottom
            continue
        return placement if placement.region.y - max(margin_above, placement.margin.top) <= line else None
    return None


def _rebase_history_placement(
    anchor: WidgetPlacement | None, destination: DockArrangeResult, row: float
) -> float | None:
    if anchor is not None:
        for current in destination.placements:
            if current.widget is anchor.widget:
                line = round(row)
                within = min(line - anchor.region.y, max(0, current.region.height - 1))
                return max(0.0, row + current.region.y + within - line)
    return None


class _HistoryGap(Widget):
    """A sizing input to Textual's vertical layout, with no DOM or render state."""

    ALLOW_SELECT = False

    def __init__(self) -> None:
        super().__init__()
        self.rows = 0

    def set_extent(self, rows: int, margin: Spacing) -> None:
        self.rows = max(0, rows)
        self.styles.set_rule("margin", margin)

    def _get_box_model(
        self,
        container: Size,
        viewport: Size,
        width_fraction: Fraction,
        height_fraction: Fraction,
        constrain_width: bool = False,
        greedy: bool = True,
    ) -> BoxModel:
        return BoxModel(Fraction(0), Fraction(self.rows), self.styles.margin)


@dataclass(frozen=True, slots=True)
class _Page:
    children: tuple[Widget, ...]
    top: UserMessage
    start: int


@dataclass(slots=True)
class _PageExtents:
    rows: HistoryRows
    measured: set[int]


class HistoryViewport:
    """A page index over the sealed prefix of a restored chat.

    An offscreen range contributes its extent through one sizing input instead
    of laying out all its widgets. Keep the logical Textual children intact:
    selection, process folds, rollback, and renderer state use their identities.
    The newest restored turn and all subsequent live content stay in the layout.
    """

    def __init__(self, children: Sequence[Widget]) -> None:
        self._entry_count = len(children)
        turns = [i for i, child in enumerate(children) if isinstance(child, UserMessage) and not child.is_injection]
        boundaries = [*turns[:-1:_TURNS_PER_PAGE], *turns[-1:]]
        self._pages = tuple(
            _Page(tuple(children[start:end]), top, start)
            for start, end in pairwise(boundaries)
            if isinstance(top := children[start], UserMessage)
        )
        self._page_of = {child: i for i, page in enumerate(self._pages) for child in page.children}
        self._archived_order = tuple(self._page_of)
        self._archived = {widget: index for index, widget in enumerate(self._archived_order)}
        self._turns = {
            child.id: child for child in self._page_of if isinstance(child, UserMessage) and child.id is not None
        }
        self._tail_start = children[turns[-1]] if turns else None
        self._bounds = HistoryRows([])
        self._extents_by_width: OrderedDict[int, _PageExtents] = OrderedDict()
        self._needs_measurement = True
        self._document_top = 0
        self._turn_margin_top = 0
        self.last_layout: DockArrangeResult | None = None
        self.last_selection_window: tuple[int, int] | None = None
        self._topology_version = -1
        self._topology_cold_count = 0
        self._prefix: tuple[Widget, ...] = ()
        self._tail: tuple[Widget, ...] = ()
        self._gaps = (_HistoryGap(), _HistoryGap(), _HistoryGap())

    @property
    def available(self) -> bool:
        return len(self._pages) >= _MIN_PAGES

    @property
    def measured(self) -> bool:
        return not self._needs_measurement

    def prime(self, size: Size) -> tuple[int, int, tuple[int, int]] | None:
        """Estimate the archive and identify the prefix and tail to mount first."""
        if len(self._pages) < _MIN_COLD_PAGES or not size:
            return None
        prefix_count = self._pages[0].start
        first_allowed_start = prefix_count + self._entry_count - REPLAY_FOREGROUND_ENTRY_LIMIT
        if self._pages[-1].start < first_allowed_start:
            # The prefix, last archive page and newest turn must all fit.
            # The bounded prepend handles a larger minimum foreground.
            return None
        estimate = [max(1, sum(estimated_rows(child, size.width) for child in page.children)) for page in self._pages]
        bounds = HistoryRows(estimate)
        stop = len(self._pages)
        viewport_start = max(0, bounds.start(stop) - max(1, size.height) * _OVERSCAN_VIEWPORTS)
        first = min(bounds.page_at(viewport_start), stop - 1)
        # Trim optional pages first. A folded page may contribute few rows
        # while still taking many roots to mount.
        while self._pages[first].start < first_allowed_start:
            first += 1
        self._bounds = bounds
        self._needs_measurement = False
        return prefix_count, self._pages[first].start, (first, stop)

    def widgets_in(self, window: tuple[int, int]) -> list[Widget]:
        first, stop = window
        return [widget for page in self._pages[first:stop] for widget in page.children]

    def selection_window(
        self, selection: HistorySelectionRange, sequence: Callable[[Widget], Fraction | None]
    ) -> tuple[int, int]:
        """Find the archive pages intersecting an inclusive transcript range."""
        first, last = selection
        page_count = len(self._pages)
        archive_end = self.last_archived_entry
        end_sequence = sequence(archive_end) if archive_end is not None else None
        if first is not None and end_sequence is not None and first > end_sequence:
            return page_count, page_count

        def page_start(page: _Page) -> Fraction | int:
            return sequence(page.top) or 0

        start = 0 if first is None else max(0, bisect_right(self._pages, first, key=page_start) - 1)
        stop = page_count if last is None else bisect_right(self._pages, last, key=page_start)
        return start, stop

    def needs_mount(self, window: tuple[int, int], needs_mount: Callable[[Sequence[Widget]], bool]) -> bool:
        first, stop = window
        return any(needs_mount(self._pages[page].children) for page in range(first, stop))

    def ready_window(
        self,
        required: tuple[int, int],
        overscan: tuple[int, int],
        needs_mount: Callable[[Sequence[Widget]], bool],
    ) -> tuple[int, int]:
        """Extend a ready viewport only through already mounted neighboring pages."""
        first, stop = required
        while first > overscan[0] and not needs_mount(self._pages[first - 1].children):
            first -= 1
        while stop < overscan[1] and not needs_mount(self._pages[stop].children):
            stop += 1
        return first, stop

    def turn(self, turn_id: str) -> UserMessage | None:
        return self._turns.get(turn_id)

    @property
    def last_archived_entry(self) -> Widget | None:
        """The final root that viewport layout or replay can omit."""
        return self._archived_order[-1] if self._archived_order else None

    def tail_is_attached_to(self, panel: Widget) -> bool:
        """Whether the start of the continuously mounted suffix still belongs to the panel."""
        return self._tail_start is not None and self._tail_start.parent is panel

    def finish_mounting(self, children: Sequence[Widget], topology_version: int) -> bool:
        """Require a complete archive once replay has no roots left to mount."""
        return self._update_topology(children, topology_version, cold_count=0)

    def is_current(self, result: DockArrangeResult) -> bool:
        """The document index and cached result must describe the same layout."""
        return self.last_layout is result

    def row_in_index(self, source: DockArrangeResult | None, row: float) -> float:
        """Translate a painted row after measuring a different history window."""
        destination = self.last_layout
        if not row or source is None or destination is None or source is destination:
            return row
        anchor = _placement_at_history_row(source, round(row))
        if anchor is None:
            return row
        rebased = _rebase_history_placement(anchor, destination, row)
        if rebased is not None:
            return rebased
        page = self._page_of.get(anchor.widget)
        if page is not None:
            opener = self._pages[page].top
            for placement in source.placements:
                if placement.widget is opener:
                    start = self._document_top + self._bounds.start(page)
                    return max(0.0, row + start - placement.region.y)
        return row

    def capture(self, result: DockArrangeResult, width: int, anchor_row: int | None = None) -> int:
        """Seed the index from a normal layout once replay has completed."""
        regions = {placement.widget: placement.region for placement in result.placements}
        if self._tail_start not in regions or any(page.top not in regions for page in self._pages):
            return 0
        anchor_page, anchor_top, anchor_height = self._anchor_page(anchor_row)
        anchor = self._anchor_placement(anchor_row, anchor_page)
        starts = [regions[page.top].y for page in self._pages]
        starts.append(regions[self._tail_start].y)
        self._document_top = starts[0]
        self._bounds = HistoryRows([end - start for start, end in pairwise(starts)])
        self._remember_width(width, _PageExtents(self._bounds, set(range(len(self._pages)))))
        self._needs_measurement = False
        self.last_layout = result
        self.last_selection_window = None
        return self._anchor_delta(regions, anchor_row, anchor_page, anchor_top, anchor_height, anchor)

    def invalidate_measurements(self, *, complete: bool = True) -> None:
        """Discard cached heights, optionally requiring a complete measurement."""
        self._needs_measurement |= complete
        self._extents_by_width.clear()

    def window(
        self, scroll_y: float, height: int, *, following: bool, overscan: int = _OVERSCAN_VIEWPORTS
    ) -> tuple[int, int]:
        row = round(scroll_y) - self._document_top
        if following:
            row = max(row, self._bounds.start(len(self._bounds)))
        guard = max(1, height) * overscan
        first = self._bounds.page_at(max(0, row - guard))
        stop = self._bounds.page_at(max(0, row + max(0, height - 1) + guard)) + 1
        return first, min(stop, len(self._pages))

    def page_row(self, widget: Widget) -> int | None:
        """Return a conservative location from which to lay out a cold jump target."""
        page = self._page_of.get(widget)
        if page is None or not self.measured:
            return None
        return self._document_top + self._bounds.start(page)

    def covered_scroll_row(self, window: tuple[int, int], row: float, height: int, bottom: int) -> float | None:
        """Give the closest row whose entire viewport fits in the measured window."""
        first, stop = window
        start = self._document_top + self._bounds.start(first) if first else 0
        end = min(bottom, self._document_top + self._bounds.start(stop) - height) if stop < len(self._pages) else bottom
        if start > end:
            return None
        return min(end, max(start, row))

    def includes(self, window: tuple[int, int] | None, widget: Widget) -> bool:
        page = self._page_of.get(widget)
        return window is None or page is None or window[0] <= page < window[1]

    def arrange(
        self,
        children: Sequence[Widget],
        topology_version: int,
        width: int,
        window: tuple[int, int],
        anchor_row: int | None,
        arrange_entries: Callable[[list[Widget]], DockArrangeResult],
        *,
        cold_count: int,
        selection_window: tuple[int, int] | None = None,
    ) -> tuple[DockArrangeResult, int] | None:
        """Measure the viewport and the prepared selection using native Textual geometry."""
        ranges = self._measurement_ranges(window, selection_window)
        if not self._update_topology(children, topology_version, cold_count=cold_count):
            return None
        tail = self._tail_start
        if tail is None or not tail.display:
            return None
        if isinstance(tail, UserMessage) and not tail.is_injection:
            self._turn_margin_top = tail.styles.margin.top
        anchor_page, anchor_top, anchor_height = self._anchor_page(anchor_row)
        anchor = self._anchor_placement(anchor_row, anchor_page)
        extents = self._extents_by_width.get(width)
        if extents is None:
            extents = _PageExtents(self._bounds.copy(), set())
        self._remember_width(width, extents)
        self._bounds = extents.rows
        active = list(self._prefix)
        gap_starts: dict[int, _HistoryGap] = {}
        previous_stop = 0
        page_count = len(self._pages)
        for first, stop in (*ranges, (page_count, page_count)):
            if previous_stop < first:
                gap = self._gaps[len(gap_starts)]
                if not self._set_gap(gap, previous_stop, first, tail):
                    return None
                gap_starts[previous_stop] = gap
                active.append(gap)
            for page in self._pages[first:stop]:
                if not page.top.display:
                    return None
                active.extend(page.children)
            previous_stop = stop
        active.extend(self._tail)

        result = arrange_entries(active)
        regions = {placement.widget: placement.region for placement in result.placements}
        document_start: Widget = gap_starts[0] if 0 in gap_starts else self._pages[0].top
        self._document_top = regions[document_start].y
        if tail not in regions:
            return None
        for first, stop in ranges:
            for index in range(first, stop):
                end_widget = self._pages[index + 1].top if index + 1 < stop else gap_starts.get(stop, tail)
                changed = self._bounds.measure(index, regions[end_widget].y - regions[self._pages[index].top].y)
                if changed and index in extents.measured and len(self._extents_by_width) > 1:
                    # A previously measured page changing at the same width means
                    # its content or fold state changed. Other widths must remeasure.
                    self._extents_by_width.clear()
                    self._remember_width(width, extents)
                extents.measured.add(index)
        result.placements[:] = [
            placement for placement in result.placements if not isinstance(placement.widget, _HistoryGap)
        ]
        result.widgets.difference_update(gap_starts.values())
        self.last_layout = result
        self.last_selection_window = selection_window
        delta = self._anchor_delta(regions, anchor_row, anchor_page, anchor_top, anchor_height, anchor)
        return result, delta

    @staticmethod
    def _measurement_ranges(window: tuple[int, int], selection: tuple[int, int] | None) -> tuple[tuple[int, int], ...]:
        if selection is None or selection[0] == selection[1]:
            return (window,)
        if window[0] == window[1]:
            return (selection,)
        first, second = sorted((window, selection))
        if first[1] >= second[0]:
            return ((first[0], max(first[1], second[1])),)
        return first, second

    def _remember_width(self, width: int, extents: _PageExtents) -> None:
        self._extents_by_width[width] = extents
        self._extents_by_width.move_to_end(width)
        if len(self._extents_by_width) > _CACHED_WIDTHS:
            self._extents_by_width.popitem(last=False)

    def _anchor_delta(
        self,
        regions: dict[Widget, Region],
        anchor_row: int | None,
        anchor_page: int | None,
        anchor_top: int,
        anchor_height: int,
        anchor: WidgetPlacement | None,
    ) -> int:
        if anchor is not None and anchor_row is not None and (new_region := regions.get(anchor.widget)) is not None:
            row_in_entry = anchor_row - anchor.region.y
            new_row = new_region.y + min(row_in_entry, max(0, new_region.height - 1))
            return new_row - anchor_row
        if anchor_page is not None and anchor_row is not None:
            new_start = self._bounds.start(anchor_page)
            within = anchor_row - anchor_top
            if anchor_height:
                new_height = self._bounds.start(anchor_page + 1) - new_start
                within = within * new_height // anchor_height
            return self._document_top + new_start + within - anchor_row
        return 0

    def _update_topology(self, children: Sequence[Widget], version: int, *, cold_count: int) -> bool:
        if self._topology_version == version and self._topology_cold_count == cold_count:
            return True
        tail = self._tail_start
        end = len(self._prefix) + len(self._archived_order) - cold_count
        if not 0 <= end < len(children) or children[end] is not tail:
            from_end = next((index for index, child in enumerate(reversed(children), 1) if child is tail), 0)
            end = len(children) - from_end if from_end else -1
        if end < 0:
            # Retiring the last restored turn removes its opener. The suffix
            # still starts immediately after the last mounted archive entry.
            last = next((i for i in range(len(children) - 1, -1, -1) if children[i] in self._archived), -1)
            if last < 0 or last + 1 == len(children):
                return False
            end = last + 1
        start = next((index for index in range(end) if children[index] in self._archived), end)
        # Keep destructive edits and insertions through the ordinary layout.
        if end - start + cold_count != len(self._archived_order):
            return False
        if cold_count:
            previous = -1
            for child in children[start:end]:
                index = self._archived.get(child, -1)
                if index <= previous:
                    return False
                previous = index
        elif tuple(children[start:end]) != self._archived_order:
            return False
        self._prefix = tuple(children[:start])
        self._tail = tuple(children[end:])
        self._tail_start = children[end]
        self._topology_version = version
        self._topology_cold_count = cold_count
        return True

    def _anchor_page(self, row: int | None) -> tuple[int | None, int, int]:
        if not len(self._bounds) or row is None or row < self._document_top:
            return None, 0, 0
        page = self._bounds.page_at(row - self._document_top)
        start = self._bounds.start(page)
        height = self._bounds.start(page + 1) - start if page < len(self._bounds) else 0
        return page, self._document_top + start, height

    def _anchor_placement(self, row: int | None, page: int | None) -> WidgetPlacement | None:
        if row is None or page is None or self.last_layout is None:
            return None
        return next(
            (
                placement
                for placement in self.last_layout.placements
                if not placement.fixed
                and row < placement.region.bottom
                and (
                    self._page_of.get(placement.widget, len(self._pages)) == page
                    or (
                        self._page_of.get(placement.widget, len(self._pages)) == page + 1
                        and placement.region.y - placement.margin.top <= row
                    )
                )
            ),
            None,
        )

    def _set_gap(self, gap: _HistoryGap, first: int, stop: int, tail: Widget) -> bool:
        top_widget = self._pages[first].top
        last = next((child for child in reversed(self._pages[stop - 1].children) if child.display), None)
        next_top = self._pages[stop].top if stop < len(self._pages) else tail
        if last is None or not top_widget.display or not next_top.display:
            return False
        bottom = last.styles.margin.bottom
        next_margin = next_top.styles.margin.top if next_top.is_attached else self._turn_margin_top
        seam = max(bottom, next_margin)
        extent = self._bounds.start(stop) - self._bounds.start(first)
        if extent < seam:
            return False
        # Cold turn openers have not picked up the panel's CSS. Their
        # leading margin agrees with the mounted turn opener's margin.
        top_margin = top_widget.styles.margin.top if top_widget.is_attached else self._turn_margin_top
        gap.set_extent(extent - seam, Spacing(top_margin, 0, bottom, 0))
        return True
