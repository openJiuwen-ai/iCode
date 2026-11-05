# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rule oracles for Vim commands treating every prompt field as one character."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from random import Random

import pytest

from chrys.app.tui.widgets.editor import EditorTextRange, VimKeymap, VimState
from tests.app.tui.widgets.editor.test_keymaps import _Host, _location, _offset, _stroke


@dataclass
class _UnitHost(_Host):
    """An ordinary buffer with tracked field lifetimes, independent of Vim's view."""

    units: tuple[tuple[int, int], ...] = ()
    _unit_undo: list[tuple[tuple[int, int], ...]] = field(default_factory=list)
    _unit_redo: list[tuple[tuple[int, int], ...]] = field(default_factory=list)

    def editor_unit_spans(self) -> list[tuple[int, int]]:
        return list(self.units)

    def replace_editor_ranges(self, replacements: Sequence[tuple[EditorTextRange, str]]) -> None:
        if not replacements:
            return
        before_text = self.text
        self._unit_undo.append(self.units)
        self._unit_redo.clear()
        for source_range, replacement in reversed(replacements):
            start, end = sorted((_offset(self.text, source_range.start), _offset(self.text, source_range.end)))
            # Writers must submit whole characters, including whole fields.
            assert all(not low < edge < high for low, high in self.units for edge in (start, end))
            if self.text[start:end] != replacement:
                difference = len(replacement) - (end - start)
                self.units = tuple(
                    (low + difference, high + difference) if low >= end else (low, high)
                    for low, high in self.units
                    if high <= start or low >= end
                )
            # Apply each step to let subsequent indices refer to the preceding original text.
            self.text = self.text[:start] + replacement + self.text[end:]
        # The base host records a public undo operation once for the entire batch.
        original = self.text
        self.text = before_text
        super().replace_editor_ranges(replacements)
        assert self.text == original

    def editor_undo(self) -> None:
        if self._undo:
            self._unit_redo.append(self.units)
            self.units = self._unit_undo.pop()
        super().editor_undo()

    def editor_redo(self) -> None:
        if self._redo:
            self._unit_undo.append(self.units)
            self.units = self._unit_redo.pop()
        super().editor_redo()


def _feed(keymap: VimKeymap, host: _UnitHost, keys: Sequence[str]) -> None:
    for key in keys:
        keymap.handle_key(_stroke(key), host)
        for location in (host.cursor, host.selection_range.start, host.selection_range.end):
            offset = _offset(host.text, location)
            assert all(not low < offset < high for low, high in host.units), (keys, key, location, host.units)


def _collapse(host: _UnitHost) -> tuple[str, tuple[int, int], tuple[tuple[int, int], tuple[int, int]]]:
    """Replace surviving fields with # and map all observed positions independently."""

    def position(location: tuple[int, int]) -> tuple[int, int]:
        offset = _offset(host.text, location)
        removed = sum(min(max(offset - low, 0), high - low - 1) for low, high in host.units)
        return _location(collapsed, offset - removed)

    collapsed = host.text
    for low, high in reversed(host.units):
        collapsed = collapsed[:low] + "#" + collapsed[high:]
    return collapsed, position(host.cursor), (position(host.selection_range.start), position(host.selection_range.end))


def _corpus(marked: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Angle marks delimit test fields; their punctuation is deliberately hostile."""
    pieces: list[str] = []
    units: list[tuple[int, int]] = []
    remaining = marked
    size = 0
    while "«" in remaining:
        prefix, remaining = remaining.split("«", 1)
        value, remaining = remaining.split("»", 1)
        pieces.extend((prefix, value))
        size += len(prefix)
        units.append((size, size + len(value)))
        size += len(value)
    return "".join((*pieces, remaining)), tuple(units)


_ORACLE_CORPORA = (
    'a «field "(x)" [q] ! z» b\n"c d" (e f)\n«second \' } , field» g',
    "«a ' [ \\ ` (»\nx «b ) ] \" field»\ny z\n",
)
_MOTIONS = ("h", "l", "0", "^", "$", "w", "b", "e", "gg", "G")
_OBJECTS = tuple(scope + obj for scope in ("i", "a") for obj in ("w", "W", '"', "'", "`", "(", "b", "[", "{", "B"))
_SINGLE_COMMANDS = (
    *_MOTIONS,
    "x",
    "X",
    "rZ",
    "~",
    "J",
    "i",
    "I",
    "a",
    "A",
    "o",
    "O",
    "u",
    "ctrl+r",
    "D",
    "C",
    "Y",
    "S",
    "s",
    "v",
    "V",
    *(operator + motion for operator in "dcy" for motion in _MOTIONS),
    "dd",
    "cc",
    "yy",
    *(operator + obj for operator in "dcy" for obj in _OBJECTS),
    *(visual + motion for visual in ("v", "V") for motion in _MOTIONS if motion != "gg"),
    *(visual + command for visual in ("v", "V") for command in ("o", "d", "c", "y", "x", "escape")),
)


def _keys(command: str) -> list[str]:
    return [command] if command in {"ctrl+r", "escape", "ctrl+d", "ctrl+u"} else list(command)


def _positions(text: str, units: tuple[tuple[int, int], ...]) -> list[tuple[int, int]]:
    return [
        _location(text, offset)
        for offset in range(len(text) + 1)
        if (offset == len(text) or text[offset] != "\n" or offset == 0 or text[offset - 1] == "\n")
        and all(not low < offset < high for low, high in units)
    ]


def _isolated(host: _UnitHost) -> bool:
    return all(
        (low == 0 or host.text[low - 1].isspace()) and (high == len(host.text) or host.text[high].isspace())
        for low, high in host.units
    )


def _differential(
    marked: str, cursor: tuple[int, int], keys: Sequence[str], *, stop_when_adjacent: bool = True
) -> tuple[int, int]:
    text, units = _corpus(marked)
    fields = _UnitHost(text, cursor=cursor, units=units)
    collapsed, plain_cursor, _selection = _collapse(fields)
    plain = _UnitHost(collapsed, cursor=plain_cursor)
    field_map, plain_map = VimKeymap(), VimKeymap()
    for index, key in enumerate(keys):
        # Ordinary # and a field cease to have the same word class once an edit
        # puts punctuation beside the field. That is outside this oracle's rule.
        if stop_when_adjacent and not _isolated(fields):
            return index, len(keys) - index
        _feed(field_map, fields, [key])
        _feed(plain_map, plain, [key])
        assert (_collapse(fields), field_map.state) == (_collapse(plain), plain_map.state), (marked, cursor, keys, key)
    return len(keys), 0


@pytest.mark.parametrize("command", _SINGLE_COMMANDS)
@pytest.mark.parametrize("marked", _ORACLE_CORPORA)
def test_vim_field_differential_single_commands(marked: str, command: str) -> None:
    text, units = _corpus(marked)
    for cursor in _positions(text, units):
        _differential(marked, cursor, _keys(command))


@pytest.mark.parametrize("seed", range(24))
def test_vim_field_differential_seeded_sequences(seed: int) -> None:
    random = Random(0x182 + seed)
    marked = _ORACLE_CORPORA[seed % len(_ORACLE_CORPORA)]
    text, units = _corpus(marked)
    commands = tuple(command for command in _SINGLE_COMMANDS if command not in {"i", "I", "a", "A", "o", "O"})
    cursor = random.choice(_positions(text, units))
    keys = []
    for _ in range(16):
        keys.extend(_keys(random.choice(commands)))
        keys.append("escape")
        if random.randrange(3) == 0:
            keys.extend((str(random.choice((2, 3, 5))), random.choice(("w", "b", "e", "x", "~"))))
        if random.randrange(4) == 0:
            keys.extend(("u", "ctrl+r"))
    _differential(marked, cursor, keys, stop_when_adjacent=False)


_COUNTED_COMMANDS = (
    *(motion for motion in _MOTIONS if motion != "0"),
    "j",
    "k",
    "ctrl+d",
    "ctrl+u",
    "x",
    "X",
    "rZ",
    "~",
    "J",
    "D",
    "C",
    "Y",
    "S",
    "s",
    "dd",
    "cc",
    "yy",
    *(operator + motion for operator in "dcy" for motion in _MOTIONS),
    *(operator + obj for operator in "dcy" for obj in _OBJECTS),
)


@pytest.mark.parametrize(
    "command", tuple(command for command in _COUNTED_COMMANDS if command not in {"j", "k", "ctrl+d", "ctrl+u"})
)
@pytest.mark.parametrize("count", (2, 3, 5))
def test_vim_counted_commands_match_one_character_oracle(command: str, count: int) -> None:
    for marked in _ORACLE_CORPORA:
        text, units = _corpus(marked)
        for cursor in _positions(text, units):
            _differential(marked, cursor, [str(count), *_keys(command)])
            _differential(marked, cursor, _keys(command) * count)


_COUNT_CORPORA = (
    "a«first ' x»«second [ y»b c d\n«third ( z»\ne f «last } q»",
    "abcdefg\nx\n«first field» z\nabcdefg\n\nabcdefg",
    "ab cd ef gh ij\nkl mn op qr st\nuv wx yz\n",
)
_REPEAT_MOTIONS = ("h", "l", "w", "b", "e", "j", "k", "ctrl+d", "ctrl+u")


@pytest.mark.parametrize("command", _REPEAT_MOTIONS)
@pytest.mark.parametrize("count", (2, 3, 5))
def test_vim_counted_motions_equal_repeating_at_every_position(command: str, count: int) -> None:
    for marked in _COUNT_CORPORA:
        text, units = _corpus(marked)
        for cursor in _positions(text, units):
            for visual in ([], ["v"], ["V"]):
                counted = _UnitHost(text, cursor=cursor, units=units)
                repeated = _UnitHost(text, cursor=cursor, units=units)
                _feed(VimKeymap(), counted, [*visual, str(count), *_keys(command)])
                _feed(VimKeymap(), repeated, [*visual, *(_keys(command) * count)])
                assert _collapse(counted) == _collapse(repeated), (marked, cursor, visual, count, command)


@pytest.mark.parametrize(
    ("command", "expected_text", "expected_cursor", "expected_register"),
    [
        ("2rZ", "ZZcd ef", (0, 1), ""),
        ("2yw", "abcd ef", (0, 0), "abcd ef"),
        ("2yy", "abcd ef", (0, 0), "abcd ef\n"),
    ],
)
def test_vim_count_semantics_do_not_replay_replace_or_yank(
    command: str, expected_text: str, expected_cursor: tuple[int, int], expected_register: str
) -> None:
    host = _UnitHost("abcd ef")
    keymap = VimKeymap()
    _feed(keymap, host, list(command))
    assert (host.text, host.cursor, keymap.register.text) == (expected_text, expected_cursor, expected_register)


@pytest.mark.parametrize("command", ("2x", "3x", "5x", "2X", "3X", "5X", "2rZ", "3rZ", "5rZ", "2~", "3~", "5~"))
def test_vim_character_counts_clip_at_current_line(command: str) -> None:
    marked = "«field x»a\nz"
    text, units = _corpus(marked)
    cursor = (0, len("field x")) if command.endswith("X") else (0, 0)
    host = _UnitHost(text, cursor=cursor, units=units)
    _feed(VimKeymap(), host, list(command))
    assert host.text.endswith("\nz")
    if command.endswith("x"):
        assert host.text == "\nz"
    elif command.endswith("X"):
        assert host.text == "a\nz"
    elif "r" in command:
        assert host.text == "ZZ\nz"
        assert host.units == ()
    else:
        assert host.text == "field xA\nz"
        assert host.units == units


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("dw", "second [ yb c"),
        ("diw", "second [ yb c"),
        ("daw", "second [ yb c"),
        ("diW", " c"),
        ("daW", "c"),
        ("2dw", "b c"),
        ("2diw", "b c"),
        ("2daw", "b c"),
    ],
)
def test_vim_adjacent_fields_are_separate_words_and_share_a_word_big(command: str, expected: str) -> None:
    text, units = _corpus("«first ' x»«second [ y»b c")
    host = _UnitHost(text, units=units)
    _feed(VimKeymap(), host, list(command))
    assert host.text == expected


@pytest.mark.parametrize(("command", "expected"), [("2$", (1, 2)), ("3gg", (2, 0)), ("3G", (2, 0))])
def test_vim_counts_for_line_end_and_document_lines(command: str, expected: tuple[int, int]) -> None:
    host = _UnitHost("abc\ndef\nghi\njkl")
    _feed(VimKeymap(), host, list(command))
    assert host.cursor == expected


def test_vim_join_count_is_number_of_lines() -> None:
    host = _UnitHost("a\nb\nc\nd")
    _feed(VimKeymap(), host, list("3J"))
    assert host.text == "a b c\nd"


def test_hash_oracle_requires_fields_to_remain_separate_from_punctuation() -> None:
    # A unit is its own word; two ordinary punctuation characters form one word.
    text, units = _corpus("«field value»(x)")
    fields, plain = _UnitHost(text, units=units), _UnitHost("#(x)")
    _feed(VimKeymap(), fields, list("dw"))
    _feed(VimKeymap(), plain, list("dw"))
    assert fields.text == "(x)"
    assert plain.text == "x)"


def test_visual_gg_uses_source_columns_like_other_vertical_moves() -> None:
    text, units = _corpus('a «field " x» b\nabcdef')
    fields, plain = _UnitHost(text, cursor=(1, 3), units=units), _UnitHost("a # b\nabcdef", cursor=(1, 3))
    _feed(VimKeymap(), fields, list("vgg"))
    _feed(VimKeymap(), plain, list("vgg"))
    assert fields.selection_range.end == (0, 2)
    assert plain.selection_range.end == (0, 3)


@pytest.mark.parametrize("visual", (False, True))
@pytest.mark.parametrize("key", ("tab", "shift+tab", "ctrl+q", "ctrl+insert", "q"))
def test_vim_every_nonvertical_key_resets_sticky_column(visual: bool, key: str) -> None:
    host = _UnitHost("abcdefghi\nxyz\nabcdefghi", cursor=(0, 6))
    keymap = VimKeymap()
    _feed(keymap, host, [*(["v"] if visual else []), "j", key, "j"])
    assert host.selection_range.end == (2, 2 + int(visual))


def _single_character_host(host: _UnitHost) -> _UnitHost:
    text, cursor, _selection = _collapse(host)
    removed = 0
    spans = []
    for low, high in host.units:
        spans.append((low - removed, low - removed + 1))
        removed += high - low - 1
    return _UnitHost(text, cursor=cursor, units=tuple(spans))


@pytest.mark.parametrize("command", _COUNTED_COMMANDS)
@pytest.mark.parametrize("count", (2, 3, 5))
def test_vim_all_counts_and_repeats_match_single_character_units(command: str, count: int) -> None:
    # Keeping one-character fields in the reference preserves the word rule
    # even when edits bring a field beside punctuation or another field.
    for marked in _COUNT_CORPORA:
        text, units = _corpus(marked)
        for cursor in _positions(text, units):
            for keys in ([str(count), *_keys(command)], _keys(command) * count):
                fields = _UnitHost(text, cursor=cursor, units=units)
                single = _single_character_host(fields)
                field_map, single_map = VimKeymap(), VimKeymap()
                for key in keys:
                    _feed(field_map, fields, [key])
                    _feed(single_map, single, [key])
                    # Vertical moves use source columns, so check their unit
                    # boundary invariant rather than equating different widths.
                    if command not in {"j", "k", "ctrl+d", "ctrl+u"}:
                        assert (_collapse(fields), field_map.state) == (_collapse(single), single_map.state), (
                            marked,
                            cursor,
                            count,
                            command,
                            keys,
                            key,
                        )


@pytest.mark.parametrize("count", (2, 3, 5))
def test_vim_change_count_applies_before_entering_insert(count: int) -> None:
    counted, repeated = _UnitHost("abcd efgh ijkl mnop qrst uvwx"), _UnitHost("abcd efgh ijkl mnop qrst uvwx")
    _feed(VimKeymap(), counted, [str(count), "c", "w"])
    _feed(VimKeymap(), repeated, list("cw") * count)
    assert counted.text != repeated.text
    assert repeated.text == " efgh ijkl mnop qrst uvwx"


@pytest.mark.parametrize("put", ("p", "P"))
@pytest.mark.parametrize("linewise", (False, True))
@pytest.mark.parametrize("count", (2, 3, 5))
def test_vim_put_count_and_repeat_for_each_register_kind(put: str, linewise: bool, count: int) -> None:
    text, units = _corpus("ab\n«field value» end")
    counted, repeated = _UnitHost(text, units=units), _UnitHost(text, units=units)
    ka, kb = VimKeymap(), VimKeymap()
    for keymap, host in ((ka, counted), (kb, repeated)):
        _feed(keymap, host, ["y", "y" if linewise else "l"])
        host.move_editor_cursor((1, 0))
    _feed(ka, counted, [str(count), put])
    _feed(kb, repeated, [put] * count)
    assert counted.text == repeated.text
    assert counted.units == repeated.units
    if linewise:
        assert counted.cursor == ((2, 0) if put == "p" else (1, 0))
        assert repeated.cursor == ((count + 1, 0) if put == "p" else (1, 0))
    elif put == "p":
        assert counted.cursor == repeated.cursor == (1, len("field value") + count - 1)
    else:
        assert counted.cursor == (1, count - 1)
        assert repeated.cursor == (1, 0)


@pytest.mark.parametrize(
    ("marked", "cursor", "expected"),
    [("«field x» b", (0, 0), " b"), ("a b", (0, 0), " b"), ("!! next", (0, 1), "! next")],
)
def test_vim_change_word_takes_current_token_even_at_its_last_character(
    marked: str, cursor: tuple[int, int], expected: str
) -> None:
    text, units = _corpus(marked)
    host = _UnitHost(text, cursor=cursor, units=units)
    _feed(VimKeymap(), host, list("cw"))
    assert host.text == expected


def test_vim_append_on_empty_line_stays_on_that_line() -> None:
    host = _UnitHost("\nabc")
    _feed(VimKeymap(), host, ["a"])
    assert host.cursor == (0, 0)
    assert host.text == "\nabc"


@pytest.mark.parametrize("put", ("p", "P"))
def test_vim_character_put_on_empty_line_stays_on_that_line(put: str) -> None:
    host = _UnitHost("\nabc", cursor=(1, 0))
    keymap = VimKeymap()
    _feed(keymap, host, list("yl"))
    host.move_editor_cursor((0, 0))
    _feed(keymap, host, [put])
    assert host.text == "a\nabc"
    assert host.cursor == (0, 0)


@pytest.mark.parametrize("visual", ("v", "V"))
@pytest.mark.parametrize("motion", ("j", "k"))
@pytest.mark.parametrize("order", ("click-only", "click-before-key", "key-before-click"))
def test_vim_visual_cursor_move_drops_goal_without_moving_active_end(visual: str, motion: str, order: str) -> None:
    start_row, end_row = (0, 2) if motion == "j" else (2, 0)
    host = _UnitHost("abcdefghi\nxyz\nabcdefghi", cursor=(start_row, 6))
    keymap = VimKeymap()
    _feed(keymap, host, [visual, motion])
    if order == "key-before-click":
        _feed(keymap, host, ["q"])
    host.move_editor_cursor((1, 1))
    if order == "click-before-key":
        _feed(keymap, host, ["q"])
    _feed(keymap, host, [motion, "escape"])
    # The click collapses the host selection but leaves Visual's active end
    # at column 2. Its movement must use that column, ignoring the old goal 6.
    assert host.cursor == (end_row, 2)


@pytest.mark.parametrize("visual", ("v", "V"))
def test_vim_visual_cursor_move_keeps_the_existing_operator_range(visual: str) -> None:
    host = _UnitHost("abcdefghi\nxyz\nabcdefghi", cursor=(0, 6))
    keymap = VimKeymap()
    _feed(keymap, host, [visual, "j"])
    host.move_editor_cursor((0, 1))
    assert host.selection_range == EditorTextRange((0, 1), (0, 1))
    assert keymap.state is (VimState.VISUAL_CHAR if visual == "v" else VimState.VISUAL_LINE)

    _feed(keymap, host, ["y"])

    assert keymap.register.text == ("ghi\nxyz" if visual == "v" else "abcdefghi\nxyz\n")


@pytest.mark.parametrize("visual", (False, True))
def test_vim_count_digits_between_vertical_moves_keep_the_original_column(visual: bool) -> None:
    text, units = _corpus("abcdefghi\nx\nab «field value» zz\nabcdefghi")
    host = _UnitHost(text, cursor=(0, 6), units=units)
    _feed(VimKeymap(), host, [*(["v"] if visual else []), "j", "2", "j"])
    assert host.selection_range.end == (3, 6 + int(visual))


@pytest.mark.parametrize("visual", (False, True))
@pytest.mark.parametrize("key", ("ctrl+d", "ctrl+u"))
def test_vim_half_page_sticky_column_survives_a_unit_landing(visual: bool, key: str) -> None:
    text, units = _corpus("abcdefghi\nx\nx\nab «field value» zz\nx\nx\nabcdefghi")
    start_row, end_row = (0, 6) if key == "ctrl+d" else (6, 0)
    host = _UnitHost(text, cursor=(start_row, 6), units=units)
    keymap = VimKeymap()
    _feed(keymap, host, [*(["v"] if visual else []), key])
    assert host.selection_range.end == (3, 14 if visual and key == "ctrl+d" else 3)
    _feed(keymap, host, [key])
    assert host.selection_range.end == (end_row, 6 + int(visual and key == "ctrl+d"))


def test_vim_inner_word_count_includes_spaces_that_repeated_objects_leave() -> None:
    counted, repeated = _UnitHost("aa bb cc"), _UnitHost("aa bb cc")
    ka, kb = VimKeymap(), VimKeymap()
    _feed(ka, counted, list("2diw"))
    _feed(kb, repeated, list("diwdiw"))
    assert counted.text == " cc"
    assert repeated.text == "  cc"
    assert ka.register.text == "aa bb"
    assert kb.register.text == "bb"


def test_vim_insert_escape_returns_to_the_previous_whole_character() -> None:
    text, units = _corpus("ab «field value»")
    host = _UnitHost(text, cursor=(0, 3), units=units)
    _feed(VimKeymap(), host, ["a", "escape"])
    assert host.cursor == (0, 3)
    assert host.units == units


@pytest.mark.parametrize("put", ("p", "P"))
@pytest.mark.parametrize("linewise", (False, True))
@pytest.mark.parametrize("count", (2, 3, 5))
def test_vim_put_counts_and_repeats_match_single_character_units_everywhere(
    put: str, linewise: bool, count: int
) -> None:
    for marked in _COUNT_CORPORA:
        text, units = _corpus(marked)
        for cursor in _positions(text, units):
            for keys in ([str(count), put], [put] * count):
                fields = _UnitHost(text, cursor=cursor, units=units)
                single = _single_character_host(fields)
                field_map, single_map = VimKeymap(), VimKeymap()
                for keymap in (field_map, single_map):
                    # The register comes from a plain buffer so payload and
                    # register shape are identical on both coordinate systems.
                    _feed(keymap, _UnitHost("ab\n"), ["y", "y" if linewise else "l"])
                for key in keys:
                    _feed(field_map, fields, [key])
                    _feed(single_map, single, [key])
                    assert (_collapse(fields), field_map.state) == (_collapse(single), single_map.state), (
                        marked,
                        cursor,
                        linewise,
                        count,
                        put,
                        keys,
                        key,
                    )


@pytest.mark.parametrize("command", ("i", "I", "a", "A", "o", "O"))
@pytest.mark.parametrize("count", (2, 3, 5))
def test_vim_insert_commands_discard_count_uniformly(command: str, count: int) -> None:
    for marked in _COUNT_CORPORA:
        text, units = _corpus(marked)
        for cursor in _positions(text, units):
            counted, once = _UnitHost(text, cursor=cursor, units=units), _UnitHost(text, cursor=cursor, units=units)
            counted_map, once_map = VimKeymap(), VimKeymap()
            _feed(counted_map, counted, [str(count), command])
            _feed(once_map, once, [command])
            assert (_collapse(counted), counted_map.state, counted_map.count) == (
                _collapse(once),
                once_map.state,
                once_map.count,
            )
