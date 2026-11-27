# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spacing and guides between process rows.

An open row's text or calls end with a blank row under its guide; a folded row is its one row. The fold row below a
folded one, or below the summary of a turn's activity, keeps a blank row above it instead, so a turn's rows stand one
blank row apart, open or folded. The last folded row can leave a blank row below it with only its outer border.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from weakref import ref

from rich.style import Style
from rich.text import Text
from textual.geometry import Offset, Region
from textual.layout import WidgetPlacement

from chrys.app.tui.widgets.chat.guide_fold import GuideFoldMixin, GuideHoverMixin, ProcessGuideMixin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.color import Color
    from textual.geometry import Size
    from textual.widget import Widget

FOLD_GAP_ROWS: Final = 1
"""The rows of a gap: above a fold row that follows a folded one."""

_GAP_OFFSET = Offset(0, FOLD_GAP_ROWS)


class FoldGap(GuideHoverMixin):
    """Mixin for a transcript entry a gap parts from a folded row above it, or that is a folded row itself.

    The transcript shows the gaps as it lays its entries out (:func:`show_fold_gaps`). An entry with the gap above
    it shown places what it shows below it and draws the gap's guide under the preceding fold arrow
    (:meth:`gap_text`).
    """

    _gap_above_from: ref[FoldGap] | None = None
    _tail_padding = False

    @property
    def gap_above_from(self) -> FoldGap | None:
        """The folded entry whose arrow sets the short guide's column."""
        return self._gap_above_from() if self._gap_above_from is not None else None

    @property
    def gap_above(self) -> bool:
        return self._gap_above_from is not None

    gap_base_color: Color | None = None
    """The border's own color while the outer process guide is lit."""
    _gap_above_hover_color: Color | None = None

    def set_gap_hover(self, color: Color | None) -> None:
        """Repaint only the short guide's row."""
        self._gap_above_hover_color = color
        gutter = self.styles.gutter
        self.refresh(Region(gutter.left, gutter.top, self.content_size.width, FOLD_GAP_ROWS), layout=False)

    def takes_fold_gap(self) -> bool:
        """Whether a gap parts it from a folded row above it."""
        return False

    def ends_folded(self) -> bool:
        """Whether it is a folded row: one row, with no blank row of its own under it, as an open one has.

        When a fold row follows, that row provides the gap and draws the connecting guide.
        """
        return False

    def show_gap_above(self, above: FoldGap | None) -> None:
        if self.gap_above == (above is not None) and self.gap_above_from is above:
            return
        # A hidden entry can keep its old layout after the preceding row is removed.
        self._gap_above_from = ref(above) if above is not None else None
        self._gaps_moved()

    def show_tail_padding(self, shown: bool) -> None:
        if shown != self._tail_padding:
            self._tail_padding = shown
            self._gaps_moved()

    def _gaps_moved(self) -> None:
        # Set as the transcript arranges it, before measuring it: what it shows moved, which neither the
        # arrangement nor the box model Textual caches for it knows. Dropped as Textual drops them for an ancestor
        # of a widget laid out anew; a layout refresh would lay the whole transcript out again. Its own rows are
        # painted anew, even when the gap stays at the same position and only its source changes.
        self._clear_arrangement_cache()
        self._layout_updates += 1
        self.refresh()
        if self.is_attached and (hover := self.app.mouse_over) is not None and self in hover.ancestors_with_self:
            self.call_after_refresh(self.refresh_guide_hover)

    def process_layout(self, placements: list[WidgetPlacement]) -> list[WidgetPlacement]:
        """What it shows starts below its gap."""
        return WidgetPlacement.translate(placements, _GAP_OFFSET) if self.gap_above else placements

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        rows = self._content_rows(container, viewport, width)
        return rows + FOLD_GAP_ROWS if self._tail_padding else rows

    def _content_rows(self, container: Size, viewport: Size, width: int) -> int:
        return super().get_content_height(container, viewport, width)

    def gap_guide_offset(self) -> int:
        """The fold arrow's column relative to this gap painter's content."""
        source = self.gap_above_from or self
        source_left = source.styles.margin.left + source.styles.gutter.left
        return source_left - self.styles.margin.left - self.styles.gutter.left

    def gap_text(self) -> Text:
        """Draw the connector or the decorative guide above a process's first member."""
        source = self.gap_above_from or self
        offset = self.gap_guide_offset()
        color = self._gap_above_hover_color or source.gap_base_color or source.styles.border_left[1]
        if offset < 0 and isinstance(source, ProcessGuideMixin) and isinstance(self, GuideFoldMixin):
            offset = 0
            color = self.gap_base_color or self.styles.border_left[1]
        style = Style(color=(self.background_colors[1] + color).rich_color)
        line = " " * offset + "│" if offset >= 0 else ""
        return Text("\n".join([line] * FOLD_GAP_ROWS), style=style)


def show_fold_gaps(entries: Iterable[Widget], *, tail: Widget | None = None) -> None:
    """Show connecting gaps between visible rows in display order.

    A transcript calls it as it lays its entries out (``pre_layout``): an entry folding, opening, showing, hiding,
    mounting or going lays them out again, and only that does, so the gaps follow without a pass of their own.
    """
    above: FoldGap | None = None
    for entry in entries:
        if isinstance(entry, FoldGap):
            takes_gap = entry.takes_fold_gap()
            entry.show_gap_above(above if takes_gap else None)
            folded = entry.ends_folded()
            entry.show_tail_padding(entry is tail and folded)
            above = entry if folded else None
        else:
            above = None
