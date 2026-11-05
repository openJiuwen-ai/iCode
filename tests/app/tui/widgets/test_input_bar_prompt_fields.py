# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for prompt-shortcut fields in the chat input: tracking, Tab jumps and the send guard."""

from __future__ import annotations

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.document._document import Location, Selection

from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ShortcutIntent, shift_prompt_fields
from chrys.foundation.text.draft_references import EMPTY_REFERENCES, iter_reference_labels
from tests.support.tui_helpers import click_when_settled, hold_prompt_fields_confirm_window
from tests.support.waiting import wait_for

# ---------------------------------------------------------------------------
# shift_prompt_fields: an edit replaces [start, end) with `inserted` characters.
# ---------------------------------------------------------------------------

_FIELDS = ((2, 5), (10, 14))


@pytest.mark.parametrize(
    ("start", "end", "inserted", "expected"),
    [
        (0, 0, 1, ((3, 6), (11, 15))),
        (7, 7, 3, ((2, 5), (13, 17))),
        (6, 8, 0, ((2, 5), (8, 12))),
        (6, 8, 5, ((2, 5), (13, 17))),
        (20, 20, 4, ((2, 5), (10, 14))),
        (3, 3, 1, ((11, 15),)),
        (2, 5, 1, ((8, 12),)),
        (4, 6, 0, ((8, 12),)),
        (1, 3, 0, ((8, 12),)),
        (4, 11, 2, ()),
        (0, 20, 0, ()),
    ],
    ids=[
        "insert-before-all",
        "insert-between",
        "delete-between",
        "replace-between",
        "insert-after-all",
        "insert-inside-first",
        "replace-first-exactly",
        "delete-over-first-end",
        "delete-over-first-start",
        "delete-across-both",
        "delete-everything",
    ],
)
def test_shift_prompt_fields(start: int, end: int, inserted: int, expected: tuple[tuple[int, int], ...]) -> None:
    assert tuple(shift_prompt_fields(_FIELDS, start, end, inserted)) == expected


@pytest.mark.parametrize(
    ("start", "end", "inserted", "expected"),
    [
        (2, 2, 3, ((5, 8), (13, 17))),
        (5, 5, 3, ((2, 5), (13, 17))),
        (0, 2, 0, ((0, 3), (8, 12))),
        (5, 7, 0, ((2, 5), (8, 12))),
        (5, 10, 0, ((2, 5), (5, 9))),
    ],
    ids=[
        "insert-at-start-edge",
        "insert-at-end-edge",
        "delete-up-to-start-edge",
        "delete-from-end-edge",
        "delete-gap-between-fields",
    ],
)
def test_shift_prompt_fields_edits_at_an_edge_keep_the_field(
    start: int, end: int, inserted: int, expected: tuple[tuple[int, int], ...]
) -> None:
    assert tuple(shift_prompt_fields(_FIELDS, start, end, inserted)) == expected


def test_shift_prompt_fields_empty() -> None:
    assert tuple(shift_prompt_fields((), 0, 3, 7)) == ()


# ---------------------------------------------------------------------------
# A real InputBar
# ---------------------------------------------------------------------------


class _FieldsApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []
        self.retried: list[str] = []

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event.text)

    def on_input_bar_retry_requested(self, event: InputBar.RetryRequested) -> None:
        self.retried.append(event.text)


_TEXT = "Translate in a tone tone:\n\ntext"
_TEXT_FIELDS = ((15, 19), (27, 31))


def _chat(app: App[None]) -> _ChatTextArea:
    return app.query_one("#chat-input", _ChatTextArea)


async def _expanded(pilot, app: _FieldsApp) -> _ChatTextArea:
    chat = _chat(app)
    await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
    app.query_one(InputBar).expand_prompt(ChatDraft(_TEXT, _TEXT_FIELDS))
    await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="prompt expanded")
    return chat


async def test_expand_selects_the_first_field_and_typing_replaces_it() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        assert chat.prompt_fields == _TEXT_FIELDS
        assert chat.selection == Selection((0, 15), (0, 19))
        assert chat.selected_text == "tone"

        await pilot.press("f", "u", "n")

        assert chat.text == "Translate in a fun tone:\n\ntext"
        # The touched field is gone; the later one moved left by one.
        assert chat.prompt_fields == ((26, 30),)
        assert app.submitted == []


async def test_tab_moves_through_fields_and_wraps() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        assert chat.selected_text == "tone"

        await pilot.press("tab")
        await wait_for(lambda: chat.selected_text == "text", pilot=pilot, description="second field selected")
        assert chat.selection.start == (2, 0)
        assert chat.selection.end == (2, 4)

        await pilot.press("tab")
        await wait_for(lambda: chat.selection.end == (0, 19), pilot=pilot, description="wrapped to first field")
        assert chat.selected_text == "tone"
        assert chat.text == _TEXT
        assert chat.prompt_fields == _TEXT_FIELDS


async def test_editing_elsewhere_shifts_later_fields_and_keeps_them() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        chat.move_cursor((0, 0))
        await pilot.press("X", "Y")

        assert chat.text == "XY" + _TEXT
        assert chat.prompt_fields == ((17, 21), (29, 33))


@pytest.mark.parametrize("pasted", ["x\r\ny", "x\ry", "x\ny"], ids=["crlf", "cr", "lf"])
async def test_pasting_lines_before_a_field_keeps_it_on_its_own_text(pasted: str) -> None:
    """Every way text reaches the draft gives it LF line ends, so a field moves by what the document stores."""
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        chat.move_cursor((0, 0))
        chat.post_message(events.Paste(pasted))
        await wait_for(lambda: chat.text == "x\ny" + _TEXT, pilot=pilot, description="pasted, line ends as LF")

        first, second = chat.prompt_fields
        assert chat.text[first[0] : first[1]] == "tone"
        assert chat.text[second[0] : second[1]] == "text"


async def test_backspace_after_a_field_deletes_it_whole_and_undo_restores_it() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        chat.move_cursor((2, 4))
        await pilot.press("backspace")

        assert chat.text == _TEXT[:27]
        assert chat.prompt_fields == ((15, 19),)

        chat.undo()
        await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="undo restored the field")
        assert chat.prompt_fields == _TEXT_FIELDS
        assert chat.selection == Selection.cursor((2, 4))

        chat.redo()
        assert chat.text == _TEXT[:27]
        assert chat.prompt_fields == ((15, 19),)


async def test_tab_selected_field_deletion_starts_its_own_undo_after_deleting_a_paste_label() -> None:
    references, entry = EMPTY_REFERENCES.with_new("paste", "one\ntwo\nthree\nfour")
    text = f"argument 1 argument 2 {entry.label}"
    draft = ChatDraft(text, fields=((0, 10), (11, 21)), references=references)
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, draft)
        await pilot.press("home", "tab", "backspace")
        assert chat.text == f" argument 2 {entry.label}"
        await pilot.press("end", "backspace")
        assert chat.text == " argument 2 "
        assert chat.prompt_fields == ((1, 11),)

        await pilot.press("tab", "backspace")
        assert chat.text == "  "
        assert chat.prompt_fields == ()
        await pilot.press("ctrl+z")
        assert chat.text == " argument 2 "
        assert chat.prompt_fields == ((1, 11),)
        assert iter_reference_labels(chat.text, chat.references) == []
        await pilot.press("ctrl+z")
        assert chat.text == f" argument 2 {entry.label}"
        assert chat.prompt_fields == ((1, 11),)
        await pilot.press("ctrl+z")
        assert chat.text == text
        assert chat.prompt_fields == draft.fields


async def test_undo_elsewhere_keeps_untouched_fields() -> None:
    """An edit outside a field only moves the fields after it, including on undo."""
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        chat.move_cursor((0, 4))
        await pilot.press("backspace")
        assert chat.text == "Traslate in a tone tone:\n\ntext"
        assert chat.prompt_fields == ((14, 18), (26, 30))

        chat.undo()
        await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="undo restored the text")
        assert chat.prompt_fields == _TEXT_FIELDS


async def test_undo_and_redo_of_typing_next_to_a_field_move_it_exactly() -> None:
    """Undo replays the recorded edit's own span: a character typed just before a
    field that starts with the same character is taken back from before the
    field, not out of it, so the field stays and Enter still asks first."""
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        assert _TEXT[15] == "t"
        chat.move_cursor((0, 15))
        await pilot.press("t", "t")
        assert chat.text == _TEXT[:15] + "tt" + _TEXT[15:]
        assert chat.prompt_fields == ((17, 21), (29, 33))

        chat.undo()
        await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="undo took the typing back")
        assert chat.prompt_fields == _TEXT_FIELDS

        chat.redo()
        await wait_for(lambda: chat.text != _TEXT, pilot=pilot, description="redo typed it again")
        assert chat.text == _TEXT[:15] + "tt" + _TEXT[15:]
        assert chat.prompt_fields == ((17, 21), (29, 33))

        chat.undo()
        await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="undone again")
        await pilot.press("enter")
        assert app.submitted == []


async def test_undo_of_typing_after_a_field_that_ends_alike_keeps_it() -> None:
    """The mirror case: a character typed just after a field that ends with it."""
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        assert _TEXT[18] == "e"
        chat.move_cursor((0, 19))
        await pilot.press("e")
        assert chat.prompt_fields == ((15, 19), (28, 32))

        chat.undo()
        await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="undo took the typing back")
        assert chat.prompt_fields == _TEXT_FIELDS


async def test_field_highlight_on_a_long_draft_reads_no_offsets_per_painted_line() -> None:
    """Each line's field columns are worked out once per change of the fields:
    painting a line far down a long draft never walks the lines above it."""
    head = "".join(f"line {number}\n" for number in range(20_000))
    text = head + "fill $X here\nspan $Y here\nafter"
    first = text.index("$X")
    second = text.index("$Y")
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = _chat(app)
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
        app.query_one(InputBar).expand_prompt(ChatDraft(text, ((first, first + 2), (second, second + 2))))
        await wait_for(lambda: chat.text == text, pilot=pilot, description="prompt expanded")
        style = chat.get_component_rich_style("draft--prompt-field", partial=False)

        def styled(row: int) -> list[tuple[int, int]]:
            return [(span.start, span.end) for span in chat.get_line(row).spans if span.style == style]

        assert styled(20_000) == [(5, 7)]
        document = chat.document
        walks: list[object] = []
        for name in ("get_index_from_location", "get_location_from_index"):
            original = getattr(document, name)

            def counted(*args: object, _original=original) -> object:
                walks.append(args)
                return _original(*args)

            setattr(document, name, counted)

        assert styled(20_001) == [(5, 7)]
        assert styled(20_002) == []
        assert styled(19_999) == []
        for row in range(19_980, 20_000):
            chat.get_line(row)
        assert walks == []


async def _draft(pilot, app: _FieldsApp, draft: ChatDraft) -> _ChatTextArea:
    chat = _chat(app)
    await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
    app.query_one(InputBar).set_draft(draft)
    await wait_for(lambda: chat.text == draft.text, pilot=pilot, description="draft written")
    return chat


@pytest.mark.parametrize(
    ("column", "key", "text", "fields"),
    [(1, "backspace", "fieldy", ((0, 5),)), (6, "delete", "xfield", ((1, 6),))],
    ids=["backspace-at-start", "delete-at-end"],
)
async def test_deleting_just_outside_a_field_keeps_it(column, key, text, fields) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        chat.move_cursor((0, column))
        await pilot.press(key)
        assert chat.text == text
        assert chat.prompt_fields == fields


@pytest.mark.parametrize(
    ("column", "key", "expected"),
    [(1, "right", 8), (8, "left", 1), (1, "ctrl+right", 8), (8, "ctrl+left", 1)],
)
async def test_cursor_and_word_moves_skip_a_whole_field(column: int, key: str, expected: int) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfill mey", ((1, 8),)))
        chat.move_cursor((0, column))
        await pilot.press(key)
        assert chat.selection == Selection.cursor((0, expected))
        assert chat.text == "xfill mey"
        assert chat.prompt_fields == ((1, 8),)


async def test_home_and_end_keep_the_cursor_outside_a_field() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("  tone tail", ((0, 6),)))
        await pilot.press("home")
        assert chat.selection == Selection.cursor((0, 0))
        await pilot.press("end")
        assert chat.selection == Selection.cursor((0, 11))
        assert chat.prompt_fields == ((0, 6),)


@pytest.mark.parametrize(
    ("column", "key", "selection"),
    [(1, "shift+right", Selection((0, 1), (0, 6))), (6, "shift+left", Selection((0, 6), (0, 1)))],
)
async def test_shift_arrows_select_a_whole_field(column: int, key: str, selection: Selection) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        chat.move_cursor((0, column))
        await pilot.press(key)
        assert chat.selection == selection
        assert chat.selected_text == "field"


@pytest.mark.parametrize(("column", "key"), [(2, "delete"), (9, "ctrl+w"), (2, "alt+delete")])
async def test_delete_and_word_delete_remove_a_whole_field(column: int, key: str) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("x fill me y", ((2, 9),)))
        chat.move_cursor((0, column))
        await pilot.press(key)
        assert chat.text == "x  y"
        assert chat.prompt_fields == ()


@pytest.mark.parametrize(("key", "text"), [("Z", "Zy"), ("backspace", "y")])
async def test_selection_from_outside_into_a_field_edits_the_whole_field(key: str, text: str) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        chat.move_cursor((0, 0))
        chat.selection = Selection((0, 0), (0, 3))
        assert chat.selected_text == "xfield"
        assert chat.selection == Selection((0, 0), (0, 6))
        await pilot.press(key)
        assert chat.text == text
        assert chat.prompt_fields == ()


@pytest.mark.parametrize("column", [1, 6], ids=["before", "after"])
@pytest.mark.parametrize("paste", [False, True], ids=["typing", "paste"])
async def test_inserting_at_a_field_edge_preserves_it(column: int, paste: bool) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        chat.move_cursor((0, column))
        if paste:
            chat.post_message(events.Paste("ZZ"))
            await wait_for(lambda: "ZZ" in chat.text, pilot=pilot, description="edge paste inserted")
        else:
            await pilot.press("Z", "Z")
        assert chat.text == ("xZZfieldy" if column == 1 else "xfieldZZy")
        assert chat.prompt_fields == (((3, 8),) if column == 1 else ((1, 6),))


async def test_replacing_a_selected_field_and_undoing_restores_the_field() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        await pilot.press("tab", "Z")
        assert chat.text == "xZy"
        assert chat.prompt_fields == ()
        chat.undo()
        assert chat.text == "xfieldy"
        assert chat.prompt_fields == ((1, 6),)
        assert chat.selected_text == "field"
        chat.redo()
        assert chat.text == "xZy"
        assert chat.prompt_fields == ()


async def test_pasting_the_same_field_text_keeps_the_field_and_its_undo_step() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        await pilot.press("X", "tab")
        assert chat.selected_text == "field"

        chat.post_message(events.Paste("field"))
        await wait_for(lambda: chat.selection.is_empty, pilot=pilot, description="same-text paste completed")
        assert chat.text == "xfieldyX"
        assert chat.prompt_fields == ((1, 6),)
        assert chat.selection == Selection.cursor((0, 6))

        chat.undo()
        assert chat.text == "xfieldyX"
        assert chat.prompt_fields == ((1, 6),)
        assert chat.selected_text == "field"

        chat.redo()
        assert chat.text == "xfieldyX"
        assert chat.prompt_fields == ((1, 6),)
        assert chat.selection == Selection.cursor((0, 6))

        chat.undo()
        chat.undo()
        assert chat.text == "xfieldy"
        assert chat.prompt_fields == ((1, 6),)


async def test_undo_of_a_crlf_paste_restores_fields_removed_by_the_edit() -> None:
    # The document keeps CRLF line breaks, so offsets after one count two characters for it:
    # a different replacement ends both fields, even when one old letter is written back.
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = _chat(app)
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
        chat.load_text("xA\r\nBy")
        chat.set_prompt_fields(((1, 2), (4, 5)), select=False)
        chat.select_all()

        chat.post_message(events.Paste("zA\r\nCy"))
        await wait_for(lambda: chat.selection.is_empty, pilot=pilot, description="CRLF paste completed")
        assert chat.text == "zA\r\nCy"
        assert chat.prompt_fields == ()

        chat.undo()
        assert chat.text == "xA\r\nBy"
        assert chat.prompt_fields == ((1, 2), (4, 5))

        chat.redo()
        assert chat.text == "zA\r\nCy"
        assert chat.prompt_fields == ()


async def test_pasting_the_same_text_into_a_crlf_draft_keeps_every_field() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = _chat(app)
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
        chat.load_text("xA\r\nBy")
        chat.set_prompt_fields(((1, 2), (4, 5)), select=False)
        chat.select_all()

        chat.post_message(events.Paste("xA\nBy"))
        await wait_for(lambda: chat.selection.is_empty, pilot=pilot, description="paste completed")
        assert chat.text == "xA\r\nBy"
        assert chat.prompt_fields == ((1, 2), (4, 5))

        chat.undo()
        assert chat.text == "xA\r\nBy"
        assert chat.prompt_fields == ((1, 2), (4, 5))
        chat.redo()
        assert chat.text == "xA\r\nBy"
        assert chat.prompt_fields == ((1, 2), (4, 5))


@pytest.mark.parametrize("replace_field", [False, True], ids=["insert-at-edge", "replace-selected-field"])
async def test_reference_insertion_moves_or_replaces_the_field_by_its_range(replace_field: bool) -> None:
    draft = ChatDraft("ab field-one cd", fields=((3, 12),))
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, draft)
        chat.selection = Selection((0, 3), (0, 12)) if replace_field else Selection.cursor((0, 3))

        assert chat.insert_reference("quote", "said")
        assert chat.text == ("ab [Quote #1] cd" if replace_field else "ab [Quote #1] field-one cd")
        assert chat.prompt_fields == (() if replace_field else ((14, 23),))
        assert app.query_one(InputBar).draft().sent_text() == (
            "ab \n> said\n\n cd" if replace_field else "ab \n> said\n\n field-one cd"
        )
        chat.undo()
        assert chat.text == draft.text
        assert chat.prompt_fields == draft.fields


async def test_pasting_a_folded_label_over_a_field_and_undoing_restores_both() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        await pilot.press("tab")
        chat.post_message(events.Paste("one\ntwo\nthree\nfour\nfive\nsix"))
        await wait_for(lambda: "[Pasted text #1" in chat.text, pilot=pilot, description="paste folded")
        assert chat.text == "x[Pasted text #1 +5 lines]y"
        assert chat.prompt_fields == ()
        assert app.query_one(InputBar).draft().sent_text() == "xone\ntwo\nthree\nfour\nfive\nsixy"
        chat.undo()
        assert chat.text == "xfieldy"
        assert chat.prompt_fields == ((1, 6),)
        assert list(iter_reference_labels(chat.text, chat.references)) == []
        chat.redo()
        assert chat.text == "x[Pasted text #1 +5 lines]y"
        assert chat.prompt_fields == ()
        assert app.query_one(InputBar).draft().sent_text() == "xone\ntwo\nthree\nfour\nfive\nsixy"


async def test_adjacent_fields_are_separate_whole_units() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfirstsecondy", ((1, 6), (6, 12))))
        chat.move_cursor((0, 1))
        await pilot.press("right")
        assert chat.selection == Selection.cursor((0, 6))
        await pilot.press("right")
        assert chat.selection == Selection.cursor((0, 12))
        await pilot.press("backspace")
        assert chat.text == "xfirsty"
        assert chat.prompt_fields == ((1, 6),)


async def test_replacing_two_fields_restores_their_order_on_undo_and_redo() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfirstsecondy", ((1, 6), (6, 12))))
        chat.selection = Selection((0, 1), (0, 12))
        await pilot.press("Z")
        assert chat.text == "xZy"
        assert chat.prompt_fields == ()
        chat.undo()
        assert chat.text == "xfirstsecondy"
        assert chat.prompt_fields == ((1, 6), (6, 12))
        assert chat.selected_text == "firstsecond"
        chat.redo()
        assert chat.text == "xZy"
        assert chat.prompt_fields == ()


async def test_undo_of_batched_deletes_restores_fields_in_tab_order() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfirstsecondy", ((1, 6), (6, 12))))
        chat.move_cursor((0, 1))
        await pilot.press("delete", "delete")
        assert chat.text == "xy"
        assert chat.prompt_fields == ()
        chat.undo()
        assert chat.text == "xfirstsecondy"
        assert chat.prompt_fields == ((1, 6), (6, 12))
        await pilot.press("tab")
        assert chat.selected_text == "first"
        await pilot.press("tab")
        assert chat.selected_text == "second"


async def test_a_field_next_to_a_label_is_deleted_without_splitting_either() -> None:
    references, _entry = EMPTY_REFERENCES.with_new("quote", "said")
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("x[Quote #1]fieldy", ((11, 16),), references=references))
        chat.move_cursor((0, 16))
        await pilot.press("backspace")
        assert chat.text == "x[Quote #1]y"
        assert chat.prompt_fields == ()
        await pilot.press("shift+left")
        assert chat.selected_text == "[Quote #1]"
        await pilot.press("backspace")
        assert chat.text == "xy"
        chat.undo()
        assert chat.text == "x[Quote #1]y"
        assert chat.prompt_fields == ()
        chat.undo()
        assert chat.text == "x[Quote #1]fieldy"
        assert chat.prompt_fields == ((11, 16),)
        assert app.query_one(InputBar).draft().sent_text() == "x\n> said\n\nfieldy"


async def test_an_edit_reaching_overlapping_label_and_field_replays_both_whole() -> None:
    references, _entry = EMPTY_REFERENCES.with_new("quote", "said")
    draft = ChatDraft("x[Quote #1]toney", ((9, 15),), references=references)
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, draft)
        chat.replace("Z", (0, 12), (0, 13))
        assert chat.text == "xZy"
        assert chat.prompt_fields == ()
        for _ in range(2):
            chat.undo()
            assert chat.text == draft.text
            assert chat.prompt_fields == draft.fields
            assert app.query_one(InputBar).draft().sent_text() == "x\n> said\n\ntoney"
            chat.redo()
            assert chat.text == "xZy"
            assert chat.prompt_fields == ()


async def test_clicking_inside_a_field_selects_it_for_replacement() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        await click_when_settled(pilot, chat, offset=(4, 0))
        assert chat.selection == Selection((0, 1), (0, 6))
        assert chat.selected_text == "field"
        await pilot.press("Z")
        assert chat.text == "xZy"
        assert chat.prompt_fields == ()


@pytest.mark.parametrize("cell", [0, 1], ids=["wide-character-left", "wide-character-right"])
async def test_clicking_the_first_character_of_a_field_selects_the_whole_field(cell: int) -> None:
    app = _FieldsApp()
    async with app.run_test(size=(80, 20)) as pilot:
        chat = await _draft(pilot, app, ChatDraft("ab 参数1 cd", ((3, 6),)))
        await click_when_settled(pilot, chat, offset=(4 + cell, 0))
        assert chat.text == "ab 参数1 cd"
        assert chat.prompt_fields == ((3, 6),)
        assert chat.selection == Selection((0, 3), (0, 6))
        assert chat.selected_text == "参数1"


@pytest.mark.parametrize(
    ("start", "moves", "selection", "selected"),
    [
        (3, (4, 3), Selection.cursor((0, 1)), ""),
        (3, (7, 3), Selection.cursor((0, 1)), ""),
        (7, (3,), Selection((0, 7), (0, 1)), "fieldy"),
    ],
    ids=["inside", "out-and-back", "reverse"],
)
async def test_dragging_does_not_trigger_the_field_click_selection(start, moves, selection, selected) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        await pilot.mouse_down(chat, offset=(start + 1, 0))
        for column in moves:
            await pilot._post_mouse_events([events.MouseMove], chat, offset=(column + 1, 0), button=1)
        await pilot._post_mouse_events([events.MouseUp, events.Click], chat, offset=(moves[-1] + 1, 0), button=1)
        assert chat.selection == selection
        assert chat.selected_text == selected
        assert chat.prompt_fields == ((1, 6),)


async def test_hovering_after_a_click_reads_no_document_locations(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        await click_when_settled(pilot, chat, offset=(1, 0))
        reads: list[object] = []
        read = chat.get_target_document_location

        def counted(event: events.MouseEvent) -> Location:
            reads.append(event)
            return read(event)

        monkeypatch.setattr(chat, "get_target_document_location", counted)
        await pilot.hover(chat, offset=(3, 0))
        await pilot.hover(chat, offset=(5, 0))
        assert reads == []


@pytest.mark.parametrize("emptied_by", ["select-all", "batched-backspaces", "editor-save"])
async def test_undo_after_emptying_a_draft_restores_its_fields_but_not_its_owner(emptied_by: str) -> None:
    # One rule for every way a draft is emptied: undo brings back what the edit removed,
    # fields included; the owner the emptying ended stays ended (backlog item).
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfield", ((1, 6),), ShortcutIntent("test", "", "")))
        if emptied_by == "batched-backspaces":
            await pilot.press("backspace", "backspace")
        elif emptied_by == "select-all":
            chat.select_all()
            await pilot.press("backspace")
        else:
            app.query_one(InputBar).replace_editor_draft(ChatDraft(""), cursor_location=(0, 0))
        assert chat.text == ""
        assert chat.prompt_fields == ()
        chat.undo()
        assert chat.text == "xfield"
        assert chat.prompt_fields == ((1, 6),)
        assert chat.chat_draft().owner is None


async def test_cutting_into_a_field_takes_it_whole_and_undo_restores_it() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        chat.move_cursor((0, 13))
        await pilot.press("shift+right", "shift+right", "shift+right")
        assert chat.selected_text == "a tone"
        await pilot.press("ctrl+x")

        assert app.clipboard == "a tone"
        assert chat.text == "Translate in  tone:\n\ntext"
        assert chat.prompt_fields == ((21, 25),)

        chat.undo()
        assert chat.text == _TEXT
        assert chat.prompt_fields == _TEXT_FIELDS


async def test_a_sent_draft_taken_back_restores_fields_once() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        draft = ChatDraft("field tail", ((0, 5),))
        chat = await _draft(pilot, app, draft)
        await pilot.press("enter", "enter")
        assert chat.text == ""
        assert app.query_one(InputBar).restore_draft(draft)
        assert chat.text == "field tail"
        assert chat.prompt_fields == ((0, 5),)
        assert chat.selection.start[1] not in range(1, 5)
        assert chat.selection.end[1] not in range(1, 5)
        await pilot.press("tab")
        assert chat.selected_text == "field"


async def test_undoing_a_send_then_a_field_delete_does_not_duplicate_fields() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        chat.move_cursor((0, 6))
        await pilot.press("backspace", "enter")
        assert app.submitted == ["xy"]
        assert chat.text == ""
        await pilot.press("ctrl+z")
        assert chat.text == "xy"
        assert chat.prompt_fields == ()
        await pilot.press("ctrl+z")
        assert chat.text == "xfieldy"
        assert chat.prompt_fields == ((1, 6),)


async def test_an_ownerless_draft_keeps_whole_field_editing_and_undo() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfieldy", ((1, 6),)))
        assert app.query_one(InputBar).draft_owner is None
        chat.move_cursor((0, 1))
        await pilot.press("delete")
        assert chat.text == "xy"
        assert chat.prompt_fields == ()
        chat.undo()
        assert chat.text == "xfieldy"
        assert chat.prompt_fields == ((1, 6),)
        assert app.query_one(InputBar).draft_owner is None


async def test_completion_reaching_into_a_field_replaces_it_whole() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _draft(pilot, app, ChatDraft("xfield@query", ((1, 12),)))
        app.query_one(InputBar).replace_trigger_text("@", "chosen")
        assert chat.text == "xchosen"
        assert chat.prompt_fields == ()
        chat.undo()
        assert chat.text == "xfield@query"
        assert chat.prompt_fields == ((1, 12),)


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
async def test_setting_fields_revalidates_the_selection_and_drops_multiline_spans(line_ending: str) -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = _chat(app)
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
        text = "field" + line_ending + "next tail"
        chat.load_text(text)
        second_line = chat.document.get_index_from_location((1, 0))
        chat.selection = Selection((0, 2), (1, 2))
        chat.set_prompt_fields(((0, 5), (3, second_line + 2), (second_line, second_line + 4)), select=False)
        assert chat.text == text
        assert chat.prompt_fields == ((0, 5), (second_line, second_line + 4))
        assert chat.selection == Selection((0, 5), (1, 4))
        assert chat.selected_text == line_ending + "next"


async def test_enter_with_untouched_fields_needs_a_second_press(monkeypatch: pytest.MonkeyPatch) -> None:
    hold_prompt_fields_confirm_window(monkeypatch)
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)

        await pilot.press("enter")
        assert app.submitted == []
        assert chat.text == _TEXT

        await pilot.press("enter")
        await wait_for(lambda: app.submitted == [_TEXT], pilot=pilot, description="second Enter sent")


async def test_enter_sends_at_once_after_every_field_is_filled() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        await pilot.press("a")
        await pilot.press("tab")
        await wait_for(lambda: chat.selected_text == "text", pilot=pilot, description="second field selected")
        await pilot.press("b")
        assert chat.prompt_fields == ()

        await pilot.press("enter")
        await wait_for(
            lambda: app.submitted == ["Translate in a a tone:\n\nb"], pilot=pilot, description="sent on first Enter"
        )


async def test_expand_without_fields_replaces_the_draft_and_sends_at_once() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = _chat(app)
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
        await pilot.press("h", "i")
        assert chat.text == "hi"

        app.query_one(InputBar).expand_prompt(ChatDraft("plain"))
        await wait_for(lambda: chat.text == "plain", pilot=pilot, description="expanded without fields")
        assert chat.prompt_fields == ()
        assert app.submitted == []

        await pilot.press("enter")
        await wait_for(lambda: app.submitted == ["plain"], pilot=pilot, description="no-field prompt sends at once")


# ---------------------------------------------------------------------------
# The Retry/Continue button guards unfilled fields as Enter does
# ---------------------------------------------------------------------------

_PENDING_WARNING = "highlighted placeholders are not filled in"


def _pending_warnings(app: App[None]) -> list[str]:
    return [n.message for n in app._notifications if _PENDING_WARNING in n.message]


async def _retry_state(pilot, app: _FieldsApp) -> InputBar:
    input_bar = app.query_one(InputBar)
    input_bar.retry_mode = True
    await wait_for(lambda: input_bar.retry_mode, pilot=pilot, description="retry/continue state")
    return input_bar


async def test_enter_in_retry_state_with_untouched_fields_warns_first() -> None:
    """Positive control: the warning the button must match."""
    app = _FieldsApp()
    async with app.run_test() as pilot:
        await _retry_state(pilot, app)
        chat = await _expanded(pilot, app)

        await pilot.press("enter")
        await wait_for(lambda: _pending_warnings(app), pilot=pilot, description="Enter warned")
        assert app.retried == []
        assert chat.text == _TEXT


async def test_retry_button_with_untouched_fields_needs_a_second_click(monkeypatch: pytest.MonkeyPatch) -> None:
    hold_prompt_fields_confirm_window(monkeypatch)
    app = _FieldsApp()
    async with app.run_test() as pilot:
        await _retry_state(pilot, app)
        chat = await _expanded(pilot, app)

        await click_when_settled(pilot, "#send-btn")
        await wait_for(lambda: _pending_warnings(app), pilot=pilot, description="button warned")
        assert _pending_warnings(app) == [
            "2 highlighted placeholders are not filled in. Press Enter again to send as is."
        ]
        assert app.retried == []
        assert app.submitted == []
        assert chat.text == _TEXT

        # A Button ignores clicks during its short press effect.
        button = app.query_one("#send-btn")
        await wait_for(lambda: not button.has_class("-active"), pilot=pilot, description="press effect over")
        await click_when_settled(pilot, "#send-btn")
        await wait_for(lambda: app.retried == [_TEXT.strip()], pilot=pilot, description="second click sent it")
        assert app.submitted == []
        assert chat.text == ""


async def test_retry_button_with_an_empty_draft_just_continues() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = _chat(app)
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
        await _retry_state(pilot, app)

        await click_when_settled(pilot, "#send-btn")
        await wait_for(lambda: app.retried == [""], pilot=pilot, description="continued at once")
        assert _pending_warnings(app) == []
