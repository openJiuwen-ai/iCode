# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""VirtualizedMarkdown — high-performance markdown widget using the Line API.

Uses virtualization (only renders visible lines) for optimal performance
with large documents.  Drop-in replacement for Textual's built-in Markdown
widget, but avoids mounting one widget per block.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote

from markdown_it import MarkdownIt
from rich._wrap import divide_line
from rich.cells import set_cell_size
from rich.segment import Segment
from rich.style import Style as RichStyle
from textual._cells import cell_len
from textual.await_complete import AwaitComplete
from textual.cache import LRUCache
from textual.content import Content, Span
from textual.dom import NoScreen
from textual.events import Click, Leave, Mount, MouseMove, Resize
from textual.geometry import Offset, Region, Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions
from textual.widget import Widget

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.support.gc_freeze import detach_lru_cache, renew_lru_cache
from chrys.app.tui.widgets import normalize_selection_rich_style as _normalize_selection_rich_style
from chrys.app.tui.widgets.markdown._utils import sanitize_location as _sanitize_location
from chrys.app.tui.widgets.markdown.anchors import find_heading
from chrys.app.tui.widgets.markdown.blocks import MarkdownBlock, MarkdownGutter, TableOfContentsType, _BlockLineInfo
from chrys.app.tui.widgets.markdown.diagram import compile_mermaid
from chrys.app.tui.widgets.markdown.diagram.messages import DiagramOpenRequested
from chrys.app.tui.widgets.markdown.diagram.model import CompiledDiagram
from chrys.app.tui.widgets.markdown.links import external_link_target
from chrys.app.tui.widgets.markdown.math import CompiledMath, compile_math
from chrys.app.tui.widgets.markdown.math.markdown import MATH_ENABLED
from chrys.app.tui.widgets.markdown.math.parser import MAX_SOURCE as MAX_MATH_SOURCE
from chrys.app.tui.widgets.markdown.parser import (
    InlineCache,
    StreamingBlockCache,
    _cell_min_width,
    _cell_natural_width,
    _cell_prefix_length,
    _cell_wrapped_height,
    _configure_markdown_parser,
    _create_markdown_parser,
    _parse_streaming_markdown,
    _parse_tokens,
    _wrap_cell_text,
    highlight_code,
)
from chrys.app.tui.widgets.markdown.stream import MarkdownStream
from chrys.foundation.i18n import msg

logger = logging.getLogger(__name__)

_DIAGRAM_OPEN = msg("tui.markdown.diagram.open", fallback="Open full diagram")
_INLINE_DIAGRAM_MAX_HEIGHT = 24
_DIAGRAM_COMPILE_CACHE_SIZE = 24
_PARSE_STALL_REPORT_SECONDS = 15.0
"""A parse still running this long logs every thread's stack once, and how long it took when it ends."""


def _strip_plain_text(strip: Strip) -> str:
    """Extract plain text from a Strip by joining segment texts."""
    return "".join(seg.text for seg in strip._segments)


def _text_height(content: Content, width: int) -> int:
    """Count default word-wrap rows without building formatted lines just to discard them."""
    key = (width, "fold", False)
    cached_key, height = content._height_cache
    if cached_key == key:
        return height

    text = content.plain
    if width <= 0 or "\t" in text or "\n" in text:
        return content.get_height({}, width)

    height = 1
    if len(text) * 2 > width and not (text.isascii() and len(text) <= width):
        height += len(divide_line(text, width))
    content._height_cache = (key, height)
    return height


def _line_ranges(text: str) -> list[tuple[int, int]]:
    """Return newline-exclusive character ranges for every source line."""
    if not text:
        return [(0, 0)]

    ranges: list[tuple[int, int]] = []
    start = 0
    while True:
        newline = text.find("\n", start)
        if newline == -1:
            ranges.append((start, len(text)))
            return ranges
        ranges.append((start, newline))
        start = newline + 1
        if start == len(text):
            ranges.append((start, start))
            return ranges


def _content_slice(content: Content, start: int, end: int, span_indices: list[int] | None = None) -> Content:
    """Return a slice of ``content`` while preserving overlapping spans."""
    text = content.plain[start:end]
    if not content.spans:
        return Content(text, strip_control_codes=False)

    spans: list[Span] = []
    source_spans = content.spans
    indices = span_indices if span_indices is not None else range(len(source_spans))
    for span_index in indices:
        span_start, span_end, style = source_spans[span_index]
        if span_end <= start:
            continue
        if span_start >= end:
            continue
        slice_start = max(span_start, start) - start
        slice_end = min(span_end, end) - start
        if slice_end > slice_start:
            spans.append(Span(slice_start, slice_end, style))
    return Content(text, spans, strip_control_codes=False)


def _span_indices_by_line(spans: Sequence[Span], line_ranges: list[tuple[int, int]]) -> list[list[int]] | None:
    """Return candidate span indices for each source line."""
    if not spans:
        return None

    line_starts = [start for start, _end in line_ranges]
    line_ends = [end for _start, end in line_ranges]
    indices_by_line: list[list[int]] = [[] for _ in line_ranges]
    for span_index, span in enumerate(spans):
        first_line = bisect_right(line_ends, span.start)
        last_line = bisect_left(line_starts, span.end)
        for line_index in range(first_line, last_line):
            indices_by_line[line_index].append(span_index)
    return indices_by_line


def _shrink_widest_columns(natural: list[int], floors: list[int], available: int) -> list[int]:
    """Fit column widths into exactly *available* cells, shrinking the widest columns first.

    Every column is capped at one shared width, the largest that fits, so a short column
    keeps its natural width while the long ones wrap; the cells the cap leaves over go to
    the capped columns one each. Shrinking every column in proportion to its natural
    width instead squeezed a three-glyph "Default" column down to two glyphs a line next
    to a long description. Requires ``sum(floors) <= available < sum(natural)``.
    """

    def total_at(cap: int) -> int:
        return sum(max(floor, min(width, cap)) for width, floor in zip(natural, floors, strict=True))

    low, high = 1, max(natural)
    while low < high:
        middle = (low + high + 1) // 2
        if total_at(middle) <= available:
            low = middle
        else:
            high = middle - 1
    widths = [max(floor, min(width, low)) for width, floor in zip(natural, floors, strict=True)]
    spare = available - sum(widths)
    for index, width in enumerate(natural):
        if spare <= 0:
            break
        if widths[index] < width:
            widths[index] += 1
            spare -= 1
    return widths


@dataclass
class _TableLayout:
    """Width and height metadata for a table block."""

    col_widths: list[int]
    header_height: int
    row_heights: list[int]
    row_starts: list[int]
    total_height: int


@dataclass
class _FenceLayout:
    """Source-line layout metadata for a fenced code block."""

    line_ranges: list[tuple[int, int]]
    line_span_indices: list[list[int]] | None
    line_starts: list[int]
    total_height: int


@dataclass(frozen=True, slots=True)
class _DiagramLayout:
    """Viewport-independent diagram metadata for one Markdown layout pass."""

    preview_height: int

    @property
    def total_height(self) -> int:
        return self.preview_height + 1


class _ParseWatch:
    """Leaves evidence of a parse that runs long: no parse of a streamed answer takes seconds.

    While one parse runs, every later update joins the parse after it, so a parse that never
    ends freezes the document; what its thread is doing is all that can explain why.
    """

    __slots__ = ("finished", "reported", "started_at", "thread_id")

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.thread_id: int | None = None
        """The executor thread running the parse; None while the parse still waits for one."""
        self.finished = False
        """Set by that thread: a busy loop can run the report after the parse ended."""
        self.reported = False

    def run(self, build: Callable[[str], list[MarkdownBlock]], markdown: str) -> list[MarkdownBlock]:
        self.thread_id = threading.get_ident()
        try:
            return build(markdown)
        finally:
            self.finished = True

    def report(self) -> None:
        if self.finished:
            return
        self.reported = True
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        if self.thread_id is None:
            where = "has not started: no executor thread took it"
        else:
            where = f"is running on thread {names.get(self.thread_id, self.thread_id)}"
        stacks = [
            f"--- {names.get(ident, ident)}{' (the parse)' if ident == self.thread_id else ''}\n"
            + "".join(traceback.format_stack(frame))
            for ident, frame in sys._current_frames().items()
        ]
        logger.warning(
            "Markdown parse %s after %.0fs; every thread's stack:\n%s",
            where,
            time.monotonic() - self.started_at,
            "\n".join(stacks),
        )

    def ended(self) -> None:
        if self.reported:
            # Quitting cancels the wait for a parse that never ends.
            logger.warning(
                "Markdown parse %s after %.1fs",
                "ended" if self.finished else "was given up unfinished",
                time.monotonic() - self.started_at,
            )


class _PendingParse:
    """The parse that ``update()``/``append()`` scheduled and that has not read the source yet.

    Requests made before it takes the widget's lock join it rather than
    scheduling a parse of their own: it parses whatever source is current once
    it holds the lock, so it installs text at least as new as every joined
    request's.
    """

    __slots__ = ("announce_contents", "task")

    def __init__(
        self,
        run: Callable[[_PendingParse], Coroutine[Any, Any, None]],
        *,
        announce_contents: bool,
    ) -> None:
        loop = asyncio.get_running_loop()
        self.announce_contents = announce_contents
        """Post ``TableOfContentsUpdated`` even without headings: an ``update()`` joined."""
        self.task: asyncio.Task[None] = loop.create_task(run(self))
        """The parse; awaiters get it shielded, so no single caller can cancel it for the others."""


class VirtualizedMarkdown(ScrollView, can_focus=True):
    """A Markdown widget that uses virtualization for efficient rendering.

    This widget parses markdown and renders it using the Line API,
    only rendering visible lines for optimal performance with large documents.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "virtualized-markdown--h1",
        "virtualized-markdown--h2",
        "virtualized-markdown--h3",
        "virtualized-markdown--h4",
        "virtualized-markdown--h5",
        "virtualized-markdown--h6",
        "virtualized-markdown--paragraph",
        "virtualized-markdown--fence",
        "virtualized-markdown--diagram",
        "virtualized-markdown--diagram-action",
        "virtualized-markdown--hr",
        "virtualized-markdown--table",
        "virtualized-markdown--table-header",
        "virtualized-markdown--block-quote",
        "virtualized-markdown--block-quote-border",
        "virtualized-markdown--bullet",
        "code_inline",
        "em",
        "strong",
        "s",
    }

    DEFAULT_CSS = """
    VirtualizedMarkdown {
        color: $foreground;
        overflow-y: auto;
        overflow-x: hidden;
        background: $surface;
        padding: 0 2 0 2;

        & > .virtualized-markdown--h1 {
            color: $markdown-h1-color;
            background: $markdown-h1-background;
            text-style: $markdown-h1-text-style;
            content-align: center middle;
        }
        & > .virtualized-markdown--h2 {
            color: $markdown-h2-color;
            background: $markdown-h2-background;
            text-style: $markdown-h2-text-style;
        }
        & > .virtualized-markdown--h3 {
            color: $markdown-h3-color;
            background: $markdown-h3-background;
            text-style: $markdown-h3-text-style;
        }
        & > .virtualized-markdown--h4 {
            color: $markdown-h4-color;
            background: $markdown-h4-background;
            text-style: $markdown-h4-text-style;
        }
        & > .virtualized-markdown--h5 {
            color: $markdown-h5-color;
            background: $markdown-h5-background;
            text-style: $markdown-h5-text-style;
        }
        & > .virtualized-markdown--h6 {
            color: $markdown-h6-color;
            background: $markdown-h6-background;
            text-style: $markdown-h6-text-style;
        }
        & > .virtualized-markdown--fence {
            background: black 10%;
            color: $text;
        }
        &:light > .virtualized-markdown--fence {
            background: white 30%;
        }
        & > .virtualized-markdown--diagram {
            background: $surface-lighten-1;
            color: $foreground;
        }
        & > .virtualized-markdown--diagram-action {
            color: $primary;
            text-style: underline;
        }
        & > .virtualized-markdown--hr {
            color: $secondary;
        }
        & > .virtualized-markdown--block-quote {
        }
        &:dark > .virtualized-markdown--block-quote-border {
            color: $text-primary 50%;
        }
        &:light > .virtualized-markdown--block-quote-border {
            color: $text-secondary;
        }
        &:dark > .virtualized-markdown--bullet {
            color: $text-primary;
        }
        &:light > .virtualized-markdown--bullet {
            color: $text-secondary;
        }
        & > .virtualized-markdown--table {
        }
        &:light > .virtualized-markdown--table {
            background: white 30%;
        }
        & > .virtualized-markdown--table-header {
            color: $primary;
            text-style: bold;
        }
        &:dark > .code_inline {
            background: $warning 10%;
            color: $text-warning 95%;
        }
        &:light > .code_inline {
            background: $error 5%;
            color: $text-error 95%;
        }
        & > .em {
            text-style: italic;
        }
        & > .strong {
            text-style: bold;
        }
        & > .s {
            text-style: strike;
        }
    }
    """

    class TableOfContentsUpdated(Message):
        """The table of contents was updated."""

        def __init__(self, markdown: VirtualizedMarkdown, table_of_contents: TableOfContentsType | None) -> None:
            super().__init__()
            self.markdown: VirtualizedMarkdown = markdown
            """The `VirtualizedMarkdown` widget associated with the table of contents."""
            self.table_of_contents: TableOfContentsType | None = table_of_contents
            """Table of contents."""

        @property
        def control(self) -> VirtualizedMarkdown:
            """The `VirtualizedMarkdown` widget associated with the table of contents."""
            return self.markdown

    class TableOfContentsSelected(Message):
        """An item in the TOC was selected."""

        def __init__(self, markdown: VirtualizedMarkdown, block_id: str) -> None:
            super().__init__()
            self.markdown: VirtualizedMarkdown = markdown
            """The `VirtualizedMarkdown` widget where the selected item is."""
            self.block_id: str = block_id
            """ID of the block that was selected."""

        @property
        def control(self) -> VirtualizedMarkdown:
            """The `VirtualizedMarkdown` widget where the selected item is."""
            return self.markdown

    class LinkClicked(Message):
        """A link in the document was clicked."""

        def __init__(self, markdown: VirtualizedMarkdown, href: str) -> None:
            super().__init__()
            self.markdown: VirtualizedMarkdown = markdown
            """The `VirtualizedMarkdown` widget containing the link clicked."""
            self.raw_href: str = href
            """The original link target, retaining percent-encoding for external opening."""
            self.href: str = unquote(href)
            """The decoded link target for local document and anchor navigation."""

        @property
        def control(self) -> VirtualizedMarkdown:
            """The `VirtualizedMarkdown` widget containing the link clicked."""
            return self.markdown

    def __init__(
        self,
        markdown: str | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        parser_factory: Callable[[], MarkdownIt] | None = None,
        open_links: bool = True,
    ):
        """A VirtualizedMarkdown widget.

        Args:
            markdown: String containing Markdown or None to leave blank for now.
            name: The name of the widget.
            id: The ID of the widget in the DOM.
            classes: The CSS classes of the widget.
            parser_factory: A factory function to return a configured MarkdownIt instance. If `None`, a "gfm-like" parser is used.
            open_links: Open external links on click and expose terminal hyperlinks. If `False`,
                only LinkClicked events and internal anchor navigation remain enabled.
        """
        super().__init__(name=name, id=id, classes=classes)
        self._initial_markdown: str | None = markdown
        self._markdown = ""
        self._source_requested = False
        """``update()`` or ``append()`` set the source: newer than the text the widget was built with."""
        self._inline_cache: InlineCache | None = None
        self._streaming_blocks: StreamingBlockCache | None = None
        self._parser_factory = parser_factory
        self._table_of_contents: TableOfContentsType | None = None
        self._open_links = open_links
        self._last_parsed_line = 0
        self._pending_parse: _PendingParse | None = None
        """The scheduled parse that has not read ``_markdown`` yet; new requests join it."""
        self._shown_source = ""
        """The source the installed blocks were parsed from."""

        # Virtualization state
        self._blocks: list[MarkdownBlock] = []
        """Parsed markdown blocks."""
        self._block_line_info: list[_BlockLineInfo] = []
        """Line information for each block."""
        self._total_lines: int = 0
        """Total virtual lines."""
        self._line_cache: LRUCache[tuple[int, int], Strip] = LRUCache(maxsize=2048)
        """Cache of rendered strips, keyed by (line_number, width)."""
        self._table_layouts: dict[int, _TableLayout] = {}
        """Layout metadata for table blocks, keyed by block index."""
        self._table_strips: LRUCache[tuple[int, int], Strip] = LRUCache(maxsize=4096)
        """Lazily-rendered table strips, keyed by (block index, content line)."""
        self._fence_layouts: dict[int, _FenceLayout] = {}
        """Source-line layout metadata for fenced code blocks, keyed by block index."""
        self._fence_line_strips: LRUCache[tuple[int, int], Strip] = LRUCache(maxsize=4096)
        """Lazily-rendered fence strips, keyed by (block index, content line)."""
        self._fence_source_line_strips: LRUCache[tuple[int, int], list[Strip]] = LRUCache(maxsize=1024)
        """Wrapped source-line strips for fence lines, keyed by (block index, source line)."""
        self._diagram_layouts: dict[int, _DiagramLayout] = {}
        """Inline preview layout metadata for diagram blocks."""
        self._diagram_action_lines: dict[int, int] = {}
        """Virtual action line to diagram block index."""
        self._diagram_action_widths: dict[int, int] = {}
        """Rendered action widths keyed by virtual line, kept in sync with cached strips."""
        self._diagram_compile_cache: OrderedDict[tuple[str, str], CompiledDiagram] = OrderedDict()
        """Small per-widget cache reused across streaming Markdown reparses."""
        self._math_compile_cache: OrderedDict[str, CompiledMath] = OrderedDict()
        self._fence_highlights: dict[tuple[str, str], Content] = {}
        """The last parse's highlighted closed fences by language and code, for a streamed reparse to reuse."""
        self._math_rows: dict[int, tuple[str, ...]] = {}
        self._block_strips: dict[int, list[Strip]] = {}
        """Cached strips for long non-table/fence blocks, keyed by block index.

        Paragraphs and similar blocks have the same cold-scroll shape as
        fences: ``Content.render_strips`` renders the entire wrapped block even
        when callers need a single row. Cache only blocks taller than the
        visible crop so short blocks still rely on the smaller per-line cache."""
        self._block_style_cache: dict[int, Style] = {}
        """Resolved per-block visual style.  Invalidated on theme / parse changes.

        ``_render_block_line`` runs once per cold row, and each call would
        otherwise walk the CSS component-style chain (``_get_block_style`` to
        ``_safe_component_style`` to ``get_visual_style``).  Memoizing per
        block index keeps that walk to at most once per block per layout."""
        self._width_at_last_layout: int = 0
        """Width when blocks were last laid out."""
        self._plain_selection_cache: tuple[list[str], list[tuple[str, int]]] | None = None
        """Cached plain text rows and inter-row separators for text selection / copy.
        Built lazily from rendered strips on first get_selection call."""
        self._frame_scroll_y: int = 0
        self._frame_width: int = 0
        self._frame_visible_height: int = 0
        self._frame_selection: object = None
        self._lines_cache_key: tuple[int, int, int, int, int, int, str, str] = (
            -1,
            -1,
            -1,
            -1,
            -1,
            -1,
            "",
            "",
        )
        self._lines_cache_result: list[Strip] | None = None

        # Seed ``virtual_size`` with a synchronous estimate so the widget
        # never reports ``(0, 0)`` between mount and ``_on_mount``'s async
        # ``update()``.  Without this, an outside reflow that lands inside
        # the executor await (e.g. ApprovalDialog dismiss on Windows) sees
        # height 0 and causes the parent ChatPanel's anchor pin to compute
        # against a transiently short ``virtual_h`` — visible to the user
        # as a chat scroll-up / scroll-to-top.  The estimate is a lower
        # bound (no wrap counted) — ``_layout_blocks`` overrides with the
        # real value once width is known.  ``set_reactive`` skips watchers
        # which is required pre-mount.
        if markdown:
            line_estimate = max(1, markdown.count("\n") + 1)
            self.set_reactive(Widget.virtual_size, Size(80, line_estimate))

    @property
    def allow_select(self) -> bool:
        """Allow text selection even though ScrollView is a container."""
        return True

    @property
    def table_of_contents(self) -> TableOfContentsType:
        """The document's table of contents."""
        if self._table_of_contents is None:
            self._table_of_contents = [
                (block.level, block.content.plain, block.block_id)
                for block in self._blocks
                if block.block_type == "heading"
            ]
        return self._table_of_contents

    @property
    def source(self) -> str:
        """The markdown source."""
        return self._markdown or ""

    @property
    def shown_source(self) -> str:
        """The source of the document installed so far: ``source`` once its parse has landed."""
        return self._shown_source

    async def _on_mount(self, _: Mount) -> None:  # ty: ignore[invalid-method-override]  # Textual dispatch awaits async handlers.
        initial_markdown = self._initial_markdown
        self._initial_markdown = None
        # An update before mount is newer than the text it was built with: installing that text would hide it.
        await self.update(self._markdown if self._source_requested else initial_markdown or "")

        if initial_markdown is None:
            self.post_message(
                VirtualizedMarkdown.TableOfContentsUpdated(self, self._table_of_contents).set_sender(self)
            )

    @classmethod
    def get_stream(cls, markdown: VirtualizedMarkdown) -> MarkdownStream:
        """Get a MarkdownStream instance.

        Args:
            markdown: A VirtualizedMarkdown widget instance.

        Returns:
            The Markdown stream object.
        """
        updater = MarkdownStream(markdown)
        updater.start()
        return updater

    def prepare_for_gc_freeze(self) -> None:
        """Detach cyclic render LRUs before the permanent generation changes."""
        # gc-freeze swaps in an acyclic capacity token; renew restores a real cache before next use.
        self._line_cache = detach_lru_cache(self._line_cache)  # ty: ignore[invalid-assignment]
        self._table_strips = detach_lru_cache(self._table_strips)  # ty: ignore[invalid-assignment]
        self._fence_line_strips = detach_lru_cache(self._fence_line_strips)  # ty: ignore[invalid-assignment]
        self._fence_source_line_strips = detach_lru_cache(self._fence_source_line_strips)  # ty: ignore[invalid-assignment]
        self._lines_cache_result = None

    def after_gc_freeze(self) -> None:
        """Recreate render LRUs after the permanent generation changes."""
        self._line_cache = renew_lru_cache(self._line_cache)
        self._table_strips = renew_lru_cache(self._table_strips)
        self._fence_line_strips = renew_lru_cache(self._fence_line_strips)
        self._fence_source_line_strips = renew_lru_cache(self._fence_source_line_strips)

    def abort_gc_freeze(self) -> None:
        """Restore any render LRUs left detached by an incomplete hook pass."""
        self.after_gc_freeze()

    def on_virtualized_markdown_link_clicked(self, event: LinkClicked) -> None:
        href = event.href
        if href.startswith("#"):
            self.goto_anchor(href[1:])
        elif self._open_links and (target := external_link_target(event.raw_href)) is not None:
            self.app.open_url(target)

    def on_click(self, event: Click) -> None:
        """Open a diagram only when its dedicated action row is clicked."""
        block_index = self._diagram_action_at(event.get_content_offset(self))
        if block_index is None:
            return
        self._request_diagram_open(block_index)
        event.prevent_default()
        event.stop()

    def _diagram_action_at(self, offset: Offset | None) -> int | None:
        """Return the diagram action under a viewport-relative content coordinate."""
        if offset is None:
            return None
        virtual_y = round(self.scroll_offset.y) + offset.y
        block_index = self._diagram_action_lines.get(virtual_y)
        if block_index is None:
            return None
        block = self._blocks[block_index]
        border_width = len(block.border_left) if block.border_left else 0
        left_offset = block.indent + block.padding_left + border_width
        content_width = (
            self._width_at_last_layout - block.indent - block.padding_left - block.padding_right - border_width - 1
        )
        if content_width <= 0:
            content_width = 1
        # Use the width recorded when the cached strip was rendered. Transcript
        # rows deliberately keep their existing language across a live locale
        # switch, so resolving the new translation here would desynchronize the
        # hitbox from the still-visible old-language action.
        action_width = self._diagram_action_widths.get(virtual_y)
        if action_width is None:
            action_text = render_str(widget_localizer(self), _DIAGRAM_OPEN.bind())
            action_width = min(content_width, cell_len(action_text))
        action_start = left_offset + content_width - action_width
        return block_index if action_start <= offset.x < action_start + action_width else None

    def on_mouse_move(self, event: MouseMove) -> None:
        pointer = "pointer" if self._diagram_action_at(event.get_content_offset(self)) is not None else "default"
        if self.styles.pointer != pointer:
            self.styles.pointer = pointer

    def on_leave(self, _event: Leave) -> None:
        if self.styles.pointer != "default":
            self.styles.pointer = "default"

    def _request_diagram_open(self, block_index: int) -> bool:
        """Post an expanded-view request for one compiled diagram block."""
        if block_index < 0 or block_index >= len(self._blocks):
            return False
        diagram = self._blocks[block_index].diagram
        if diagram is None:
            return False
        self.post_message(DiagramOpenRequested(diagram))
        return True

    @staticmethod
    def sanitize_location(location: str) -> tuple[Path, str]:
        """Given a location, break out the path and any anchor.

        Args:
            location: The location to sanitize.

        Returns:
            A tuple of the path to the location cleaned of any anchor, plus
            the anchor (or an empty string if none was found).
        """
        return _sanitize_location(location)

    def goto_anchor(self, anchor: str) -> bool:
        """Try and find the given anchor in the current document.

        Args:
            anchor: The anchor to try and find.

        Returns:
            True when the anchor was found, False otherwise.
        """
        if self._table_of_contents is None:
            return False
        found = find_heading([title for _, title, _ in self._table_of_contents], anchor)
        if found is None:
            return False
        header_id = self._table_of_contents[found][2]
        for info in self._block_line_info:
            block = self._blocks[info.block_index]
            if block.block_id == header_id:
                self.scroll_to(y=info.start_line, animate=False)
                return True
        return True

    async def load(self, path: Path) -> None:
        """Load a new Markdown document.

        Args:
            path: Path to the document.

        Raises:
            OSError: If there was some form of error loading the document.
        """
        path, anchor = self.sanitize_location(str(path))
        data = await asyncio.get_running_loop().run_in_executor(None, partial(path.read_text, encoding="utf-8"))
        await self.update(data)
        if anchor:
            self.goto_anchor(anchor)

    def set_streaming(self, streaming: bool) -> None:
        """Keep one snapshot of default-parser inlines while a message is streaming."""
        if not streaming:
            # Drop the owner. A build already running in the executor can only fill its local cache.
            self._inline_cache = None
            self._streaming_blocks = None
        elif self._inline_cache is None and self._parser_factory is None:
            self._inline_cache = {}
            # A subclass may decorate the returned blocks in place (for example,
            # a copy footer adds padding to fences). It owns those blocks alone.
            if type(self)._build_blocks is VirtualizedMarkdown._build_blocks:
                self._streaming_blocks = StreamingBlockCache()

    def on_unmount(self) -> None:
        self.set_streaming(False)

    def _build_blocks(self, markdown: str) -> list[MarkdownBlock]:
        """Parse markdown source into blocks.

        Args:
            markdown: Markdown document string.

        Returns:
            A list of MarkdownBlock objects.
        """
        parser = (
            _create_markdown_parser()
            if self._parser_factory is None
            else _configure_markdown_parser(self._parser_factory())
        )
        cache = self._inline_cache
        tokens = parser.parse(markdown) if cache is None else _parse_streaming_markdown(parser, markdown, cache)
        streaming = self._streaming_blocks
        if streaming is not None:
            context = (widget_localizer(self).effective_locale, self._open_links)
            if streaming.context != context:
                streaming.tokens = []
                streaming.boundaries = []
                streaming.context = context
        previous = self._fence_highlights
        highlights: dict[tuple[str, str], Content] = {}

        def highlight_closed_fence(code: str, language: str) -> Content:
            # A streamed reparse leaves closed fences unchanged. A fence is kept
            # under the Content's own text, so the map holds no second copy of
            # the code, and only when that text is the code itself.
            highlighted = previous.get((language, code))
            if highlighted is None:
                highlighted = highlight_code(code, language)
            if highlighted.plain == code:
                highlights[language, highlighted.plain] = highlighted
            return highlighted

        blocks = _parse_tokens(
            tokens,
            compile_diagram=self._compile_mermaid_cached,
            open_links=self._open_links,
            math_enabled=bool(parser.options.get(MATH_ENABLED, False)),
            math_compiler=self._compile_math_cached,
            highlight_closed_fence=highlight_closed_fence,
            streaming=streaming,
        )
        if streaming is not None:
            for block in blocks[: streaming.reused]:
                if block.block_type == "fence":
                    key = (block.code_language, block.content.plain)
                    if previous.get(key) is block.content:
                        highlights[key] = block.content
        self._fence_highlights = highlights
        return blocks

    def _compile_math_cached(self, source: str) -> CompiledMath:
        """Bound both successful and failed compilations across streamed reparses."""
        if len(source) > MAX_MATH_SOURCE:
            return CompiledMath(source)
        cached = self._math_compile_cache.get(source)
        if cached is not None:
            self._math_compile_cache.move_to_end(source)
            return cached
        result = compile_math(source)
        self._math_compile_cache[source] = result
        while len(self._math_compile_cache) > 64:
            self._math_compile_cache.popitem(last=False)
        return result

    def _compile_mermaid_cached(self, source: str) -> CompiledDiagram:
        """Compile one closed Mermaid fence, reusing stable streamed blocks."""
        localizer = widget_localizer(self)
        cache_key = (localizer.effective_locale, source)
        cached = self._diagram_compile_cache.get(cache_key)
        if cached is not None:
            self._diagram_compile_cache.move_to_end(cache_key)
            return cached

        diagram = compile_mermaid(source, render_message=localizer.render)
        self._diagram_compile_cache[cache_key] = diagram
        self._diagram_compile_cache.move_to_end(cache_key)
        while len(self._diagram_compile_cache) > _DIAGRAM_COMPILE_CACHE_SIZE:
            self._diagram_compile_cache.popitem(last=False)
        return diagram

    def _layout_blocks(self, width: int | None = None, *, unchanged_prefix: int = 0) -> None:
        """Measure the changed blocks, keeping same-width metadata for the identical prefix."""
        if width is None:
            width = self.scrollable_content_region.width
        if width <= 0:
            width = 80

        if width != self._width_at_last_layout:
            unchanged_prefix = 0
        self._width_at_last_layout = width
        del self._block_line_info[unchanged_prefix:]
        for layouts in (self._table_layouts, self._fence_layouts, self._diagram_layouts, self._math_rows):
            if unchanged_prefix:
                for index in tuple(layouts):
                    if index >= unchanged_prefix:
                        del layouts[index]
            else:
                layouts.clear()
        self._diagram_action_lines = {
            line: index for line, index in self._diagram_action_lines.items() if index < unchanged_prefix
        }
        self._line_cache.clear()
        self._table_strips.clear()
        self._fence_line_strips.clear()
        self._fence_source_line_strips.clear()
        self._diagram_action_widths.clear()
        self._block_strips.clear()
        self._block_style_cache.clear()

        current_line = 0
        last_bottom_margin = 0
        if self._block_line_info:
            last = self._block_line_info[-1]
            current_line = last.start_line + last.height
            last_bottom_margin = last.bottom_margin
        render_rules = self.styles.get_render_rules()

        for index in range(unchanged_prefix, len(self._blocks)):
            block = self._blocks[index]
            top_margin = max(block.top_margin, last_bottom_margin) - last_bottom_margin
            if index == 0:
                top_margin = 0

            border_width = len(block.border_left) if block.border_left else 0
            # -1 reserves the right margin char appended by _render_block_line
            content_width = width - block.indent - block.padding_left - block.padding_right - border_width - 1
            if content_width <= 0:
                content_width = 1

            if block.block_type == "hr":
                content_height = 1
            elif block.block_type == "table" and block.table_headers is not None:
                table_layout = self._build_table_layout(block, content_width)
                self._table_layouts[index] = table_layout
                content_height = table_layout.total_height
            elif block.block_type == "fence":
                fence_layout = self._build_fence_layout(block, content_width, render_rules)
                self._fence_layouts[index] = fence_layout
                content_height = fence_layout.total_height
            elif block.block_type == "diagram" and block.diagram is not None:
                preview_height = min(block.diagram.height, _INLINE_DIAGRAM_MAX_HEIGHT)
                diagram_layout = _DiagramLayout(preview_height=preview_height)
                self._diagram_layouts[index] = diagram_layout
                content_height = diagram_layout.total_height
            elif block.block_type == "math" and block.math is not None:
                if block.math.rows and block.math.width <= content_width:
                    self._math_rows[index] = block.math.rows
                    content_height = len(block.math.rows)
                else:
                    # Source fallback uses the same wrapping and clipboard
                    # mapping as ordinary text, so soft wraps never split TeX.
                    content_height = _text_height(block.content, content_width)
            else:
                content_height = _text_height(block.content, content_width)

            content_height += block.padding_top + block.padding_bottom

            total_height = top_margin + content_height + block.bottom_margin

            self._block_line_info.append(
                _BlockLineInfo(
                    block_index=index,
                    start_line=current_line,
                    height=total_height,
                    content_height=content_height,
                    top_margin=top_margin,
                    bottom_margin=block.bottom_margin,
                )
            )

            if block.block_type == "diagram" and index in self._diagram_layouts:
                action_line = (
                    current_line + top_margin + block.padding_top + self._diagram_layouts[index].preview_height
                )
                self._diagram_action_lines[action_line] = index

            current_line += total_height
            last_bottom_margin = block.bottom_margin

        self._plain_selection_cache = None
        self._lines_cache_result = None
        self._total_lines = current_line - last_bottom_margin
        self.virtual_size = Size(width, self._total_lines)
        self._refresh_scrollbars()

    def _converge_layout(self) -> None:
        """Re-layout until the scrollbar-adjusted content width is stable.

        ``_layout_blocks`` sets ``virtual_size`` and refreshes the scrollbars,
        which can toggle the vertical scrollbar and shift
        ``scrollable_content_region.width`` by one.  Laying out a single time
        would then paint strips (including cached table/block strips) sized for
        the pre-scrollbar width into the post-scrollbar viewport for one frame —
        the 1-column offset seen while resizing.  Bounded so a borderline width
        that flip-flops can't spin.
        """
        if not self._blocks:
            return
        for _ in range(3):
            width = self.scrollable_content_region.width
            if width <= 0 or self._width_at_last_layout == width:
                return
            self._layout_blocks()

    def on_resize(self, _event: Resize) -> None:
        """Eagerly update layout when the viewport width changes.

        Waiting until ``render_lines`` to discover a new width is too late for
        parent scroll/resize fast paths: they may compute dirty spans and child
        heights from the previous table wrap.  Keeping ``virtual_size`` current
        before the next paint gives Textual the right geometry to invalidate.
        """
        width = self.scrollable_content_region.width
        if width <= 0 or not self._blocks or self._width_at_last_layout == width:
            return
        self._converge_layout()
        self.refresh(layout=True)

    def _find_block_at_line(self, line: int) -> tuple[int, _BlockLineInfo] | None:
        """Find which block contains a given virtual line using binary search."""
        if not self._block_line_info:
            return None

        infos = self._block_line_info
        lo, hi = 0, len(infos) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            info = infos[mid]
            if line < info.start_line:
                hi = mid - 1
            elif line >= info.start_line + info.height:
                lo = mid + 1
            else:
                return mid, info
        return None

    def _render_block_line(self, block: MarkdownBlock, info: _BlockLineInfo, line: int, width: int) -> Strip:
        """Render a single virtual line from a block."""
        local_line = line - info.start_line

        base_style = self.visual_style
        block_style = self._get_block_style_cached(info.block_index, block)

        if local_line < info.top_margin:
            return self._render_margin_line(info.block_index - 1, info.block_index, width)

        content_end = info.top_margin + info.content_height
        if local_line >= content_end:
            return self._render_margin_line(info.block_index, info.block_index + 1, width)

        content_line = local_line - info.top_margin

        if content_line < block.padding_top:
            return self._render_padding_line(block, base_style, block_style, width)

        actual_content_end = info.content_height - block.padding_bottom
        if content_line >= actual_content_end:
            return self._render_padding_line(block, base_style, block_style, width)

        actual_line = content_line - block.padding_top

        border_width = len(block.border_left) if block.border_left else 0
        # -1 reserves the right margin char appended at the end of this method
        content_width = width - block.indent - block.padding_left - block.padding_right - border_width - 1
        if content_width <= 0:
            content_width = 1

        if block.block_type == "hr":
            strip = Strip([Segment("─" * content_width, block_style.rich_style)], content_width)
        elif block.block_type == "table" and info.block_index in self._table_layouts:
            cache_key = (info.block_index, actual_line)
            strip = self._table_strips.get(cache_key)
            if strip is None:
                strip = self._render_table_line(block, info.block_index, actual_line, content_width, block_style)
                self._table_strips[cache_key] = strip
        elif block.block_type == "fence":
            cache_key = (info.block_index, actual_line)
            strip = self._fence_line_strips.get(cache_key)
            if strip is None:
                strip = self._render_fence_line(block, info.block_index, actual_line, content_width, block_style)
                self._fence_line_strips[cache_key] = strip
        elif block.block_type == "math" and info.block_index in self._math_rows:
            rows = self._math_rows[info.block_index]
            row = rows[actual_line] if actual_line < len(rows) else ""
            strip = Strip([Segment(row, block_style.rich_style)], cell_len(row))
        elif block.block_type == "diagram" and block.diagram is not None:
            diagram_layout = self._diagram_layouts.get(info.block_index)
            if diagram_layout is None:
                strip = Strip.blank(content_width, block_style.rich_style)
            elif actual_line < diagram_layout.preview_height:
                row = block.diagram.crop_plain_row(actual_line, 0, content_width)
                strip = Strip([Segment(row, block_style.rich_style)], content_width)
            else:
                action_text = render_str(widget_localizer(self), _DIAGRAM_OPEN.bind())
                action_width = min(content_width, cell_len(action_text))
                self._diagram_action_widths[line] = action_width
                action_text = set_cell_size(action_text, action_width)
                action_component_style = self._safe_component_style("virtualized-markdown--diagram-action").rich_style
                action_style = RichStyle(
                    color=action_component_style.color,
                    bgcolor=block_style.rich_style.bgcolor,
                    underline=action_component_style.underline,
                )
                strip = Strip(
                    [
                        Segment(" " * (content_width - action_width), block_style.rich_style),
                        Segment(action_text, action_style),
                    ],
                    content_width,
                )
        else:
            strips = self._get_long_block_strips(block, info, content_width, block_style)
            if strips is None:
                strips = self._build_block_strips(block, content_width, block_style)

            if actual_line < len(strips):
                strip = strips[actual_line]
            else:
                strip = Strip.blank(content_width, block_style.rich_style)

        if block.text_align == "center":
            strip_len = strip.cell_length
            if strip_len < content_width:
                pad_left = (content_width - strip_len) // 2
                pad_right = content_width - strip_len - pad_left
                strip = Strip(
                    [
                        Segment(" " * pad_left, block_style.rich_style),
                        *strip._segments,
                        Segment(" " * pad_right, block_style.rich_style),
                    ],
                    content_width,
                )

        left_offset = block.indent + block.padding_left + border_width
        if left_offset > 0 or block.prefix:
            segments = self._render_block_gutter(block, first_line=actual_line == 0)
            if block.padding_left > 0:
                segments.append(Segment(" " * block.padding_left, block_style.rich_style))
            segments.extend(strip._segments)
            if block.padding_right > 0:
                segments.append(Segment(" " * block.padding_right, block_style.rich_style))
            strip = Strip(segments)

        pad_rich_style = block_style.rich_style
        if pad_rich_style.underline or pad_rich_style.overline or pad_rich_style.strike:
            pad_rich_style = RichStyle(
                color=pad_rich_style.color,
                bgcolor=pad_rich_style.bgcolor,
                bold=pad_rich_style.bold,
                dim=pad_rich_style.dim,
                italic=pad_rich_style.italic,
            )
        if width > 1:
            strip = strip.adjust_cell_length(width - 1, pad_rich_style)
            strip = Strip(
                [*strip._segments, Segment(" ", base_style.rich_style)],
                width,
            )
        else:
            strip = strip.extend_cell_length(width, pad_rich_style)

        return strip

    def _safe_component_style(self, *names: str) -> Style:
        """Get a visual style, falling back to base style if component styles aren't ready.

        During CSS reapplication (e.g. on focus change), ``_component_styles``
        is briefly cleared before being repopulated.  If a render is triggered
        in that window, ``get_visual_style`` raises ``KeyError``.  This helper
        catches that and returns the widget's base visual style instead.
        """
        try:
            return self.get_visual_style(*names)
        except KeyError:
            return self.visual_style

    def _get_block_style(self, block: MarkdownBlock) -> Style:
        """Get the visual style for a block."""
        if block.bq_depth > 0 and block.block_type == "paragraph":
            return self._get_bq_depth_style(block.bq_depth)
        style = self._safe_component_style(block.style_name) if block.style_name else self.visual_style
        if block.bq_depth > 0 and block.block_type not in {"diagram", "fence", "math"}:
            style = replace(style, background=self._get_bq_depth_style(block.bq_depth).background)
        return style

    def _get_block_style_cached(self, block_index: int, block: MarkdownBlock) -> Style:
        """Memoized :meth:`_get_block_style` for use in the per-row hot path.

        Cache is keyed by block index and invalidated wherever the layout or
        component-style resolution can change (``_layout_blocks``,
        ``notify_style_update``, ``update``, ``append``)."""
        cached = self._block_style_cache.get(block_index)
        if cached is not None:
            return cached
        style = self._get_block_style(block)
        self._block_style_cache[block_index] = style
        return style

    def _get_bq_depth_style(self, depth: int) -> Style:
        """Compute a Style with a compounding background for blockquote depth."""
        base_style = self._safe_component_style("virtualized-markdown--block-quote")
        base_bg = base_style.background or self.visual_style.background
        if base_bg is None:
            return base_style

        contrast = base_bg.get_contrast_text(1.0)
        blended = base_bg
        boost_factor = 0.04
        for _ in range(depth):
            blended = blended.blend(contrast, boost_factor, alpha=1.0)

        return replace(base_style, background=blended)

    def _render_gutter(self, gutter: tuple[MarkdownGutter, ...], *, first_line: bool = False) -> list[Segment]:
        """Paint containers in source order, keeping list markers inside their quotes."""
        bq_border_style = self._safe_component_style("virtualized-markdown--block-quote-border")
        segments: list[Segment] = []
        depth = 0
        style = self.visual_style.rich_style
        for part in gutter:
            if part.kind == "quote":
                depth += 1
                style = self._get_bq_depth_style(depth).rich_style
                segments.append(Segment("▌", RichStyle(color=bq_border_style.rich_style.color, bgcolor=style.bgcolor)))
                segments.append(Segment(" ", style))
            else:
                prefix = part.prefix if first_line else ""
                segments.append(Segment(" " * (part.width - cell_len(prefix)), style))
                if prefix:
                    bullet_style = self._safe_component_style("virtualized-markdown--bullet").rich_style
                    segments.append(Segment(prefix, bullet_style + RichStyle(bgcolor=style.bgcolor)))
        return segments

    def _render_block_gutter(self, block: MarkdownBlock, *, first_line: bool = False) -> list[Segment]:
        if block.gutter:
            return self._render_gutter(block.gutter, first_line=first_line)
        # External token handlers may still provide the flat block attributes.
        prefix = block.prefix if first_line else ""
        segments = [Segment(" " * max(0, block.indent - cell_len(prefix)), self.visual_style.rich_style)]
        if prefix:
            segments.append(Segment(prefix, self._safe_component_style("virtualized-markdown--bullet").rich_style))
        if block.border_left:
            segments.append(Segment(block.border_left, self._get_block_style(block).rich_style))
        return segments

    def _render_margin_line(self, previous: int, following: int, width: int) -> Strip:
        """Keep only shared container borders across the gap between two blocks."""
        shared: list[MarkdownGutter] = []
        if previous >= 0 and following < len(self._blocks):
            for left, right in zip(self._blocks[previous].gutter, self._blocks[following].gutter, strict=False):
                if left.container_id != right.container_id:
                    break
                shared.append(left)
        depth = sum(part.kind == "quote" for part in shared)
        style = self._get_bq_depth_style(depth) if depth else self.visual_style
        strip = Strip(self._render_gutter(tuple(shared)))
        strip = strip.adjust_cell_length(max(0, width - 1), style.rich_style)
        return Strip([*strip._segments, Segment(" ", self.visual_style.rich_style)], width)

    def _render_padding_line(self, block: MarkdownBlock, base_style: Style, block_style: Style, width: int) -> Strip:
        """Render a padding line (e.g. top/bottom padding of a code fence)."""
        border_width = len(block.border_left) if block.border_left else 0
        left_offset = block.indent + block.padding_left + border_width
        if left_offset > 0:
            segments = self._render_block_gutter(block)
            if block.padding_left > 0:
                segments.append(Segment(" " * block.padding_left, block_style.rich_style))
            remaining = width - left_offset
            if block.block_type in {"diagram", "fence"} and remaining > 1:
                segments.append(Segment(" " * (remaining - 1), block_style.rich_style))
                segments.append(Segment(" ", base_style.rich_style))
            elif remaining > 0:
                segments.append(Segment(" " * remaining, block_style.rich_style))
            return Strip(segments, width)
        if block.block_type in {"diagram", "fence"} and width > 1:
            return Strip(
                [
                    Segment(" " * (width - 1), block_style.rich_style),
                    Segment(" ", base_style.rich_style),
                ],
                width,
            )
        return Strip.blank(width, block_style.rich_style)

    def _build_table_layout(self, block: MarkdownBlock, content_width: int) -> _TableLayout:
        """Build table width/height metadata without rendering every row."""
        headers = block.table_headers or []
        rows = block.table_rows or []
        if not headers:
            return _TableLayout([], 0, [], [], 0)

        col_count = len(headers)
        cell_pad = 1

        overhead = col_count * (2 * cell_pad + 1) + 1
        available = content_width - overhead
        available = max(available, col_count)

        nat_widths: list[int] = []
        min_widths: list[int] = []
        # A column holding any unbreakable 2-cell cluster (CJK glyph, emoji
        # sequence, etc.) needs at least 2 cells when space allows.  ASCII-only
        # columns may shrink to 1 because they can wrap between characters.
        for header in headers:
            nat_widths.append(max(_cell_natural_width(header.plain), 1))
            min_widths.append(_cell_min_width(header.plain))
        for row in rows:
            for i, cell_content in enumerate(row):
                if i < col_count:
                    nat_widths[i] = max(nat_widths[i], _cell_natural_width(cell_content.plain))
                    min_widths[i] = max(min_widths[i], _cell_min_width(cell_content.plain))

        total_nat = sum(nat_widths)
        if total_nat <= available:
            col_widths = nat_widths[:]
        else:
            # If the double-width floors can't all fit the content area, drop
            # them to 1 so the table still fits exactly (no right-edge crop that
            # would clip borders/data); cell rendering then truncates a glyph too
            # wide for its 1-cell column.  ``available >= col_count`` (clamped
            # above), so a floor of 1 per column always fits.
            if sum(min_widths) > available:
                min_widths = [1] * col_count
            col_widths = _shrink_widest_columns(nat_widths, min_widths, available)

        header_height = max(
            (_cell_wrapped_height(header.plain, col_widths[i]) for i, header in enumerate(headers)), default=1
        )

        row_heights: list[int] = []
        for row in rows:
            height = 1
            for i in range(col_count):
                if i < len(row):
                    height = max(height, _cell_wrapped_height(row[i].plain, col_widths[i]))
            row_heights.append(height)

        row_starts: list[int] = []
        current_line = 2 + header_height
        for row_index, row_height in enumerate(row_heights):
            row_starts.append(current_line)
            current_line += row_height
            if row_index < len(row_heights) - 1:
                current_line += 1

        # top border + header + header/body separator + body rows + body separators + bottom border
        total_height = current_line + 1
        return _TableLayout(
            col_widths=col_widths,
            header_height=header_height,
            row_heights=row_heights,
            row_starts=row_starts,
            total_height=total_height,
        )

    def _table_border_style(self, block_style: Style) -> RichStyle:
        return RichStyle(
            color=block_style.rich_style.color,
            bgcolor=block_style.rich_style.bgcolor,
            dim=True,
        )

    def _render_table_border(
        self,
        layout: _TableLayout,
        *,
        left: str,
        mid: str,
        right: str,
        border_rs: RichStyle,
    ) -> Strip:
        parts: list[str] = [left]
        for i, width in enumerate(layout.col_widths):
            parts.append("\u2500" * (width + 2))
            if i < len(layout.col_widths) - 1:
                parts.append(mid)
        parts.append(right)
        return Strip([Segment("".join(parts), border_rs)])

    def _render_table_data_line(
        self,
        layout: _TableLayout,
        cells: list[Content],
        line_idx: int,
        *,
        text_style: Style,
        cell_rs: RichStyle,
        border_rs: RichStyle,
    ) -> Strip:
        segs: list[Segment] = [Segment("\u2502", border_rs)]
        for col_idx, col_width in enumerate(layout.col_widths):
            line_content = Content("")
            if col_idx < len(cells):
                content = cells[col_idx]
                cell_lines = _wrap_cell_text(content.plain, col_width)
                if line_idx < len(cell_lines):
                    start = 0
                    for previous_line in cell_lines[:line_idx]:
                        start += len(previous_line)
                        if content.plain[start : start + 1] == "\n":
                            start += 1
                    end = start + _cell_prefix_length(cell_lines[line_idx], col_width)
                    line_content = _content_slice(content, start, end)
            segs.append(Segment(" ", cell_rs))
            segs.extend(
                Segment(text, style.rich_style)
                for text, style in line_content.render(text_style, end="", parse_style=self._get_style)
                if text
            )
            padding = col_width - cell_len(line_content.plain)
            if padding:
                # Padding and separators must not inherit a cell's final link.
                segs.append(Segment(" " * padding, text_style.rich_style))
            segs.append(Segment(" ", cell_rs))
            segs.append(Segment("\u2502", border_rs))
        return Strip(segs)

    def _render_table_line(
        self,
        block: MarkdownBlock,
        block_index: int,
        actual_line: int,
        content_width: int,
        block_style: Style,
    ) -> Strip:
        """Render a single content line from a table block."""
        layout = self._table_layouts.get(block_index)
        headers = block.table_headers or []
        rows = block.table_rows or []
        if layout is None or not headers:
            return Strip.blank(content_width, block_style.rich_style)

        header_style = self._safe_component_style("virtualized-markdown--table-header")
        if block.bq_depth:
            header_style = replace(header_style, background=block_style.background)
        cell_rs = block_style.rich_style
        border_rs = self._table_border_style(block_style)

        if actual_line == 0:
            return self._render_table_border(layout, left="\u250c", mid="\u252c", right="\u2510", border_rs=border_rs)

        header_end = 1 + layout.header_height
        if actual_line < header_end:
            return self._render_table_data_line(
                layout,
                headers,
                actual_line - 1,
                text_style=header_style,
                cell_rs=cell_rs,
                border_rs=border_rs,
            )

        if actual_line == header_end:
            return self._render_table_border(layout, left="\u251c", mid="\u253c", right="\u2524", border_rs=border_rs)

        if actual_line == layout.total_height - 1:
            return self._render_table_border(layout, left="\u2514", mid="\u2534", right="\u2518", border_rs=border_rs)

        if not layout.row_starts:
            return Strip.blank(content_width, block_style.rich_style)

        row_index = bisect_right(layout.row_starts, actual_line) - 1
        if row_index < 0 or row_index >= len(rows):
            return Strip.blank(content_width, block_style.rich_style)

        row_start = layout.row_starts[row_index]
        row_height = layout.row_heights[row_index]
        row_line = actual_line - row_start
        if row_line < row_height:
            return self._render_table_data_line(
                layout,
                rows[row_index],
                row_line,
                text_style=block_style,
                cell_rs=cell_rs,
                border_rs=border_rs,
            )

        return self._render_table_border(layout, left="\u251c", mid="\u253c", right="\u2524", border_rs=border_rs)

    def _build_table_strips(self, block: MarkdownBlock, content_width: int) -> list[Strip]:
        """Build every strip for a table block.

        This compatibility helper is intentionally not used by layout; table
        rows are rendered lazily through ``_render_table_line``. It is kept so
        profiler instrumentation and any external/debug callers that still look
        up ``_build_table_strips`` by name continue to work.
        """
        layout = self._build_table_layout(block, content_width)
        if layout.total_height <= 0:
            return []
        block_index = -1
        self._table_layouts[block_index] = layout
        block_style = self._get_block_style(block)
        try:
            return [
                self._render_table_line(block, block_index, actual_line, content_width, block_style)
                for actual_line in range(layout.total_height)
            ]
        finally:
            self._table_layouts.pop(block_index, None)

    def _build_fence_layout(self, block: MarkdownBlock, content_width: int, render_rules: Any) -> _FenceLayout:
        """Build source-line heights for a fence without rendering styled strips."""
        text = block.content.plain
        line_ranges = _line_ranges(text)

        line_starts: list[int] = []
        current_line = 0
        # ``Content.get_height`` wraps each line at the content width minus
        # both line pads and splits only past it, so a tab-free line whose
        # cells fit is exactly one row. Every character is at most two cells
        # wide, which lets short lines skip even the cell count; only tabbed
        # or possibly overlong lines pay for building Content and wrapping it.
        wrap_width = content_width - render_rules.get("line_pad", 0) * 2
        for start, end in line_ranges:
            line_starts.append(current_line)
            source = text[start:end]
            if "\t" not in source and (2 * (end - start) <= wrap_width or cell_len(source) <= wrap_width):
                current_line += 1
                continue
            line = Content(source, strip_control_codes=False)
            height = max(1, line.get_height(render_rules, content_width))
            current_line += height

        return _FenceLayout(
            line_ranges=line_ranges,
            line_span_indices=_span_indices_by_line(block.content.spans, line_ranges),
            line_starts=line_starts,
            total_height=current_line,
        )

    def _render_fence_line(
        self,
        block: MarkdownBlock,
        block_index: int,
        actual_line: int,
        content_width: int,
        block_style: Style,
    ) -> Strip:
        """Render one wrapped content line from a fenced code block."""
        layout = self._fence_layouts.get(block_index)
        if layout is None:
            layout = self._build_fence_layout(block, content_width, self.styles.get_render_rules())
            self._fence_layouts[block_index] = layout
        if actual_line < 0 or actual_line >= layout.total_height or not layout.line_starts:
            return Strip.blank(content_width, block_style.rich_style)

        source_line_index = bisect_right(layout.line_starts, actual_line) - 1
        if source_line_index < 0 or source_line_index >= len(layout.line_ranges):
            return Strip.blank(content_width, block_style.rich_style)

        source_cache_key = (block_index, source_line_index)
        source_strips = self._fence_source_line_strips.get(source_cache_key)
        if source_strips is None:
            source_start, source_end = layout.line_ranges[source_line_index]
            source_line = _content_slice(
                block.content,
                source_start,
                source_end,
                None if layout.line_span_indices is None else layout.line_span_indices[source_line_index],
            )
            render_options = RenderOptions(self._get_style, self.styles.get_render_rules())
            source_strips = source_line.render_strips(
                content_width,
                None,
                block_style,
                render_options,
            )
            self._fence_source_line_strips[source_cache_key] = source_strips

        source_line_offset = actual_line - layout.line_starts[source_line_index]
        if source_line_offset < len(source_strips):
            return source_strips[source_line_offset]
        return Strip.blank(content_width, block_style.rich_style)

    def _visible_height_threshold(self) -> int:
        """Return the current viewport-sized threshold for block-strip caching."""
        if self._frame_visible_height > 0:
            return self._frame_visible_height
        if self.is_attached:
            return max(1, self.screen.size.height)
        return 40

    def _get_long_block_strips(
        self,
        block: MarkdownBlock,
        info: _BlockLineInfo,
        content_width: int,
        block_style: Style,
    ) -> list[Strip] | None:
        """Return cached strips for blocks large enough to exceed the visible crop."""
        if info.content_height <= self._visible_height_threshold():
            return None

        strips = self._block_strips.get(info.block_index)
        if strips is None:
            strips = self._build_block_strips(block, content_width, block_style)
            self._block_strips[info.block_index] = strips
        return strips

    def _build_block_strips(self, block: MarkdownBlock, content_width: int, block_style: Style) -> list[Strip]:
        """Render all strips for a non-table/fence markdown block."""
        render_options = RenderOptions(self._get_style, self.styles.get_render_rules())
        return block.content.render_strips(content_width, None, block_style, render_options)

    def _build_fence_strips(self, block: MarkdownBlock, content_width: int, block_style: Style) -> list[Strip]:
        """Render and cache every strip of a fenced code block.

        Compatibility helper for profilers/debug callers that still ask for
        the whole fence at once. Normal scrolling uses ``_render_fence_line``
        so a long offscreen fence doesn't block the UI thread when its first
        row becomes visible.
        """
        render_options = RenderOptions(self._get_style, self.styles.get_render_rules())
        return block.content.render_strips(content_width, None, block_style, render_options)

    def render_line(self, y: int) -> Strip:
        """Render a line of content for the Line API."""
        if not self.is_attached:
            return Strip.blank(max(0, self.size.width)).apply_offsets(0, y)

        self._converge_layout()
        width = self.scrollable_content_region.width
        if width <= 0:
            return Strip.blank(0)

        line_number = round(self.scroll_offset.y) + y

        cache_key = (line_number, width)
        cached = self._line_cache.get(cache_key)
        if cached is not None:
            strip = cached
        elif line_number >= self._total_lines or line_number < 0:
            strip = Strip.blank(width, self.visual_style.rich_style)
            self._line_cache[cache_key] = strip
        else:
            result = self._find_block_at_line(line_number)
            if result is None:
                strip = Strip.blank(width, self.visual_style.rich_style)
            else:
                _block_idx, info = result
                block = self._blocks[info.block_index]
                strip = self._render_block_line(block, info, line_number, width)
            self._line_cache[cache_key] = strip

        # Apply selection highlight (post-cache so base strip stays clean)
        selection = self._safe_text_selection()
        if selection is not None:
            span = selection.get_span(line_number)
            if span is not None:
                strip = self._apply_selection_highlight(strip, span)

        # Embed offset metadata for selection coordinate resolution after
        # highlighting. Highlighting splits segments, and same-row selection
        # hit-testing needs each split segment to carry its own row offset.
        strip = strip.apply_offsets(0, line_number)

        return strip

    def _safe_text_selection(self) -> Selection | None:
        try:
            return self.text_selection
        except NoScreen:
            return None

    def _apply_selection_highlight(self, strip: Strip, span: tuple[int, int]) -> Strip:
        """Apply selection highlight style to a strip.

        Args:
            strip: The strip to highlight.
            span: Tuple of (start_x, end_x) character offsets. end_x=-1 means end of line.
        """
        start_x, end_x = span
        selection_style = _normalize_selection_rich_style(self.screen.get_component_rich_style("screen--selection"))
        segments: list[Segment] = []
        pos = 0
        for seg in strip._segments:
            text, style, control = seg
            seg_start = pos
            seg_end = pos + len(text)
            actual_end = seg_end if end_x == -1 else min(seg_end, end_x)

            if actual_end > seg_start and start_x < seg_end and seg_start < (seg_end if end_x == -1 else end_x):
                # This segment overlaps with selection
                sel_start = max(0, start_x - seg_start)
                sel_end = len(text) if end_x == -1 else min(len(text), end_x - seg_start)

                if sel_start > 0:
                    segments.append(Segment(text[:sel_start], style, control))
                combined = style + selection_style if style else selection_style
                segments.append(Segment(text[sel_start:sel_end], combined, control))
                if sel_end < len(text):
                    segments.append(Segment(text[sel_end:], style, control))
            else:
                segments.append(seg)
            pos = seg_end
        return Strip(segments, strip.cell_length)

    def notify_style_update(self) -> None:
        """Clear caches when theme or styles change so strips are re-rendered."""
        self._line_cache.clear()
        self._table_layouts.clear()
        self._table_strips.clear()
        self._fence_layouts.clear()
        self._fence_line_strips.clear()
        self._fence_source_line_strips.clear()
        self._block_strips.clear()
        self._block_style_cache.clear()
        self._plain_selection_cache = None
        self._width_at_last_layout = 0  # force _layout_blocks on next render
        self._lines_cache_result = None
        super().notify_style_update()

    def render_lines(self, crop: Region) -> list[Strip]:
        """Render visible lines."""
        if not self.is_attached:
            return [Strip.blank(crop.width) for _ in crop.line_range]

        self._converge_layout()

        width = self.scrollable_content_region.width
        if width <= 0:
            return [Strip.blank(crop.width)] * crop.height

        self._frame_scroll_y = round(self.scroll_offset.y)
        self._frame_width = width
        self._frame_visible_height = crop.height
        self._frame_selection = self._safe_text_selection()

        if self._can_render_lines_direct(crop):
            hover_link_style = self._hover_link_style()
            lines_key = (
                self._frame_scroll_y,
                width,
                crop.x,
                crop.y,
                crop.width,
                crop.height,
                self.app.theme,
                str(self.app.console.color_system),
            )
            cacheable = self._frame_selection is None and hover_link_style is None
            if (
                cacheable
                and lines_key == self._lines_cache_key
                and self._lines_cache_result is not None
                and crop.height == len(self._lines_cache_result)
            ):
                return self._lines_cache_result

            result = self._render_lines_direct(crop, hover_link_style)
            if cacheable:
                self._lines_cache_key = lines_key
                self._lines_cache_result = result
            else:
                self._lines_cache_result = None
            return result

        self._lines_cache_result = None
        return super().render_lines(crop)

    def _can_render_lines_direct(self, crop: Region) -> bool:
        """Return true when the chat markdown direct renderer preserves widget styling."""
        styles = self.styles
        if styles.gutter:
            return False
        if any(edge for edge, _color in styles.outline):
            return False
        if styles.has_rule("hatch") and styles.hatch != "none":
            return False
        if styles.tint.a:
            return False
        if styles.background_tint.a:
            return False
        if styles.text_opacity != 1.0 or self.opacity != 1.0:
            return False
        if styles.line_pad:
            return False
        if styles.text_align not in {"start", "left"}:
            return False
        if self.show_vertical_scrollbar or self.show_horizontal_scrollbar:
            return False
        return crop.width > 0 and self.content_region.width > 0

    def _hover_link_style(self) -> RichStyle | None:
        """Return link hover style only when Textual would apply it."""
        hover_style = self.hover_style
        # Textual exposes no public predicate for clickable hover links, so
        # mirror StylesCache.render_widget's internal metadata checks here.
        if not self.auto_links or not hover_style._link_id or not hover_style._meta:
            return None
        if "@click" not in hover_style.meta:
            return None
        return self.link_style_hover

    def _render_lines_direct(self, crop: Region, hover_link_style: RichStyle | None) -> list[Strip]:
        """Render clipped markdown rows without ``StylesCache.render``.

        Chat message markdown has no padding, borders, filters, or internal
        scrollbars. In that case ``render_line`` already returns the final
        strip, and the generic style wrapper only adds per-row bookkeeping
        during parent chat scrolling.
        """
        content_width = self.content_region.width
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
            if hover_link_style:
                strip = strip.style_links(self.hover_style.link_id, hover_link_style)
            if crop.column_span != (0, content_width):
                strip = strip.crop(crop.x, crop.x + crop.width)
            strips.append(strip)
        return strips

    def get_content_width(self, container: Size, viewport: Size) -> int:
        return self.virtual_size.width

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        if self._blocks and width > 0 and self._width_at_last_layout != width:
            self._layout_blocks(width)
        return self.virtual_size.height

    def update(self, markdown: str) -> AwaitComplete:
        """Update the document with new Markdown.

        Updates coalesce: until the scheduled parse starts, further
        ``update()``/``append()`` calls join it, and it parses the source as it
        stands when it starts. A burst of cumulative streamed updates therefore
        costs one parse and one layout, not one per call.

        Args:
            markdown: A string containing Markdown.

        Returns:
            An optionally awaitable object that resolves once this markdown, or
            newer, is installed. Cancelling one awaiter leaves the shared parse
            running for the others.
        """
        self._markdown = markdown
        return self._request_parse(announce_contents=True)

    def append(self, markdown: str) -> AwaitComplete:
        """Append markdown to the document.

        Coalesces with pending updates as ``update()`` does.

        Args:
            markdown: A fragment of markdown to be appended.

        Returns:
            An optionally awaitable object that resolves once the document with
            this fragment, or newer, is installed.
        """
        # Before mount, and before any update, the document is the text it was built with.
        base = self.source if self._source_requested else self._initial_markdown or ""
        self._markdown = base + markdown
        return self._request_parse(announce_contents=False)

    def _request_parse(self, *, announce_contents: bool) -> AwaitComplete:
        """Join the pending parse of ``_markdown``, or schedule one.

        ``TableOfContentsUpdated`` follows the parse when any joined request
        set ``announce_contents`` (every ``update()`` does) or when the parsed
        document has headings, so a group of ``append()`` calls announces only
        what one of them alone would have.
        """
        self._table_of_contents = None
        self._source_requested = True
        pending = self._pending_parse
        # A pending task is done here only when it was cancelled before it read
        # the source (loop shutdown); it can no longer parse, so it is not joinable.
        if pending is not None and not pending.task.done():
            pending.announce_contents = pending.announce_contents or announce_contents
            return AwaitComplete(asyncio.shield(pending.task))

        # The render caches stay until the new blocks are installed: the old
        # blocks still paint meanwhile, and a table without its layout paints blank.
        pending = _PendingParse(self._run_parse, announce_contents=announce_contents)
        self._pending_parse = pending
        return AwaitComplete(asyncio.shield(pending.task))

    async def _run_parse(self, request: _PendingParse) -> None:
        """Parse and install the current source once the lock is free."""
        async with self.lock:
            # Reading the source ends the join window: a later request is
            # newer than this parse's text and schedules a parse of its own.
            if self._pending_parse is request:
                self._pending_parse = None
            markdown = self._markdown
            loop = asyncio.get_running_loop()
            watch = _ParseWatch()
            report = loop.call_later(_PARSE_STALL_REPORT_SECONDS, watch.report)
            try:
                blocks = await loop.run_in_executor(None, watch.run, self._build_blocks, markdown)
            finally:
                report.cancel()
                watch.ended()
            self._install_blocks(blocks)
            self._shown_source = markdown

            lines = markdown.splitlines()
            self._last_parsed_line = len(lines) - (1 if lines and lines[-1] else 0)
            self.refresh()
            if request.announce_contents or any(block.block_type == "heading" for block in blocks):
                self.post_message(
                    VirtualizedMarkdown.TableOfContentsUpdated(self, self.table_of_contents).set_sender(self)
                )

    def _install_blocks(self, blocks: list[MarkdownBlock]) -> None:
        """Commit freshly parsed blocks and lay them out.

        The heading list is dropped here as well as when the update starts: an
        earlier update still holding the lock rebuilds it from its own blocks
        after this update's start cleared it, and ``_layout_blocks`` does not
        touch it.

        A widget that has never been arranged (just mounted, or mounted inside
        a hidden container) has no content width yet. Laying out now would use
        the placeholder width that the first arrange immediately lays out
        again, so the layout waits for the real width instead:
        ``get_content_height`` supplies it on the first arrange, and
        ``on_resize`` and ``render_line`` converge on it otherwise. Those skip
        a widget without blocks, so an empty document, zero lines at any
        width, is laid out at once.
        """
        unchanged_prefix = 0
        for old, new in zip(self._blocks, blocks, strict=False):
            if old is not new:
                break
            unchanged_prefix += 1
        self._blocks = blocks
        self._table_of_contents = None
        if blocks and self.scrollable_content_region.width <= 0:
            self._defer_layout_until_arranged()
            return
        if unchanged_prefix:
            self._layout_blocks(unchanged_prefix=min(unchanged_prefix, len(self._block_line_info)))
        else:
            self._layout_blocks()

    def _defer_layout_until_arranged(self) -> None:
        """Drop the layout of replaced blocks and request a measuring pass.

        Everything cleared here indexes blocks by position, so none of it may
        outlive the blocks it was computed from. ``virtual_size`` keeps its
        last value (the constructor's line estimate for a fresh widget), so
        the parent never measures a zero-height transcript row meanwhile.
        """
        self._width_at_last_layout = 0
        self._total_lines = 0
        self._block_line_info.clear()
        self._line_cache.clear()
        self._table_layouts.clear()
        self._table_strips.clear()
        self._fence_layouts.clear()
        self._fence_line_strips.clear()
        self._fence_source_line_strips.clear()
        self._block_strips.clear()
        self._block_style_cache.clear()
        self._diagram_action_lines.clear()
        self._diagram_action_widths.clear()
        self._plain_selection_cache = None
        self._lines_cache_result = None
        self.refresh(layout=True)

    def scroll_to_block_id(self, block_id: str) -> None:
        """Scroll to a block by its ID.

        Args:
            block_id: The block ID to scroll to.
        """
        for info in self._block_line_info:
            block = self._blocks[info.block_index]
            if block.block_id == block_id:
                self.scroll_to(y=info.start_line, animate=False)
                return

    def selection_updated(self, selection: Selection | None) -> None:
        """Refresh display when selection changes (highlight is post-cache)."""
        self._plain_selection_cache = None
        self._lines_cache_result = None
        self.refresh()

    def _build_plain_selection_lines(self) -> tuple[list[str], list[tuple[str, int]]]:
        """Build plain text rows and copy separators from rendered strips.

        Renders every virtual line (using cache where available) and
        extracts plain text.  This ensures indentation, prefixes, borders,
        padding, and word-wrapping match the screen exactly.

        The companion separator list records how adjacent rendered rows should
        be joined when copied. Textual selection is visual-row based, so a
        soft-wrapped paragraph row would otherwise become a hard ``\n`` in the
        clipboard. Markdown source line ends still copy as newlines.
        """
        width = self._width_at_last_layout
        if width <= 0 or self._total_lines == 0:
            return [], []

        lines: list[str] = []
        formatted_cache: dict[int, list[Any]] = {}
        for y in range(self._total_lines):
            cache_key = (y, width)
            cached = self._line_cache.get(cache_key)
            if cached is None:
                result = self._find_block_at_line(y)
                if result is None:
                    cached = Strip.blank(width, self.visual_style.rich_style)
                else:
                    _, info = result
                    block = self._blocks[info.block_index]
                    cached = self._render_block_line(block, info, y, width)
                self._line_cache[cache_key] = cached
            lines.append(_strip_plain_text(cached).rstrip())
        separators = [
            self._selection_separator_after_line(y, width, formatted_cache, lines[y + 1])
            for y in range(self._total_lines - 1)
        ]
        return lines, separators

    def _selection_separator_after_line(
        self,
        line: int,
        width: int,
        formatted_cache: dict[int, list[Any]],
        following_visual_line: str,
    ) -> tuple[str, int]:
        """Return the clipboard separator and next-row prefix skip after a rendered virtual line."""
        current = self._find_block_at_line(line)
        following = self._find_block_at_line(line + 1)
        if current is None or following is None:
            return "\n", 0

        _current_index, current_info = current
        _following_index, following_info = following
        if current_info.block_index != following_info.block_index:
            return "\n", 0

        block = self._blocks[current_info.block_index]
        if block.block_type in {"diagram", "hr", "table"} or current_info.block_index in self._math_rows:
            return "\n", 0

        current_content_line = self._actual_content_line_index(block, current_info, line)
        following_content_line = self._actual_content_line_index(block, following_info, line + 1)
        if current_content_line is None or following_content_line is None:
            return "\n", 0

        formatted_lines = self._formatted_lines_for_selection(block, current_info, width, formatted_cache)
        if current_content_line >= len(formatted_lines) or following_content_line >= len(formatted_lines):
            return "\n", 0

        current_formatted = formatted_lines[current_content_line]
        following_formatted = formatted_lines[following_content_line]
        if current_formatted.line_end or current_formatted.y != following_formatted.y:
            return "\n", 0
        separator = self._soft_wrap_separator(block, current_formatted, following_formatted)
        return separator, self._soft_wrap_prefix_skip(following_formatted, following_visual_line)

    def _actual_content_line_index(
        self,
        block: MarkdownBlock,
        info: _BlockLineInfo,
        line: int,
    ) -> int | None:
        """Map a virtual line to the block content line, excluding margins/padding."""
        local_line = line - info.start_line
        if local_line < info.top_margin:
            return None

        content_end = info.top_margin + info.content_height
        if local_line >= content_end:
            return None

        content_line = local_line - info.top_margin
        if content_line < block.padding_top:
            return None

        actual_content_end = info.content_height - block.padding_bottom
        if content_line >= actual_content_end:
            return None

        return content_line - block.padding_top

    def _formatted_lines_for_selection(
        self,
        block: MarkdownBlock,
        info: _BlockLineInfo,
        width: int,
        formatted_cache: dict[int, list[Any]],
    ) -> list[Any]:
        """Return Textual formatted content lines for source-line boundary checks."""
        cached = formatted_cache.get(info.block_index)
        if cached is not None:
            return cached

        content_width = self._content_width_for_block(block, width)
        rules = self.styles.get_render_rules()
        get_rule = rules.get
        lines = block.content._wrap_and_format(
            content_width,
            align=get_rule("text_align", "left"),
            overflow=get_rule("text_overflow", "fold"),
            no_wrap=get_rule("text_wrap", "wrap") == "nowrap",
            line_pad=get_rule("line_pad", 0),
            tab_size=8,
            get_style=self._get_style,
        )
        formatted_cache[info.block_index] = lines
        return lines

    def _content_width_for_block(self, block: MarkdownBlock, width: int) -> int:
        """Return the content cell width used by ``_render_block_line``."""
        border_width = len(block.border_left) if block.border_left else 0
        content_width = width - block.indent - block.padding_left - block.padding_right - border_width - 1
        return max(1, content_width)

    @staticmethod
    def _soft_wrap_separator(block: MarkdownBlock, current_line: Any, following_line: Any) -> str:
        """Return source whitespace removed between two visual wrap rows."""
        source_lines = block.content.plain.split("\n")
        source_line_index = current_line.y
        if source_line_index < 0 or source_line_index >= len(source_lines):
            return " "

        source = source_lines[source_line_index]
        source_offsets = current_line.source_offsets
        if source_offsets is not None:
            rendered_end = min(len(current_line.plain), len(source_offsets) - 1)
            current_source_end = source_offsets[rendered_end]
        else:
            current_source_end = current_line.x + len(current_line.plain)
        following_source_offsets = following_line.source_offsets
        following_source_start = (
            following_source_offsets[0] if following_source_offsets is not None else following_line.x
        )
        return source[current_source_end:following_source_start]

    @staticmethod
    def _soft_wrap_prefix_skip(following_line: Any, following_visual_line: str) -> int:
        """Return visual-only prefix length to drop from a soft-wrapped continuation row."""
        content_text = following_line.plain.rstrip()
        if not content_text or not following_visual_line.endswith(content_text):
            return 0
        return len(following_visual_line) - len(content_text)

    def _extract_selection_from_plain_lines(
        self,
        selection: Selection,
        lines: list[str],
        separators: list[tuple[str, int]],
    ) -> str:
        """Extract selected text while honoring per-row clipboard separators."""
        if not lines:
            return ""

        if selection.start is None:
            start_line_index = 0
            start_offset = 0
        else:
            start_line_index, start_offset = selection.start.transpose

        if selection.end is None:
            end_line_index = len(lines) - 1
            end_offset = len(lines[-1])
        else:
            end_line_index, end_offset = selection.end.transpose

        if start_line_index < 0 or start_line_index >= len(lines):
            return ""
        end_line_index = min(len(lines) - 1, end_line_index)

        if start_line_index == end_line_index:
            return lines[start_line_index][start_offset:end_offset]

        chunks: list[str] = []
        for line_index in range(start_line_index, end_line_index + 1):
            line = lines[line_index]
            start = start_offset if line_index == start_line_index else 0
            end = end_offset if line_index == end_line_index else len(line)
            if line_index > start_line_index:
                _separator, prefix_skip = separators[line_index - 1]
                if start == 0:
                    start = prefix_skip
            end = max(start, end)
            chunks.append(line[start:end])
            if line_index < end_line_index:
                separator, _prefix_skip = separators[line_index]
                chunks.append(separator)
        return "".join(chunks)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract selected text from rendered strips.

        Builds plain text from the actual rendered strips so that character
        positions (indentation, prefixes, borders, wrapping) match the
        screen layout exactly.
        """
        if self._total_lines == 0:
            return None
        if self._plain_selection_cache is None:
            self._plain_selection_cache = self._build_plain_selection_lines()
        lines, separators = self._plain_selection_cache
        extracted = self._extract_selection_from_plain_lines(selection, lines, separators)
        if not extracted:
            return None
        return extracted, "\n"

    async def action_link(self, href: str) -> None:
        """Called on link click."""
        self.post_message(VirtualizedMarkdown.LinkClicked(self, href))
