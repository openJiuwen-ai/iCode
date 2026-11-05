# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A row of chat images, drawn by the terminal where it can and as half-blocks everywhere else.

The cell layout is the half-block one whatever draws the images, so a terminal image arriving, or a
modal turning one back into half-blocks, never moves anything. See ``app/tui/graphics/manager.py``
for how each protocol draws.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.segment import Segment
from rich.style import Style
from textual.filter import ANSIToTruecolor
from textual.strip import Strip
from textual.widget import Widget

from chrys.app.tui.graphics import kitty
from chrys.app.tui.graphics.detect import GraphicsProtocol
from chrys.app.tui.graphics.manager import ImageSlot, graphics_for
from chrys.app.tui.widgets.chat.image_preview import (
    DEFAULT_MAX_ROWS,
    ChatImagePreview,
    ImageBox,
    half_block_rows,
    layout_image_row,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from textual.geometry import Size
    from textual.selection import Selection


class TerminalImages(Widget):
    """Images side by side, each fitted to at most ``max_rows`` rows."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    TerminalImages {
        height: auto;
        background: transparent;
    }
    """

    def __init__(
        self,
        previews: Sequence[ChatImagePreview],
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        allow_upscale: bool = False,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._max_rows = max_rows
        self._allow_upscale = allow_upscale
        self._slots = tuple(ImageSlot(preview) for preview in previews)
        self._layout: tuple[int, tuple[ImageBox, ...]] = (-1, ())
        self._half_blocks: dict[tuple[int, ImageBox], list[list[Segment]]] = {}

    @property
    def slots(self) -> tuple[ImageSlot, ...]:
        """One per image, in order."""
        return self._slots

    @property
    def previews(self) -> tuple[ChatImagePreview, ...]:
        """The images shown."""
        return tuple(slot.preview for slot in self._slots)

    def set_previews(self, previews: Sequence[ChatImagePreview]) -> None:
        """Show other images; what the terminal holds for the old ones is freed.

        The same images again, as each progress update of a tool brings them, keep what the terminal
        holds: freeing it would show half-blocks until the images were sent again.
        """
        if [preview.data_uri for preview in previews] == [slot.preview.data_uri for slot in self._slots]:
            return
        graphics = graphics_for(self.app)
        if graphics is not None:
            for slot in self._slots:
                graphics.release(slot)
        self._slots = tuple(ImageSlot(preview) for preview in previews)
        self._layout = (-1, ())
        self._half_blocks.clear()
        self.refresh(layout=True)

    def image_cells(self, width: int) -> list[tuple[ImageSlot, ImageBox]]:
        """Each image and its cell box at a content ``width``."""
        return list(zip(self._slots, self._boxes(width), strict=True))

    def _boxes(self, width: int) -> tuple[ImageBox, ...]:
        cached_width, boxes = self._layout
        if cached_width != width:
            boxes = layout_image_row(
                self.previews, max_width=width, max_rows=self._max_rows, allow_upscale=self._allow_upscale
            )
            self._layout = (width, boxes)
        return boxes

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        if not self._slots:
            return super().get_content_height(container, viewport, width)
        return max(box.rows for box in self._boxes(width))

    def render_line(self, y: int) -> Strip:
        if not self._slots:
            return super().render_line(y)
        graphics = graphics_for(self.app)
        image_id_for = None
        if graphics is not None:
            graphics.add_view(self)
            if graphics.protocol is GraphicsProtocol.KITTY and self._placeholders_keep_color():
                image_id_for = graphics.kitty_image
        background = self.rich_style.bgcolor
        blank = Style(bgcolor=background)
        segments: list[Segment] = []
        end = 0
        for index, (slot, box) in enumerate(self.image_cells(self.content_size.width)):
            if box.x > end:
                segments.append(Segment(" " * (box.x - end), blank))
            end = box.x + box.columns
            if y >= box.rows:
                segments.append(Segment(" " * box.columns, blank))
                continue
            image_id = image_id_for(self, slot, box) if image_id_for is not None else 0
            if image_id:
                style = kitty.image_id_style(image_id) + Style(bgcolor=background)
                segments.extend(Segment(cell, style) for cell in kitty.placeholder_cells(y, box.columns))
            else:
                segments.extend(self._half_block_row(index, slot, box, y))
        return Strip(segments, end)

    def _half_block_row(self, index: int, slot: ImageSlot, box: ImageBox, y: int) -> list[Segment]:
        rows = self._half_blocks.get((index, box))
        if rows is None:
            rows = half_block_rows(slot.preview.image, box)
            self._half_blocks = {key: value for key, value in self._half_blocks.items() if key[0] != index}
            self._half_blocks[(index, box)] = rows
        return rows[y]

    def _placeholders_keep_color(self) -> bool:
        """Whether the colors naming each image reach the terminal unchanged.

        A screen under a modal is tinted, and tint, opacity and every filter but the ANSI-to-truecolor
        one (which leaves truecolor alone) blend the foreground: the cell would show another image or
        none. Half-blocks are drawn instead, blended like everything else.
        """
        styles = self.styles
        if self.screen is not self.app.screen or styles.tint.a or styles.text_opacity != 1.0 or self.opacity != 1.0:
            return False
        return all(isinstance(line_filter, ANSIToTruecolor) for line_filter in self.get_line_filters())

    def on_unmount(self) -> None:
        graphics = graphics_for(self.app)
        if graphics is not None:
            graphics.remove_view(self, self._slots)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Images copy as nothing."""
        _ = selection
        return None
