# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The code of a diff: a column that scrolls both ways and draws only the rows on screen."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, NamedTuple

from rich.style import Style
from textual import events
from textual.cache import LRUCache
from textual.content import Content
from textual.dom import NoScreen
from textual.geometry import Region, Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.style import Style as VisualStyle

from chrys.app.tui.support.gc_freeze import detach_lru_cache, renew_lru_cache
from chrys.app.tui.widgets import HATCH_GLYPH, normalize_selection_rich_style
from chrys.app.tui.widgets.diff_view.palette import DiffLook
from chrys.app.tui.widgets.diff_view.rows import DiffRow, RowKind, line_identity

if TYPE_CHECKING:
    from textual.selection import Selection

    from chrys.app.tui.widgets.diff_view.gutter import GutterColumn


class _Frame(NamedTuple):
    """What every row of one repaint has in common, measured once instead of once per row."""

    scroll_x: int
    scroll_y: int
    width: int
    """The cells a row may fill. The scrollbar gutter is always reserved and lies outside it."""
    content_width: int
    selection: Selection | None


class CodeColumn(ScrollView):
    """The code of one side of a diff, or of both in a unified one.

    Textual asks for the lines of the viewport only, so a diff of any length costs what its
    visible part costs. A rendered row is kept until the scroll position across, the width or the
    look of the app changes; scrolling down reuses all of them.
    """

    class RowRequested(Message):
        """A line number (or the comment icon in its place) requested a comment on this exact row."""

        def __init__(self, column: CodeColumn, index: int) -> None:
            super().__init__()
            self.column = column
            self.index = index

    ALLOW_SELECT = True

    COMPONENT_CLASSES: ClassVar[set[str]] = {"hatch--pattern", "diff--comment-line"}

    DEFAULT_CSS = """
    CodeColumn > .hatch--pattern {
        color: $hatch-color;
    }
    CodeColumn > .diff--comment-line {
        background: $accent 22%;
    }

    CodeColumn {
        overflow: auto auto;
        scrollbar-size: 1 2;
        scrollbar-gutter: stable;
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(
        self,
        rows: Sequence[DiffRow],
        code_width: int,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.rows = rows
        self.code_width = code_width
        self.scroll_sync: CodeColumn | None = None
        """The other side of a split diff, which scrolls along."""
        self.gutters: list[GutterColumn] = []
        """The columns beside this one. They follow its scroll position and have none of their own."""
        self.comments_enabled = False
        self.current_comment_row: int | None = None
        self._syncing = False
        self._strip_cache: LRUCache[int, Strip] = LRUCache(maxsize=2000)
        self._strips_valid_for: tuple[int, int] | None = None
        self._look_key: tuple[str, str | None] | None = None
        self._look: DiffLook | None = None
        self._frame: _Frame | None = None
        self._painted: tuple[tuple[_Frame, Region], list[Strip]] | None = None

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

    def on_mount(self) -> None:
        self.virtual_size = Size(self.code_width, len(self.rows))

    def _get_render_width(self) -> int:
        """The width rows are rendered at.

        With ``scrollbar-gutter: stable`` the scrollbar's column is always reserved. Rows fill the
        scrollable area, and the scrollbar or the widget's background takes the column beside it.
        """
        return self.scrollable_content_region.width

    def _begin_frame(self) -> _Frame:
        """Measure what a repaint depends on, and forget the rows those measurements have outdated."""
        look_key = (self.app.theme, self.app.console.color_system)
        if look_key != self._look_key:
            self._invalidate_render_cache()
            self._look_key = look_key
        scroll_x, scroll_y = self.scroll_offset
        frame = _Frame(
            round(scroll_x),
            round(scroll_y),
            self._get_render_width(),
            self.content_region.width,
            self._safe_text_selection(),
        )
        if (frame.scroll_x, frame.width) != self._strips_valid_for:
            self._strip_cache.clear()
            self._strips_valid_for = (frame.scroll_x, frame.width)
        return frame

    def _safe_text_selection(self) -> Selection | None:
        try:
            return self.text_selection
        except NoScreen:
            return None

    def render_lines(self, crop: Region) -> list[Strip]:
        if not self.is_attached:
            return [Strip.blank(crop.width) for _ in crop.line_range]
        frame = self._begin_frame()
        if frame.selection is None and self._painted is not None and self._painted[0] == (frame, crop):
            # Nothing moved: a repaint asked for by something else, say the parent scrolling.
            return self._painted[1]
        self._frame = frame
        try:
            strips = self._render_lines_direct(crop)
        finally:
            self._frame = None
        self._painted = ((frame, crop), strips) if frame.selection is None else None
        return strips

    def _render_lines_direct(self, crop: Region) -> list[Strip]:
        """Render the visible rows without Textual's generic style wrapper.

        The column has neither border nor padding, and `render_line` already resolves a row's
        background and text styles. Going around ``StylesCache.render`` matters when the parent
        scrolls over a clipped diff: the crop then changes with every frame, and the generic cache
        would walk every visible row although the rows themselves are all cached.
        """
        if self._frame is None:
            raise RuntimeError("Rendering diff lines requires a prepared frame.")
        content_width = self._frame.content_width
        rich_style = self.visual_style.rich_style
        line_filters = self.get_line_filters()
        _base_background, background = self.background_colors
        strips: list[Strip] = []
        for y in crop.line_range:
            strip = self.render_line(y)
            if strip.cell_length != content_width:
                strip = strip.adjust_cell_length(content_width, rich_style)
            for line_filter in line_filters:
                strip = strip.apply_filter(line_filter, background)
            if crop.column_span != (0, content_width):
                strip = strip.crop(crop.x, crop.x + crop.width)
            strips.append(strip)
        return strips

    def render_line(self, y: int) -> Strip:
        if not self.is_attached:
            return Strip.blank(max(0, self.size.width))
        # Outside a repaint of our own, a caller gets the row as it would be drawn right now.
        frame = self._begin_frame() if self._frame is None else self._frame
        index = frame.scroll_y + y
        if not 0 <= index < len(self.rows):
            return Strip.blank(frame.width, self.visual_style.rich_style)
        selected = None if frame.selection is None else frame.selection.get_span(index)
        if selected is not None:
            return self._render_row(index, frame, selected)
        strip = self._strip_cache.get(index)
        if strip is None:
            strip = self._strip_cache[index] = self._render_row(index, frame, None)
        return strip

    def _render_row(self, index: int, frame: _Frame, selected: tuple[int, int] | None) -> Strip:
        row = self.rows[index]
        visual_style = self.visual_style
        width = frame.width
        if row.code is None:
            # A break or a filler spans the viewport and stays put when the code scrolls sideways.
            line = Content.styled(HATCH_GLYPH * width, self._current_look().hatch)
            strip = Strip(line.render_segments(visual_style), cell_length=line.cell_length)
            return strip.adjust_cell_length(width, visual_style.rich_style)

        line = row.code
        # Pad before applying the current-line background, which fills the visible row.
        if (padding := max(self.code_width, width) - line.cell_length) > 0:
            line = line.pad_right(padding)
        if index == self.current_comment_row:
            line = line.stylize(
                VisualStyle.from_rich_style(Style(bgcolor=self.get_component_rich_style("diff--comment-line").bgcolor))
            )
        if selected is not None:
            start, end = selected
            selection_style = normalize_selection_rich_style(self.screen.get_component_rich_style("screen--selection"))
            line = line.stylize(
                VisualStyle.from_rich_style(selection_style), start, len(row.code) if end == -1 else end
            )
        # The line's background reaches the edge of the viewport, also where that is wider than the code.
        line = line.stylize_before(self._current_look().line_style(row.kind))
        strip = Strip(line.render_segments(visual_style), cell_length=line.cell_length)
        # Textual finds the character under the pointer from offsets that count characters of the
        # line, so the strip has to say which one the scroll position brought to its left edge.
        # The crop is asked, not a rule of our own: where it cuts through wide characters, emoji
        # sequences and combining marks is its business, and the count must agree with what it left.
        visible = strip.crop(frame.scroll_x)
        first_character = len(line) - sum(len(segment.text) for segment in visible)
        strip = visible.crop(0, width).adjust_cell_length(width, visual_style.rich_style)
        return strip.apply_offsets(first_character, index)

    def set_current_comment(self, index: int | None) -> None:
        if self.current_comment_row == index:
            return
        # An open comment bar covers the pointer; the icon comes back when it moves again.
        self.set_comment_hover(None)
        self.current_comment_row = index
        self._invalidate_render_cache()
        self.refresh()
        for gutter in self.gutters:
            gutter.set_current_comment(index)

    def row_at(self, y: int) -> int | None:
        """The row at viewport line *y*, if it is one a comment can go on."""
        index = round(self.scroll_offset.y) + y
        if (
            self.comments_enabled
            and 0 <= y < self.scrollable_content_region.height
            and 0 <= index < len(self.rows)
            and line_identity(self.rows[index]) is not None
        ):
            return index
        return None

    def set_comment_hover(self, index: int | None) -> None:
        """Turn the number of row *index* of this column into the comment icon.

        Each side of a split diff has lines of its own, so only the side under the pointer shows
        it. A context line is the exception: its icon stands in the right column, so the pointer
        over its left half shows it there.
        """
        target: CodeColumn = self
        if (
            index is not None
            and self.scroll_sync is not None
            and self.has_class("code-left")
            and self.rows[index].kind is RowKind.CONTEXT
        ):
            target = self.scroll_sync
        for column in (self, self.scroll_sync):
            if column is not None:
                for gutter in column.gutters:
                    gutter.set_comment_hover(index if column is target else None)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.comments_enabled:
            # A drag selects code to copy; the icon would only flicker along it.
            self.set_comment_hover(None if event.button else self.row_at(event.y))

    def on_leave(self, _event: events.Leave) -> None:
        if self.comments_enabled:
            self.set_comment_hover(None)

    # -- scrolling ---------------------------------------------------------------------------------

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if round(old_value) != round(new_value) and self.comments_enabled:
            # The pointer is over another row now; the next move shows that one.
            self.set_comment_hover(None)
        if self._syncing:
            return
        self._syncing = True
        try:
            if self.scroll_sync is not None and self.scroll_sync.scroll_offset.y != new_value:
                self.scroll_sync.scroll_y = new_value
            # The gutters show other rows only once the position has moved by a whole line.
            if round(old_value) != round(new_value):
                for gutter in self.gutters:
                    gutter.refresh()
        finally:
            self._syncing = False

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        if self._syncing:
            return
        self._syncing = True
        try:
            if self.scroll_sync is not None and self.scroll_sync.scroll_offset.x != new_value:
                self.scroll_sync.scroll_x = new_value
        finally:
            self._syncing = False

    # -- selection ---------------------------------------------------------------------------------

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """The selected code. A break or a filler counts as an empty line, which keeps rows and lines in step."""
        text = "\n".join("" if row.code is None else row.code.plain for row in self.rows)
        return selection.extract(text), "\n"
