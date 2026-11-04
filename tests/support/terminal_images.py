# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An app that draws chat images with a terminal graphics protocol, recording what it writes."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image
from textual._compositor import CompositorUpdate
from textual.app import App

from chrys.app.tui.graphics import kitty
from chrys.app.tui.graphics.manager import TerminalGraphics, _OverlayFrame
from chrys.app.tui.widgets.chat.image_preview import ChatImagePreview, extract_image_previews

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual.screen import Screen

    from chrys.app.tui.graphics.detect import GraphicsProtocol


class TerminalImagesApp(App[None]):
    """Installs ``TerminalGraphics`` for ``protocol`` at mount (none for None); mount images after it.

    ``writes`` holds what the graphics wrote outside frames (kitty commands) and ``frames`` each frame
    the overlay extended, rendered as the terminal would receive it, frame text included. Rendering
    happens here because a headless app never renders its frames. ``overlay_frames`` counts those the
    overlay wrote of its own, to draw rows owed; the others are Textual's.
    """

    CSS = "Screen { layers: base above; }"

    def __init__(self, protocol: GraphicsProtocol | None) -> None:
        super().__init__()
        self.protocol = protocol
        self.writes: list[str] = []
        self.frames: list[str] = []
        self.overlay_frames = 0
        self.graphics: TerminalGraphics | None = None

    def on_mount(self) -> None:
        if self.protocol is not None:
            self.graphics = TerminalGraphics(self, self.protocol, self.writes.append, cell_pixels=lambda: (10, 20))

    async def on_unmount(self) -> None:
        if self.graphics is not None:
            await self.graphics.close()

    def _display(self, screen: Screen, renderable: RenderableType | None) -> None:
        framed = renderable if self.graphics is None else self.graphics.frame(renderable)
        if framed is not renderable and isinstance(framed, CompositorUpdate):
            self.frames.append(framed.render_segments(self.console))
            self.overlay_frames += isinstance(renderable, _OverlayFrame)
        super()._display(screen, framed)

    def screen_lines(self) -> list[str]:
        """The active screen's text as composited now, from each widget's cached lines."""
        return [strip.text for strip in self.screen._compositor.render_strips()]

    def placeholder_ids(self) -> set[int]:
        """The kitty image ids the active screen's placeholder cells name, as composited now."""
        ids: set[int] = set()
        for strip in self.screen._compositor.render_strips():
            for segment in strip:
                color = segment.style.color if segment.style else None
                if segment.text.startswith(kitty.PLACEHOLDER) and color is not None:
                    red, green, blue = color.get_truecolor()
                    ids.add(red << 16 | green << 8 | blue)
        return ids


def solid_png(size: tuple[int, int], color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    """A solid PNG of ``size``."""
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def image_previews(*sizes: tuple[int, int]) -> list[ChatImagePreview]:
    """Decoded previews of solid images, one per size."""
    contents = [
        {"media_type": "image/png", "uri": "data:image/png;base64," + base64.b64encode(solid_png(size)).decode()}
        for size in sizes
    ]
    return extract_image_previews(contents)
