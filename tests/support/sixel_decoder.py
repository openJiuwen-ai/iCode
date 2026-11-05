# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A small Sixel decoder, so tests read back the pixels an encoded image draws.

It knows what the encoder writes: one DCS image with raster attributes, ``#n;2;r;g;b`` color
definitions in percent, color selection, ``!n`` repeats, ``$`` (back to the band's start) and ``-``
(next band). Unset pixels are absent from the result: with ``P2=1`` they stay transparent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_IMAGE = re.compile(r"\x1bP(?P<params>[0-9;]*)q(?P<body>.*)\x1b\\\Z", re.DOTALL)
_TOKEN = re.compile(
    r'"(?P<raster>[0-9;]*)|#(?P<color>[0-9;]+)|!(?P<repeat>[0-9]+)(?P<rchar>[?-~])|(?P<sixel>[?-~])|(?P<cr>\$)|(?P<nl>-)'
)


@dataclass(frozen=True)
class DecodedSixel:
    """The pixels one Sixel image sets, and what its introducer and raster attributes declared."""

    params: tuple[int, ...]
    raster: tuple[int, ...]
    pixels: dict[tuple[int, int], tuple[int, int, int]]
    bands: int

    def rgb(self, x: int, y: int) -> tuple[int, int, int] | None:
        """The color at a pixel, as 0-255 RGB rounded from the percent the image gave."""
        value = self.pixels.get((x, y))
        if value is None:
            return None
        red, green, blue = value
        return round(red * 255 / 100), round(green * 255 / 100), round(blue * 255 / 100)


def decode_sixel(text: str) -> DecodedSixel:
    """Decode one ``ESC P … q … ESC \\`` image; raise on anything the encoder never writes."""
    match = _IMAGE.fullmatch(text)
    if match is None:
        raise ValueError(f"not one sixel image: {text[:40]!r}")
    params = tuple(int(part or 0) for part in match["params"].split(";"))
    palette: dict[int, tuple[int, int, int]] = {}
    pixels: dict[tuple[int, int], tuple[int, int, int]] = {}
    raster: tuple[int, ...] = ()
    color = 0
    x = band = 0
    position = 0
    body = match["body"]
    while position < len(body):
        token = _TOKEN.match(body, position)
        if token is None:
            raise ValueError(f"unexpected sixel data at {position}: {body[position : position + 20]!r}")
        position = token.end()
        if token["raster"] is not None:
            raster = tuple(int(part) for part in token["raster"].split(";"))
        elif token["color"] is not None:
            parts = [int(part) for part in token["color"].split(";")]
            color = parts[0]
            if len(parts) == 5:
                if parts[1] != 2:
                    raise ValueError("only RGB color definitions are expected")
                palette[color] = (parts[2], parts[3], parts[4])
        elif token["cr"] is not None:
            x = 0
        elif token["nl"] is not None:
            x = 0
            band += 1
        else:
            count = int(token["repeat"]) if token["repeat"] is not None else 1
            value = ord(token["rchar"] or token["sixel"]) - 0x3F
            for column in range(x, x + count):
                for bit in range(6):
                    if value & (1 << bit):
                        pixels[(column, band * 6 + bit)] = palette[color]
            x += count
    return DecodedSixel(params=params, raster=raster, pixels=pixels, bands=band + 1)
