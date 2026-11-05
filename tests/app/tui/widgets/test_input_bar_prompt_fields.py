# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for prompt-shortcut fields in the chat input: tracking, Tab jumps and the send guard."""

from __future__ import annotations

import pytest
from textual import events
from textual.app import App, ComposeResult

from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar, _ChatTextArea, shift_prompt_fields
from tests.support.tui_helpers import click_when_settled
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


async def test_backspace_into_a_field_drops_it_and_undo_does_not_restore_it() -> None:
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        chat.move_cursor((2, 4))
        await pilot.press("backspace")

        assert chat.text == _TEXT[:-1]
        assert chat.prompt_fields == ((15, 19),)

        chat.undo()
        await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="undo restored the text")
        assert (27, 31) not in chat.prompt_fields


async def test_undo_elsewhere_keeps_untouched_fields() -> None:
    """Spec 4: only an edit that touches a field drops it; edits elsewhere just shift later fields."""
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = await _expanded(pilot, app)
        chat.move_cursor((2, 4))
        await pilot.press("backspace")
        assert chat.prompt_fields == ((15, 19),)

        chat.undo()
        await wait_for(lambda: chat.text == _TEXT, pilot=pilot, description="undo restored the text")
        assert chat.prompt_fields == ((15, 19),)


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
    text = head + "fill $X here\nspan $Y\nacross"
    first = text.index("$X")
    second = text.index("$Y")
    app = _FieldsApp()
    async with app.run_test() as pilot:
        chat = _chat(app)
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
        # The second field runs from ``$Y`` across the line break into ``across``.
        app.query_one(InputBar).expand_prompt(ChatDraft(text, ((first, first + 2), (second, len(text) - 3))))
        await wait_for(lambda: chat.text == text, pilot=pilot, description="prompt expanded")
        style = chat.get_component_rich_style("chat-input--prompt-field", partial=False)

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
        assert styled(20_002) == [(0, 3)]
        assert styled(19_999) == []
        for row in range(19_980, 20_000):
            chat.get_line(row)
        assert walks == []


async def test_enter_with_untouched_fields_needs_a_second_press() -> None:
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


async def test_retry_button_with_untouched_fields_needs_a_second_click() -> None:
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
