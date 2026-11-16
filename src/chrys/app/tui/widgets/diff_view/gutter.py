# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A column of cells beside the code: line numbers, or the ``+`` and ``-`` of changed lines."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, ClassVar, Self

from rich.style import Style
from textual import events
from textual.cache import LRUCache
from textual.content import Content
from textual.geometry import Offset, Region, Size
from textual.strip import Strip
from textual.style import Style as VisualStyle
from textual.widget import Widget

from chrys.app.tui.i18n import render_text, widget_localizer
from chrys.app.tui.support.gc_freeze import detach_lru_cache, renew_lru_cache
from chrys.app.tui.widgets.diff_view.cells import COMMENT_ICON
from chrys.app.tui.widgets.diff_view.palette import DiffLook
from chrys.app.tui.widgets.diff_view.rows import Side, comment_side, line_identity
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from chrys.app.tui.widgets.diff_view.code import CodeColumn
    from chrys.app.tui.widgets.diff_view.rows import DiffRow

type GutterCell = Callable[[DiffRow, DiffLook], Content]
"""Draws what a gutter shows for one row. See `chrys.app.tui.widgets.diff_view.cells`."""


_ADD_COMMENT_TIP = msg(
    "tui.diff_quote.bubble_add",
    fallback="Comment on this line. It goes into the message input; send it from the chat.",
)
_EDIT_COMMENT_TIP = msg("tui.diff_quote.bubble_edit", fallback="Edit this comment")


class GutterColumn(Widget):
    """One cell per row, in step with a `CodeColumn`.

    The gutter does not scroll. It reads the position of its code column and draws the cells of
    the rows that column shows, and the code column refreshes it when the position changes.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {"hatch--pattern", "diff--comment-line"}

    DEFAULT_CSS = """
    GutterColumn > .hatch--pattern {
        color: $hatch-color;
    }
    GutterColumn > .diff--comment-line { background: $accent 22%; }
    GutterColumn {
        width: auto;
        height: 1fr;
    }
    """

    def __init__(
        self,
        rows: Sequence[DiffRow],
        cell: GutterCell,
        cell_width: int,
        code_column: CodeColumn,
        *,
        side: Side | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.side = side
        self.current_comment_row: int | None = None
        self.comment_rows: frozenset[int] = frozenset()
        """Rows with a comment in the draft: their number stays the comment icon."""
        self._hover_row: int | None = None
        """The row under the pointer anywhere in the diff, which shows the icon to start a comment."""
        self._press_offset: Offset | None = None
        self.rows = rows
        self.code_column = code_column
        self._cell = cell
        self._cell_width = cell_width
        self._strip_cache: LRUCache[int, Strip] = LRUCache(maxsize=500)
        self._look_key: tuple[str, str | None] | None = None
        self._look: DiffLook | None = None
        self._painted: tuple[tuple[int, int, Region], list[Strip]] | None = None

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
        self._look_key = None
        self._look = None
        self._painted = None

    def notify_style_update(self) -> None:
        """Forget rendered cells when the theme or a CSS variable changes."""
        super().notify_style_update()
        self._invalidate_render_cache()
        self.refresh()

    def refresh(self, *regions: Region, repaint: bool = True, layout: bool = False, recompose: bool = False) -> Self:
        """Whoever asks for a refresh knows of a change that the scroll position does not show."""
        self._painted = None
        return super().refresh(*regions, repaint=repaint, layout=layout, recompose=recompose)

    # -- rendering ---------------------------------------------------------------------------------

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return self._cell_width

    def _window(self) -> tuple[int, int]:
        """The first row on display, and how many: never more than the code column has lines for.

        The code column may be the shorter of the two by the scrollbar under it. Cells drawn
        beside that scrollbar would belong to rows the column is not showing.
        """
        look_key = (self.app.theme, self.app.console.color_system)
        if look_key != self._look_key:
            self._invalidate_render_cache()
            self._look_key = look_key
        code_column = self.code_column
        return round(code_column.scroll_offset.y), code_column.scrollable_content_region.height

    def render_lines(self, crop: Region) -> list[Strip]:
        if not self.is_attached:
            return [Strip.blank(crop.width) for _ in crop.line_range]
        first_row, height = self._window()
        key = (first_row, height, crop)
        if self._painted is not None and self._painted[0] == key:
            return self._painted[1]

        width = self._cell_width
        line_filters = self.get_line_filters()
        _base_background, background = self.background_colors
        strips: list[Strip] = []
        for y in crop.line_range:
            strip = self._render_cell(first_row + y) if y < height else self._blank()
            for line_filter in line_filters:
                strip = strip.apply_filter(line_filter, background)
            if crop.column_span != (0, width):
                strip = strip.crop(crop.x, crop.x + crop.width)
            strips.append(strip)
        self._painted = (key, strips)
        return strips

    def render_line(self, y: int) -> Strip:
        if not self.is_attached:
            return Strip.blank(self._cell_width)
        first_row, height = self._window()
        return self._render_cell(first_row + y) if y < height else self._blank()

    def _blank(self) -> Strip:
        return Strip.blank(self._cell_width, self.visual_style.rich_style)

    def _render_cell(self, index: int) -> Strip:
        if not 0 <= index < len(self.rows):
            return self._blank()
        strip = self._strip_cache.get(index)
        if strip is None:
            if self._look is None:
                self._look = DiffLook.of(self)
            visual_style = self.visual_style
            cell = self._cell(self.rows[index], self._look)
            icon = self._shows_icon(index)
            if icon:
                cell = self._icon_cell(cell)
            if index == self.current_comment_row:
                cell = cell.stylize(
                    VisualStyle.from_rich_style(
                        Style(bgcolor=self.get_component_rich_style("diff--comment-line").bgcolor)
                    )
                )
            strip = Strip(cell.render_segments(visual_style), cell_length=cell.cell_length)
            strip = self._strip_cache[index] = strip.adjust_cell_length(self._cell_width, visual_style.rich_style)
        return strip

    def _shows_icon(self, index: int) -> bool:
        """Whether row *index* draws the comment icon here instead of its number."""
        if self.side is None or not self.code_column.comments_enabled:
            return False
        row = self.rows[index]
        if line_identity(row) is None or comment_side(row) is not self.side:
            return False
        return index in self.comment_rows or index == self._hover_row

    def _icon_cell(self, cell: Content) -> Content:
        """The number cell with its digits replaced by the icon; the edge bar keeps its colour."""
        icon = Content(COMMENT_ICON)
        # Centered in what the edge leaves: on the digits of a one- or two-digit number.
        room = max(0, self._cell_width - 1 - icon.cell_length)
        left = room // 2
        replacement = Content(" " * left) + icon + Content(" " * (room - left))
        for start, end, style in cell.spans:
            if start <= 1 < end:
                replacement = replacement.stylize(style)
        return cell[:1] + replacement

    def _refresh_rows(self, *indexes: int | None) -> None:
        first, _height = self._window()
        for changed in indexes:
            if changed is not None:
                self._strip_cache.discard(changed)
                self.refresh(Region(0, changed - first, self._cell_width, 1))

    def set_comment_rows(self, rows: frozenset[int]) -> None:
        if rows != self.comment_rows:
            changed = rows ^ self.comment_rows
            self.comment_rows = rows
            self._refresh_rows(*changed)

    def set_comment_hover(self, index: int | None) -> None:
        """Show the icon on row *index* (if this column is where it belongs); repaint only the two rows."""
        if index != self._hover_row:
            old, self._hover_row = self._hover_row, index
            self._refresh_rows(old, index)

    def set_current_comment(self, index: int | None) -> None:
        if self.current_comment_row != index:
            self.current_comment_row = index
            self._invalidate_render_cache()
            self.refresh()

    def _row_at(self, y: int) -> int | None:
        if self.side is None or not self.code_column.comments_enabled:
            return None
        first, height = self._window()
        index = first + y
        if 0 <= y < height and 0 <= index < len(self.rows):
            row = self.rows[index]
            if not row.is_placeholder and row.number(self.side) is not None:
                return index
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._press_offset = event.offset if event.button == 1 else None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if event.offset != self._press_offset:
            self._press_offset = None
        # Anywhere on a row counts, also in a number column that has no number for it.
        index = self.code_column.row_at(event.y)
        self.code_column.set_comment_hover(index)
        tooltip = None
        if index is not None and self._shows_icon(index):
            message = _EDIT_COMMENT_TIP if index in self.comment_rows else _ADD_COMMENT_TIP
            tooltip = render_text(widget_localizer(self), message.bind())
        if self.tooltip != tooltip:
            self.tooltip = tooltip

    def on_leave(self, _event: events.Leave) -> None:
        self._press_offset = None
        self.code_column.set_comment_hover(None)
        self.tooltip = None

    def on_click(self, event: events.Click) -> None:
        index = self._row_at(event.y)
        clicked = self._press_offset == event.offset
        self._press_offset = None
        if event.button == 1 and index is not None and clicked:
            event.prevent_default()
            event.stop()
            self.post_message(self.code_column.RowRequested(self.code_column, index))
