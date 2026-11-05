# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sixel encoding, one image per terminal cell row.

Windows Terminal draws Sixel on a fixed virtual cell of 10x20 pixels, whatever the font, and scales
each cell's slice to the real cell size. An image is encoded as one Sixel image per cell row so that
any run of rows can be drawn on its own: a widget scrolled half out of view, or with a toast over one
row, redraws only the rows that need it, from strings encoded once.

A 20-pixel row takes four 6-pixel bands, the last one only a third used. The image is sent with a
transparent background (``P2=1``), so the unused part, which reaches into the next cell row, leaves
whatever is there untouched.
"""

from __future__ import annotations

import re
from functools import cache
from typing import TYPE_CHECKING, Final

from PIL import Image

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

SIXEL_CELL_WIDTH: Final = 10
SIXEL_CELL_HEIGHT: Final = 20
_COLORS: Final = 256
_BAND: Final = 6
_SIXEL_ZERO: Final = 0x3F
_RUN = re.compile(rb"(.)\1{3,}")
# DCS P1=0 (its aspect is replaced by the raster attributes), P2=1 (unset pixels stay transparent).
_INTRO: Final = b"\x1bP0;1;0q"
_ST: Final = b"\x1b\\"


class EncodingStopped(Exception):
    """The rows being encoded were no longer wanted."""


def encode_rows(
    image: Image.Image, *, row_height: int = SIXEL_CELL_HEIGHT, wanted: Callable[[], bool] | None = None
) -> tuple[str, ...]:
    """Encode an RGB image as one Sixel image per ``row_height`` pixels, sharing one palette.

    The last row may be shorter. Each string draws at the text cursor and leaves the cursor in the
    row its last band starts in, never below the image. ``wanted`` is asked before each row: a tall
    image takes seconds, and once it says no, ``EncodingStopped`` is raised.
    """
    quantized = image.convert("RGB").quantize(colors=_COLORS, method=Image.Quantize.MEDIANCUT)
    width, height = quantized.size
    data = quantized.tobytes()
    palette = quantized.getpalette() or []
    definitions = [
        b"#%d;2;%d;%d;%d" % (index, *(round(value * 100 / 255) for value in palette[3 * index : 3 * index + 3]))
        for index in range(len(palette) // 3)
    ]
    zero_row = int.from_bytes(bytes((_SIXEL_ZERO,)) * width, "big")
    rows: list[str] = []
    for top in range(0, height, row_height):
        if wanted is not None and not wanted():
            raise EncodingStopped
        bottom = min(top + row_height, height)
        used: set[int] = set()
        bands: list[bytes] = []
        for band_top in range(top, bottom, _BAND):
            lines = [data[y * width : (y + 1) * width] for y in range(band_top, min(band_top + _BAND, bottom))]
            present = set(b"".join(lines))
            used |= present
            bands.append(b"$".join(_color_line(color, lines, width, zero_row) for color in sorted(present)))
        header = b'%s"1;1;%d;%d' % (_INTRO, width, bottom - top)
        palette_part = b"".join(definitions[color] for color in sorted(used))
        rows.append((header + palette_part + b"-".join(bands) + _ST).decode("ascii"))
    return tuple(rows)


def _color_line(color: int, lines: Sequence[bytes], width: int, zero_row: int) -> bytes:
    """One band's sixels for one color: bit ``n`` of each column is pixel row ``n`` of the band."""
    mask = _mask_table(color)
    bits = 0
    for shift, line in enumerate(lines):
        # Each byte is 0 or 1, so the shifted rows add up per byte without carrying.
        bits |= int.from_bytes(line.translate(mask), "big") << shift
    sixels = (bits + zero_row).to_bytes(width, "big").rstrip(b"?")
    return b"#%d%s" % (color, _RUN.sub(_run_length, sixels))


def _run_length(match: re.Match[bytes]) -> bytes:
    run = match.group(0)
    return b"!%d%c" % (len(run), run[0])


@cache
def _mask_table(color: int) -> bytes:
    """A ``bytes.translate`` table mapping ``color`` to 1 and every other index to 0."""
    return bytes(int(index == color) for index in range(256))
