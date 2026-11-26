# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CopyableMarkdown — VirtualizedMarkdown with copy buttons on code fences.

Each fence block gets a clickable "copy" button rendered in its bottom
padding. Clicking the button copies the raw code to the clipboard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from rich.segment import Segment, cell_len
from rich.style import Style as RichStyle
from textual.events import Click
from textual.strip import Strip

from chrys.app.tui.clipboard import copy_text_to_clipboards, notify_after_copy
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets.markdown.blocks import MarkdownBlock, _BlockLineInfo
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.i18n import MessageRef, msg

if TYPE_CHECKING:
    pass

_COPY_BUTTON_RIGHT_MARGIN = 4
_CODE_BLOCK_COPIED = msg(
    "tui.markdown.copy.code_block",
    fallback="Code block copied to clipboard",
)
_CODE_BLOCK_COPIED_REF = _CODE_BLOCK_COPIED.bind()
_COPIED_TITLE_REF = COPIED_TITLE.bind()


class CopyableMarkdown(VirtualizedMarkdown):
    """Markdown widget with copyable code blocks.

    Each fence block has a copy button displayed below the code.
    Clicking the button copies the code to the clipboard.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = VirtualizedMarkdown.COMPONENT_CLASSES | {
        "markdown--copy-button",
    }

    DEFAULT_CSS = """
    CopyableMarkdown {
        background: transparent;

        & > .markdown--copy-button {
            background: transparent;
            text-style: underline;
            color: $text-muted;
        }
    }
    """

    def __init__(
        self,
        markdown: str | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        parser_factory: Callable | None = None,
        open_links: bool = True,
        copy_button_text: str = "📋 Copy",
        copy_success_text: MessageRef | str = _CODE_BLOCK_COPIED_REF,
        copy_success_title: MessageRef | str = _COPIED_TITLE_REF,
    ):
        super().__init__(
            markdown,
            name=name,
            id=id,
            classes=classes,
            parser_factory=parser_factory,
            open_links=open_links,
        )
        self._copy_button_text = copy_button_text
        self._copy_success_text = copy_success_text
        self._copy_success_title = copy_success_title
        self._copy_button_lines: dict[int, int] = {}
        """Mapping from virtual line number (copy button line) to block index."""

    def _build_blocks(self, markdown: str) -> list[MarkdownBlock]:
        blocks = super()._build_blocks(markdown)
        for block in blocks:
            if block.block_type == "fence":
                # A footer belongs to the block, even when it ends the document.
                block.padding_bottom += 1
        return blocks

    def _layout_blocks(self, width: int | None = None, *, unchanged_prefix: int = 0) -> None:
        super()._layout_blocks(width, unchanged_prefix=unchanged_prefix)

        # Build copy-button-line → block-index mapping.
        self._copy_button_lines.clear()
        for idx, block in enumerate(self._blocks):
            if block.block_type == "fence":
                info = self._block_line_info[idx]
                copy_line = info.start_line + info.top_margin + info.content_height - 1
                self._copy_button_lines[copy_line] = idx

    def _render_block_line(self, block: MarkdownBlock, info: _BlockLineInfo, line: int, width: int) -> Strip:
        if line in self._copy_button_lines:
            return self._render_copy_button(block, width)
        return super()._render_block_line(block, info, line, width)

    def _copy_button_region(self, block: MarkdownBlock, width: int) -> tuple[int, int]:
        """Use the same clipped button bounds for painting and hit testing."""
        gutter_width = block.indent + len(block.border_left)
        end = max(gutter_width, width - _COPY_BUTTON_RIGHT_MARGIN)
        start = max(gutter_width, end - cell_len(self._copy_button_text))
        return min(start, width), min(end, width)

    def _render_copy_button(self, block: MarkdownBlock, width: int) -> Strip:
        copy_text = self._copy_button_text
        button_style = self._safe_component_style("markdown--copy-button").rich_style
        style = self._get_bq_depth_style(block.bq_depth) if block.bq_depth else self.visual_style
        start, end = self._copy_button_region(block, width)
        gutter = Strip(self._render_block_gutter(block)).adjust_cell_length(start, style.rich_style)
        button = Strip([Segment(copy_text, button_style + RichStyle(bgcolor=style.rich_style.bgcolor))]).crop(
            0, end - start
        )
        strip = Strip([*gutter._segments, *button._segments]).adjust_cell_length(max(0, width - 1), style.rich_style)
        return Strip([*strip._segments, Segment(" ", self.visual_style.rich_style)], width)

    def on_click(self, event: Click) -> None:
        offset = event.get_content_offset(self)
        if offset is None:
            return
        virtual_y = offset.y + self.scroll_offset.y

        if virtual_y not in self._copy_button_lines:
            super().on_click(event)
            return

        block_index = self._copy_button_lines[virtual_y]
        block = self._blocks[block_index]

        # Hit-test: is the click within the button text?
        width = self.scrollable_content_region.width

        button_start, button_end = self._copy_button_region(block, width)

        if button_start <= offset.x < button_end:
            code = block.content.plain
            dispatched = copy_text_to_clipboards(self.app, code)
            notify_after_copy(
                self,
                self._render_toast(self._copy_success_text),
                dispatched=dispatched,
                title=self._render_toast(self._copy_success_title),
                markup=False,
            )
            event.prevent_default()
            event.stop()

    def _render_toast(self, message: MessageRef | str) -> str:
        if isinstance(message, str):
            return message
        return render_str(widget_localizer(self), message)
