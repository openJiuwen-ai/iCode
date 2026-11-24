# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The blank row above a fold row that follows a folded one, and below a folded one that ends the transcript.

An open row's text or calls end with a blank row under its guide; a folded row is its one row. The fold row below a
folded one, or below the summary of a turn's activity, keeps a blank row above it instead, so a turn's rows stand one
blank row apart, open or folded, and the guide under their fold arrows strings them together. Nothing follows the
last of them: the margin before what comes next parts it from that. A folded row that ends the chat transcript keeps a
blank row below it, as an open one ends with: only its own guide runs on through it, as no fold row follows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from weakref import ref

from rich.style import Style
from rich.text import Text
from textual.geometry import Offset, Region
from textual.layout import WidgetPlacement

from chrys.app.tui.widgets.chat.guide_fold import GuideHoverMixin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.color import Color
    from textual.geometry import Size
    from textual.widget import Widget

FOLD_GAP_ROWS: Final = 1
"""The rows of a gap: above a fold row that follows a folded one, or below a folded row that ends the transcript."""

_GAP_OFFSET = Offset(0, FOLD_GAP_ROWS)


class FoldGap(GuideHoverMixin):
    """Mixin for a transcript entry a gap parts from a folded row above it, or that is a folded row itself.

    The transcript shows the gaps as it lays its entries out (:func:`show_fold_gaps`). An entry with the gap above
    it shown places what it shows below it and draws the gap's guide under the preceding fold arrow
    (:meth:`gap_text`). An entry with a gap below draws its own guide through that gap.
    """

    _gap_above_from: ref[FoldGap] | None = None

    @property
    def gap_above_from(self) -> FoldGap | None:
        """The folded entry whose arrow sets the short guide's column."""
        return self._gap_above_from() if self._gap_above_from is not None else None

    @property
    def gap_above(self) -> bool:
        return self._gap_above_from is not None

    gap_below = False
    """Whether a gap shows below it: a folded row, it ends the transcript."""

    gap_base_color: Color | None = None
    """The border's own color while the outer process guide is lit."""
    _gap_above_hover_color: Color | None = None
    _gap_below_hover_color: Color | None = None

    def set_gap_hover(self, below: bool, color: Color | None) -> None:
        """Repaint only the short guide's row."""
        if below:
            self._gap_below_hover_color = color
        else:
            self._gap_above_hover_color = color
        gutter = self.styles.gutter
        y = self.size.height - gutter.bottom - 1 if below else gutter.top
        self.refresh(Region(gutter.left, y, self.content_size.width, FOLD_GAP_ROWS), layout=False)

    def gap_render(self) -> Text:
        """Render short guides above or below the children covering this entry."""
        if not self.gap_below:
            return self.gap_text()
        rows = [Text()] * max(self.content_size.height, 1)
        if self.gap_above:
            rows[0] = self.gap_text()
        rows[-1] = self.gap_text(below=True)
        return Text("\n").join(rows)

    def takes_fold_gap(self) -> bool:
        """Whether a gap parts it from a folded row above it."""
        return False

    def ends_folded(self) -> bool:
        """Whether it is a folded row: one row, with no blank row of its own under it, as an open one has.

        The gap above a fold row below it stands in for that row, or the gap below it as it ends the transcript.
        """
        return False

    def show_gap_above(self, above: FoldGap | None) -> None:
        if self.gap_above == (above is not None) and self.gap_above_from is above:
            return
        # A hidden entry can keep its old layout after the preceding row is removed.
        self._gap_above_from = ref(above) if above is not None else None
        self._gaps_moved()

    def show_gap_below(self, gap: bool) -> None:
        if gap is self.gap_below:
            return
        self.gap_below = gap
        self._gaps_moved()

    def _gaps_moved(self) -> None:
        # Set as the transcript arranges it, before measuring it: what it shows moved, which neither the
        # arrangement nor the box model Textual caches for it knows. Dropped as Textual drops them for an ancestor
        # of a widget laid out anew; a layout refresh would lay the whole transcript out again. Its own rows are
        # painted anew, though its size may not change: a gap can show above it as the one below it goes.
        self._clear_arrangement_cache()
        self._layout_updates += 1
        self.refresh()
        if self.is_attached and (hover := self.app.mouse_over) is not None and self in hover.ancestors_with_self:
            self.call_after_refresh(self.refresh_guide_hover)

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        """Its content's rows, and those of the gap below them when it shows one: its own guide runs on through
        them, as it is drawn down the entry's height."""
        rows = self._content_rows(container, viewport, width)
        return rows + FOLD_GAP_ROWS if self.gap_below else rows

    def _content_rows(self, container: Size, viewport: Size, width: int) -> int:
        """The rows of what it shows, its gap above included, and not the gap below it."""
        return super().get_content_height(container, viewport, width)

    def process_layout(self, placements: list[WidgetPlacement]) -> list[WidgetPlacement]:
        """What it shows starts below its gap."""
        return WidgetPlacement.translate(placements, _GAP_OFFSET) if self.gap_above else placements

    def gap_guide_offset(self, *, below: bool = False) -> int:
        """The fold arrow's column relative to this gap painter's content."""
        source = self if below else self.gap_above_from or self
        source_left = source.styles.margin.left + source.styles.gutter.left
        return source_left - self.styles.margin.left - self.styles.gutter.left

    def gap_text(self, *, below: bool = False) -> Text:
        """Draw the short guide under its source's fold arrow."""
        source = self if below else self.gap_above_from or self
        hovered = self._gap_below_hover_color if below else self._gap_above_hover_color
        color = hovered or source.gap_base_color or source.styles.border_left[1]
        style = Style(color=(self.background_colors[1] + color).rich_color)
        offset = self.gap_guide_offset(below=below)
        # The outer border already draws the line when a process title is above a member.
        line = " " * offset + "│" if offset >= 0 else ""
        return Text("\n".join([line] * FOLD_GAP_ROWS), style=style)


def show_fold_gaps(entries: Iterable[Widget], *, end: Widget | None = None) -> None:
    """Show the gap of each of the *entries* shown, in order, that follows a folded row, and the gap below *end*, the
    entry that ends the transcript, when it is a fold row folded; hide the others'.

    A transcript calls it as it lays its entries out (``pre_layout``): an entry folding, opening, showing, hiding,
    mounting or going lays them out again, and only that does, so the gaps follow without a pass of their own.
    """
    above: FoldGap | None = None
    for entry in entries:
        if isinstance(entry, FoldGap):
            takes_gap = entry.takes_fold_gap()
            entry.show_gap_above(above if takes_gap else None)
            folded = entry.ends_folded()
            entry.show_gap_below(entry is end and takes_gap and folded)
            above = entry if folded else None
        else:
            above = None
