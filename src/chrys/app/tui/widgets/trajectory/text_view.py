# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The trajectory dashboard's text view: a virtualized, cell-width-aware line
surface the Overview, Timeline and Insights pages render into."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len
from textual import events
from textual.cache import LRUCache
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip

from chrys.app.tui.support.gc_freeze import DetachedLruCache, detach_lru_cache, renew_lru_cache

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.text import Text


class TrajectoryTextView(ScrollView):
    """Virtualized, cell-width-aware line surface for dashboard presentations."""

    can_focus = True

    DEFAULT_CSS = """
    TrajectoryTextView {
        height: 1fr;
        overflow: auto auto;
        scrollbar-size: 1 1;
    }
    TrajectoryTextView.-overview {
        overflow-x: hidden;
    }
    /* Overview and Insights pad their content inside section boxes; the
       Timeline has none, so the view keeps it off the dashboard border. The
       right gap is left by the page itself (the dashboard's _right_gap), so a
       vertical scrollbar stays flush against the border. */
    TrajectoryTextView.-timeline {
        padding-left: 1;
    }
    """

    def __init__(
        self,
        *,
        on_resized: Callable[[], None],
        open_session_folder: Callable[[], None],
        copy_session_path: Callable[[], None],
    ) -> None:
        super().__init__()
        self._on_resized = on_resized
        self._open_session_folder = open_session_folder
        self._copy_session_path = copy_session_path
        self._lines: list[Text] = []
        self._width = 0
        self._strips: LRUCache[tuple[int, int, int], Strip] | DetachedLruCache = LRUCache(maxsize=500)

    def set_lines(self, lines: list[Text], *, reset_scroll: bool = True, min_width: int = 0) -> None:
        """Show *lines*, scrolling over at least *min_width* columns."""
        old_x = self.scroll_offset.x
        old_y = self.scroll_offset.y
        self._lines = lines
        self._width = max(max((cell_len(line.plain) for line in lines), default=0), min_width)
        if not isinstance(self._strips, DetachedLruCache):
            self._strips.clear()
        self.virtual_size = Size(self._width, len(lines))
        if reset_scroll:
            self.scroll_home(animate=False)
        else:
            self.scroll_to(x=old_x, y=old_y, animate=False, force=True, immediate=True)
        self.refresh(layout=True)

    def clear_lines(self) -> None:
        self.set_lines([])

    def release(self) -> None:
        """Release content and the render LRU at the dashboard ownership boundary."""
        self.clear_lines()
        self._strips = LRUCache(maxsize=500)

    def detach_cache(self) -> None:
        self._strips = detach_lru_cache(self._strips)

    def renew_cache(self) -> None:
        self._strips = renew_lru_cache(self._strips)

    def notify_style_update(self) -> None:
        """Discard strips that contain resolved theme styles."""
        super().notify_style_update()
        if not isinstance(self._strips, DetachedLruCache):
            self._strips.clear()

    def on_resize(self, _event: events.Resize) -> None:
        """Sibling chrome (the turn tab strip) resizes this view only after
        lines commit; the owner rebuilds them for the settled region."""
        self._on_resized()

    def action_open_session_folder(self) -> None:
        """``@click`` target of the session info section's folder control."""
        self._open_session_folder()

    def action_copy_session_path(self) -> None:
        """``@click`` target of the session info section's copy control."""
        self._copy_session_path()

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        scroll_x = round(self.scroll_offset.x)
        absolute_line = y + round(self.scroll_offset.y)
        widget_style = self.visual_style.rich_style
        if absolute_line < 0 or absolute_line >= len(self._lines):
            return Strip.blank(width, widget_style)
        key = (absolute_line, scroll_x, width)
        cache = self._strips
        if isinstance(cache, DetachedLruCache):
            cache = renew_lru_cache(cache)
            self._strips = cache
        cached = cache.get(key)
        if cached is not None:
            return cached
        line = self._lines[absolute_line]
        strip = Strip(list(line.render(self.app.console)), cell_len(line.plain))
        rendered = strip.crop(scroll_x, scroll_x + width).adjust_cell_length(width)
        line_style = self.app.console.get_style(line.style)
        rendered = rendered.apply_style(widget_style + line_style)
        cache[key] = rendered
        return rendered
