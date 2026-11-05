# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build what a terminal is sent for one image, from its data URI, off the event loop.

Chat previews keep a small half-block thumbnail; a graphics protocol shows far more pixels, so each
payload decodes the original again at the size its cell box needs.
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import TYPE_CHECKING, Final

from PIL import Image, ImageOps

from chrys.app.tui.graphics.sixel import SIXEL_CELL_HEIGHT, SIXEL_CELL_WIDTH, EncodingStopped, encode_rows
from chrys.app.tui.widgets.chat.image_preview import (
    MAX_PREVIEW_INPUT_PIXELS,
    TRANSPARENT_BACKGROUND,
    decode_data_uri,
)
from chrys.foundation.text.images import IMAGE_DECODE_FORMATS

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_TRANSMIT_EDGE: Final = 4096
"""The longest edge sent to a kitty-protocol terminal; it scales the image to its cells itself."""


def kitty_png(data_uri: str, *, columns: int, rows: int, cell_pixels: tuple[int, int]) -> bytes:
    """A PNG no larger than the box's pixels (nor the original), keeping transparency."""
    cell_width, cell_height = cell_pixels
    box = (min(columns * cell_width, MAX_TRANSMIT_EDGE), min(rows * cell_height, MAX_TRANSMIT_EDGE))
    with _open(data_uri) as opened:
        opened.draft("RGB", box)
        image = ImageOps.exif_transpose(opened)
        image = image.convert("RGBA" if _has_alpha(image) else "RGB")
    image.thumbnail(box, Image.Resampling.LANCZOS)
    out = BytesIO()
    image.save(out, format="PNG", compress_level=1)
    return out.getvalue()


def sixel_rows(
    data_uri: str, *, columns: int, rows: int, pixel_height: int, wanted: Callable[[], bool] | None = None
) -> tuple[str, ...]:
    """Sixel rows filling ``columns`` x ``rows`` cells exactly as the half-blocks do (``_row_canvas``).

    ``wanted`` is passed to ``encode_rows``.
    """
    return encode_rows(_row_canvas(data_uri, columns=columns, rows=rows, pixel_height=pixel_height), wanted=wanted)


def iterm_rows(
    data_uri: str, *, columns: int, rows: int, pixel_height: int, wanted: Callable[[], bool] | None = None
) -> tuple[str, ...]:
    """iTerm2 inline images, one per cell row, filling the cells as ``sixel_rows`` does.

    Each is a PNG stretched over ``columns`` cells by one, the cursor left where it was. Like Sixel it
    is 10 by 20 pixels a cell, which the terminal scales to its cells: far more than half-blocks show,
    for little to send. ``wanted`` is asked before each row; once it says no, ``EncodingStopped`` is
    raised.
    """
    canvas = _row_canvas(data_uri, columns=columns, rows=rows, pixel_height=pixel_height)
    encoded: list[str] = []
    for top in range(0, canvas.height, SIXEL_CELL_HEIGHT):
        if wanted is not None and not wanted():
            raise EncodingStopped
        out = BytesIO()
        canvas.crop((0, top, canvas.width, top + SIXEL_CELL_HEIGHT)).save(out, format="PNG", compress_level=1)
        png = out.getvalue()
        encoded.append(
            f"\x1b]1337;File=inline=1;size={len(png)};width={columns};height=1;preserveAspectRatio=0;"
            f"doNotMoveCursor=1:{base64.b64encode(png).decode('ascii')}\x1b\\"
        )
    return tuple(encoded)


def _row_canvas(data_uri: str, *, columns: int, rows: int, pixel_height: int) -> Image.Image:
    """``columns`` x ``rows`` cells at 10 by 20 pixels a cell, laid out exactly as the half-blocks.

    The half-block box is ``columns`` pixels wide and ``pixel_height`` tall at two pixels a cell, so
    each half-block pixel becomes 10x10 pixels; an odd last half-row is padded with the half-block
    background, as the half-blocks pad it.
    """
    size = (columns * SIXEL_CELL_WIDTH, pixel_height * SIXEL_CELL_WIDTH)
    with _open(data_uri) as opened:
        opened.draft("RGB", size)
        image = ImageOps.exif_transpose(opened)
        image = image.convert("RGBA" if _has_alpha(image) else "RGB")
    image = image.resize(size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (size[0], rows * SIXEL_CELL_HEIGHT), TRANSPARENT_BACKGROUND)
    canvas.paste(image, (0, 0), image if image.mode == "RGBA" else None)
    return canvas


def _open(data_uri: str) -> Image.Image:
    image = Image.open(BytesIO(decode_data_uri(data_uri)), formats=IMAGE_DECODE_FORMATS)
    if image.width * image.height > MAX_PREVIEW_INPUT_PIXELS:
        image.close()
        msg = "image exceeds the supported pixel limit"
        raise ValueError(msg)
    return image


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info
