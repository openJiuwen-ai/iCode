# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Kitty graphics commands and Unicode placeholder cells."""

from __future__ import annotations

import base64
import re

from rich.segment import Segment
from textual.strip import Strip

from chrys.app.tui.graphics import kitty

_COMMAND = re.compile(r"\x1b_G(?P<keys>[^;\x1b]*)(?:;(?P<payload>[A-Za-z0-9+/=]*))?\x1b\\")


def _commands(text: str) -> list[tuple[dict[str, str], str]]:
    parsed: list[tuple[dict[str, str], str]] = []
    position = 0
    while position < len(text):
        match = _COMMAND.match(text, position)
        assert match is not None, text[position : position + 40]
        keys = dict(pair.split("=", 1) for pair in match["keys"].split(","))
        parsed.append((keys, match["payload"] or ""))
        position = match.end()
    return parsed


def test_placeholder_cells_name_their_row_and_column_with_the_kitty_diacritics() -> None:
    cells = kitty.placeholder_cells(2, 3)

    # The first entries of kitty's rowcolumn-diacritics.txt.
    assert cells == ("\U0010eeee\u030e\u0305", "\U0010eeee\u030e\u030d", "\U0010eeee\u030e\u030e")
    assert kitty.MAX_PLACEMENT_CELLS == 297
    assert kitty.placeholder_cells(296, 297)[-1][1:] == "\U0001d244" * 2


def test_image_id_is_the_truecolor_foreground() -> None:
    style = kitty.image_id_style(0x12_34_56)

    assert style.color is not None
    assert style.color.triplet is not None
    assert tuple(style.color.triplet) == (0x12, 0x34, 0x56)
    assert "38;2;18;52;86" in style._make_ansi_codes(style.color.system)


def test_transmit_sends_a_png_with_a_virtual_placement_in_quiet_chunks() -> None:
    png = bytes(range(256)) * 40

    commands = _commands("".join(kitty.transmit_commands(77, png, columns=12, rows=5)))

    first, *rest = commands
    assert first[0] == {"a": "T", "U": "1", "f": "100", "t": "d", "q": "2", "i": "77", "c": "12", "r": "5", "m": "1"}
    assert all(keys == {"q": "2", "m": "1"} for keys, _ in rest[:-1])
    assert rest[-1][0] == {"q": "2", "m": "0"}
    assert all(len(payload) <= 4096 and len(payload) % 4 == 0 for _, payload in commands)
    assert base64.b64decode("".join(payload for _, payload in commands)) == png


def test_a_small_image_is_one_command() -> None:
    commands = _commands("".join(kitty.transmit_commands(1, b"png", columns=1, rows=1)))

    assert [keys["m"] for keys, _ in commands] == ["0"]


def test_delete_frees_the_image_quietly() -> None:
    assert _commands(kitty.delete_command(9)) == [({"a": "d", "d": "I", "i": "9", "q": "2"}, "")]


def test_cropping_one_segment_per_cell_never_splits_a_cell_from_its_marks() -> None:
    columns = 40
    cells = kitty.placeholder_cells(0, columns)
    strip = Strip([Segment(cell) for cell in cells], columns)

    for start in range(columns):
        for end in range(start + 1, columns + 1):
            assert "".join(segment.text for segment in strip.crop(start, end)) == "".join(cells[start:end])
    for cut in range(1, columns):
        left, right = strip.divide([cut, columns])
        assert "".join(segment.text for segment in left) == "".join(cells[:cut])
        assert "".join(segment.text for segment in right) == "".join(cells[cut:])
