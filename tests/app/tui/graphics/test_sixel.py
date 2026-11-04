# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sixel encoding, read back with the test decoder."""

from __future__ import annotations

import pytest
from PIL import Image

from chrys.app.tui.graphics.sixel import EncodingStopped, encode_rows
from tests.support.sixel_decoder import decode_sixel


def test_each_cell_row_is_its_own_transparent_image_with_square_pixels() -> None:
    image = Image.new("RGB", (30, 45), (255, 0, 0))
    image.paste((0, 0, 255), (10, 0, 20, 45))

    rows = encode_rows(image)

    assert len(rows) == 3
    decoded = [decode_sixel(row) for row in rows]
    assert [row.params for row in decoded] == [(0, 1, 0)] * 3
    assert [row.raster for row in decoded] == [(1, 1, 30, 20), (1, 1, 30, 20), (1, 1, 30, 5)]
    for row in decoded:
        assert row.rgb(0, 0) == (255, 0, 0)
        assert row.rgb(15, 4) == (0, 0, 255)
        assert row.rgb(29, row.raster[3] - 1) == (255, 0, 0)
    # A 20-pixel row ends in the fourth band's third pixel; the rest of the band stays unset.
    assert max(y for _, y in decoded[0].pixels) == 19
    assert decoded[0].bands == 4
    assert max(y for _, y in decoded[2].pixels) == 4


def test_every_row_defines_the_colors_it_uses() -> None:
    image = Image.new("RGB", (4, 40), (10, 200, 30))
    image.paste((250, 250, 250), (0, 20, 4, 40))

    first, second = (decode_sixel(row) for row in encode_rows(image))

    assert set(first.pixels.values()) == {first.pixels[(0, 0)]}
    assert first.rgb(3, 19) == (10, 199, 31)
    assert second.rgb(3, 19) == (250, 250, 250)


def test_runs_are_length_encoded_and_each_row_is_complete() -> None:
    image = Image.new("RGB", (200, 20), (0, 128, 0))

    (row,) = encode_rows(image)

    assert "!200" in row
    decoded = decode_sixel(row)
    assert len(decoded.pixels) == 200 * 20


def test_many_colors_are_quantized_to_the_registers() -> None:
    image = Image.new("RGB", (64, 20))
    image.putdata([(x * 4, y * 12, (x + y) % 256) for y in range(20) for x in range(64)])

    (row,) = encode_rows(image)

    decoded = decode_sixel(row)
    assert len(decoded.pixels) == 64 * 20
    assert len(set(decoded.pixels.values())) <= 256
    red, green, _ = decoded.rgb(63, 19) or (0, 0, 0)
    assert abs(red - 252) <= 16
    assert abs(green - 228) <= 16


def test_encoding_stops_before_the_next_row_once_the_rows_are_not_wanted() -> None:
    image = Image.new("RGB", (30, 100), (255, 0, 0))
    asked: list[bool] = []

    def wanted() -> bool:
        asked.append(len(asked) < 2)
        return asked[-1]

    with pytest.raises(EncodingStopped):
        encode_rows(image, wanted=wanted)
    assert asked == [True, True, False]
    assert encode_rows(image, wanted=lambda: True) == encode_rows(image)
