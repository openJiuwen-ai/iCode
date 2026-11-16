# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A unified diff drawn by one widget, for places where something else does the scrolling."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from textual.cache import LRUCache
from textual.content import Content
from textual.dom import NoScreen
from textual.geometry import Offset, Region, Size
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style as VisualStyle
from textual.widget import Widget

from chrys.app.tui.support.gc_freeze import detach_lru_cache, renew_lru_cache
from chrys.app.tui.widgets import HATCH_GLYPH, normalize_selection_rich_style
from chrys.app.tui.widgets.diff_view.cells import (
    ANNOTATION_WIDTH,
    COLLAPSED_ANNOTATION_WIDTH,
    COMMENT_ICON,
    MARKER_WIDTH,
    annotation_cell,
    marker_cell,
    number_cell,
    number_cell_width,
)
from chrys.app.tui.widgets.diff_view.palette import DiffLook
from chrys.app.tui.widgets.diff_view.rows import DiffRow, Side


class UnifiedDiffLines(Widget):
    """Both line numbers, the annotation and the code of every row, as lines of a single widget.

    `DiffView` lays a unified diff out as gutter columns around a code column, so that the code
    can scroll under its own scrollbars. The chat shows diffs at their full height and scrolls
    them with the transcript. Columns kept in step would be compositor work for nothing there,
    so this widget draws the same cells and the same code side by side in one line.
    """

    ALLOW_SELECT = True

    COMPONENT_CLASSES: ClassVar[set[str]] = {"hatch--pattern"}

    DEFAULT_CSS = """
    UnifiedDiffLines > .hatch--pattern {
        color: $hatch-color;
    }
    UnifiedDiffLines {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(
        self,
        rows: Sequence[DiffRow],
        *,
        number_digits: int,
        code_width: int,
        annotations: bool = True,
        number_side: Side | None = None,
        marked_row: int | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """*number_side* shows only that side's numbers, for rows from one text; *marked_row* gets the comment
        icon ahead of its number, with room for it on every row."""
        super().__init__(name=name, id=id, classes=classes)
        self._rows = rows
        self._number_digits = number_digits
        self._code_width = code_width
        self._annotations = annotations
        self._number_side = number_side
        self._marked_row = marked_row
        self._strip_cache: LRUCache[int, Strip] = LRUCache(maxsize=2000)
        self._strips_valid_for: int | None = None
        self._look_key: tuple[str, str | None] | None = None
        self._look: DiffLook | None = None
        self._painted: tuple[tuple[int, Region], list[Strip]] | None = None

    # -- content -----------------------------------------------------------------------------------

    @property
    def rows(self) -> Sequence[DiffRow]:
        return self._rows

    def set_rows(self, rows: Sequence[DiffRow], *, number_digits: int, code_width: int) -> None:
        """Show other rows."""
        self._rows = rows
        self._number_digits = number_digits
        self._code_width = code_width
        self._invalidate_render_cache()
        self.refresh(layout=True)

    @property
    def annotations(self) -> bool:
        """Whether the ``+`` and ``-`` of changed rows show."""
        return self._annotations

    def set_annotations(self, annotations: bool) -> None:
        if annotations == self._annotations:
            return
        self._annotations = annotations
        self._invalidate_render_cache()
        self.refresh(layout=True)

    @property
    def _annotation_width(self) -> int:
        return ANNOTATION_WIDTH if self._annotations else COLLAPSED_ANNOTATION_WIDTH

    @property
    def _gutter_width(self) -> int:
        numbers = 1 if self._number_side is not None else 2
        marker = MARKER_WIDTH if self._marked_row is not None else 0
        return marker + numbers * number_cell_width(self._number_digits) + self._annotation_width

    def _gutter_characters(self, y: int) -> int:
        """The characters of row ``y``'s gutter, which selections count: the comment icon is one character in
        two cells."""
        if y == self._marked_row:
            return self._gutter_width - MARKER_WIDTH + len(COMMENT_ICON)
        return self._gutter_width

    # -- caches ------------------------------------------------------------------------------------

    def prepare_for_gc_freeze(self) -> None:
        """Detach the cyclic strip LRU before the permanent generation changes."""
        # gc-freeze swaps in an acyclic capacity token; renew restores a real cache before next use.
        self._strip_cache = detach_lru_cache(self._strip_cache)  # ty: ignore[invalid-assignment]
        self._painted = None

    def after_gc_freeze(self) -> None:
        """Recreate the strip LRU after the permanent generation changes."""
        self._strip_cache = renew_lru_cache(self._strip_cache)

    def abort_gc_freeze(self) -> None:
        """Restore the strip LRU after an incomplete hook pass."""
        self._strip_cache = renew_lru_cache(self._strip_cache)

    def _invalidate_render_cache(self) -> None:
        self._strip_cache.clear()
        self._strips_valid_for = None
        self._look_key = None
        self._look = None
        self._painted = None

    def notify_style_update(self) -> None:
        """Forget rendered rows when the theme or a CSS variable changes."""
        super().notify_style_update()
        self._invalidate_render_cache()
        self.refresh()

    def _current_look(self) -> DiffLook:
        if self._look is None:
            self._look = DiffLook.of(self)
        return self._look

    # -- rendering ---------------------------------------------------------------------------------

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return self._gutter_width + self._code_width

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return len(self._rows)

    def _begin_frame(self) -> int:
        """The width to draw rows at, having forgotten the rows that it or the app's look have outdated."""
        look_key = (self.app.theme, self.app.console.color_system)
        if look_key != self._look_key:
            self._invalidate_render_cache()
            self._look_key = look_key
        width = self.content_region.width
        if width != self._strips_valid_for:
            self._strip_cache.clear()
            self._strips_valid_for = width
        return width

    def _safe_text_selection(self) -> Selection | None:
        try:
            return self.text_selection
        except NoScreen:
            return None

    def render_lines(self, crop: Region) -> list[Strip]:
        if not self.is_attached:
            return [Strip.blank(crop.width) for _ in crop.line_range]
        width = self._begin_frame()
        selection = self._safe_text_selection()
        # Line filters and the background need no place in this key: the app pins ``ansi_color``,
        # so they change only together with the theme, which forgets everything anyway.
        key = (width, crop)
        if selection is None and self._painted is not None and self._painted[0] == key:
            return self._painted[1]

        line_filters = self.get_line_filters()
        _base_background, background = self.background_colors
        strips: list[Strip] = []
        for y in crop.line_range:
            strip = self._render_row(y, width, selection)
            for line_filter in line_filters:
                strip = strip.apply_filter(line_filter, background)
            if crop.column_span != (0, width):
                strip = strip.crop(crop.x, crop.x + crop.width)
            strips.append(strip)
        self._painted = (key, strips) if selection is None else None
        return strips

    def render_line(self, y: int) -> Strip:
        if not self.is_attached:
            return Strip.blank(max(0, self.size.width))
        return self._render_row(y, self._begin_frame(), self._safe_text_selection())

    def _render_row(self, y: int, width: int, selection: Selection | None) -> Strip:
        if width <= 0:
            return Strip.blank(0)
        if not 0 <= y < len(self._rows):
            return Strip.blank(width, self.visual_style.rich_style)
        selected = None if selection is None else self._selected_characters(selection, y)
        if selected is not None:
            return self._draw_row(y, width, selected)
        strip = self._strip_cache.get(y)
        if strip is None:
            strip = self._strip_cache[y] = self._draw_row(y, width, None)
        return strip

    def _draw_row(self, y: int, width: int, selected: tuple[int, int] | None) -> Strip:
        row = self._rows[y]
        look = self._current_look()
        digits = self._number_digits
        if self._number_side is None:
            numbers = number_cell(row, look, side=Side.BEFORE, digits=digits, edge=True) + number_cell(
                row, look, side=Side.AFTER, digits=digits, edge=False
            )
        else:
            numbers = number_cell(row, look, side=self._number_side, digits=digits, edge=True)
        if self._marked_row is not None:
            numbers = marker_cell(row, look, marked=y == self._marked_row) + numbers
        # The cell opens with a space. Cut down to it, the annotation is off and the code keeps its distance.
        gutter = numbers + annotation_cell(row, look)[: self._annotation_width]
        room = max(1, width - gutter.cell_length)
        if row.code is None:
            # A break between hunks: with its cells, hatching from edge to edge.
            code = Content.styled(HATCH_GLYPH * room, look.hatch)
        else:
            code = row.code
            if code.cell_length < room:
                code = code.pad_right(room - code.cell_length)
            code = code.stylize_before(look.line_style(row.kind))
        line = gutter + code
        if selected is not None:
            start, end = selected
            selection_style = normalize_selection_rich_style(self.screen.get_component_rich_style("screen--selection"))
            line = line.stylize(VisualStyle.from_rich_style(selection_style), start, len(line) if end == -1 else end)
        visual_style = self.visual_style
        strip = Strip(line.render_segments(visual_style), cell_length=line.cell_length)
        # Offsets count the characters of the whole line, gutter included; see `_code_selection`.
        return strip.crop(0, width).adjust_cell_length(width, visual_style.rich_style).apply_offsets(0, y)

    # -- selection ---------------------------------------------------------------------------------

    def _selected_characters(self, selection: Selection, y: int) -> tuple[int, int] | None:
        """The part of row ``y`` to draw as selected. The gutter is never part of a selection."""
        span = selection.get_span(y)
        if span is None:
            return None
        start, end = span
        gutter = self._gutter_characters(y)
        if end != -1 and end <= gutter:
            return None
        return max(start, gutter), end

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """The selected code. A break counts as an empty line, which keeps rows and lines in step."""
        text = "\n".join("" if row.code is None else row.code.plain for row in self._rows)
        return self._code_selection(selection).extract(text), "\n"

    def _code_selection(self, selection: Selection) -> Selection:
        """``selection``, which counts from the left edge of the widget, counted from where the code begins."""

        def in_code(offset: Offset | None) -> Offset | None:
            return None if offset is None else Offset(max(0, offset.x - self._gutter_characters(offset.y)), offset.y)

        return Selection(in_code(selection.start), in_code(selection.end))
