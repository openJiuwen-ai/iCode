# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The full-text editor preserves whole prompt fields across editing and a round trip."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste
from textual.widgets.text_area import Selection

from chrys.app.tui.screens.dialogs.editor import EditorDialog, EditorDialogResult
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar
from chrys.app.tui.widgets.chrome.prompt_draft import ShortcutIntent
from chrys.app.tui.widgets.editor import EditorBufferSnapshot, EditorMode, MessageEditor
from chrys.foundation.events.bus import EventBus
from chrys.foundation.text.draft_references import (
    EMPTY_REFERENCES,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

_SIX_LINES = "def f():\n    return 1\n\nprint(f())\nprint(f() + 1)\nprint(f() + 2)"


@pytest.fixture(autouse=True)
def _no_branch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


class _App(TuiVariableDefaultsMixin, App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[EditorDialogResult] = []


async def _open(
    app: _App, pilot, draft: ChatDraft, *, cursor: tuple[int, int] = (0, 0), mode: EditorMode = EditorMode.STANDARD
) -> DraftEditor:
    def record_result(result: EditorDialogResult | None) -> None:
        if result is not None:
            app.results.append(result)

    dialog = EditorDialog(
        EditorBufferSnapshot(draft.text, cursor),
        mode=mode,
        draft=draft,
    )
    app.push_screen(dialog, record_result)
    await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="editor open")
    editor = dialog.query_one(DraftEditor)
    await wait_for(lambda: editor.has_focus, pilot=pilot, description="editor focused")
    return editor


async def _commit(app: _App, pilot) -> EditorDialogResult:
    await pilot.press("ctrl+o")
    await wait_for(lambda: bool(app.results), pilot=pilot, description="editor committed")
    return app.results[-1]


@pytest.mark.asyncio
async def test_a_draft_the_editor_leaves_untouched_comes_back_whole() -> None:
    draft = ChatDraft("fix argument now", fields=((4, 12),), owner=ShortcutIntent("fix", "", ""))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft)
        assert editor.chat_draft().references == EMPTY_REFERENCES
        assert editor.prompt_fields == ((4, 12),)

        result = await _commit(app, pilot)

        assert result.accepted
        assert result.draft == draft


@pytest.mark.asyncio
async def test_a_long_paste_in_the_editor_stays_full_text() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _open(app, pilot, ChatDraft("x "), cursor=(0, 2))

        await editor._on_paste(Paste(_SIX_LINES))

        assert editor.text == f"x {_SIX_LINES}"
        assert editor.chat_draft().references == EMPTY_REFERENCES
        result = await _commit(app, pilot)
        assert result.draft.sent_text() == f"x {_SIX_LINES}"


@pytest.mark.parametrize(
    ("mode", "jumps"), [(EditorMode.STANDARD, True), (EditorMode.EMACS, True), (EditorMode.VIM, False)]
)
@pytest.mark.asyncio
async def test_tab_goes_to_the_next_field_only_while_text_can_be_typed(mode: EditorMode, jumps: bool) -> None:
    app = _App()
    async with app.run_test() as pilot:
        draft = ChatDraft("Review A then B", fields=((7, 8), (14, 15)))
        editor = await _open(app, pilot, draft, mode=mode)
        editor.move_cursor((0, 0))

        await pilot.press("tab")

        assert (sorted(editor.selection) == [(0, 7), (0, 8)]) is jumps
        assert editor.prompt_fields == ((7, 8), (14, 15))


@pytest.mark.asyncio
async def test_a_plain_message_editor_keeps_label_text_plain_and_pastes_inline() -> None:
    class _Plain(TuiVariableDefaultsMixin, App[None]):
        def compose(self) -> ComposeResult:
            yield MessageEditor(text="[Quote #1] ")

    app = _Plain()
    async with app.run_test(size=(100, 30)) as pilot:
        editor = app.query_one(MessageEditor)
        editor.focus()
        editor.move_cursor((0, 11))
        await pilot.pause()

        await editor._on_paste(Paste(_SIX_LINES))

        assert editor.text == f"[Quote #1] {_SIX_LINES}"


class _MainApp(TuiVariableDefaultsMixin, App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


@pytest.mark.asyncio
async def test_the_input_box_and_the_editor_hand_one_draft_back_and_forth() -> None:
    app = _MainApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        input_bar.value = "explain "
        assert input_bar.insert_reference("quote", "it failed")
        await pilot.pause()

        await pilot.press("ctrl+o")
        await wait_for(lambda: isinstance(app.screen, EditorDialog), pilot=pilot, description="editor open")
        editor = app.screen.query_one(DraftEditor)
        assert editor.text == "explain \n> it failed\n\n "
        assert editor.chat_draft().references == EMPTY_REFERENCES

        editor.move_cursor(editor.document.end)
        await pilot.press("n", "o", "w")
        await pilot.press("ctrl+o")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot, description="editor committed")

        assert input_bar.value == "explain \n> it failed\n\n now"
        assert input_bar.draft().sent_text() == "explain \n> it failed\n\n now"


@pytest.mark.asyncio
async def test_the_editor_steps_over_a_field_and_backspace_undo_restores_it_whole() -> None:
    draft = ChatDraft("ab field-one cd", fields=((3, 12),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, cursor=(0, 3))

        await pilot.press("right")
        assert editor.selection.start == editor.selection.end == (0, 12)
        await pilot.press("left")
        assert editor.selection.start == editor.selection.end == (0, 3)
        await pilot.press("right", "backspace")
        assert editor.text == "ab  cd"
        assert editor.prompt_fields == ()

        await pilot.press("ctrl+z")
        assert editor.text == draft.text
        assert editor.prompt_fields == draft.fields
        assert all(not 3 < column < 12 for _row, column in editor.selection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cursor", "key", "expected"),
    [
        ((0, 3), "l", (0, 12)),
        ((0, 12), "h", (0, 3)),
        ((0, 3), "w", (0, 13)),
        ((0, 12), "b", (0, 3)),
        ((0, 12), "0", (0, 0)),
        ((0, 3), "dollar_sign", (0, 14)),
    ],
)
async def test_vim_motions_never_rest_inside_a_prompt_field(
    cursor: tuple[int, int], key: str, expected: tuple[int, int]
) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(
            app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), cursor=cursor, mode=EditorMode.VIM
        )

        await pilot.press(key)

        assert editor.selection.start == editor.selection.end == expected
        assert editor.prompt_fields == ((3, 12),)


@pytest.mark.asyncio
async def test_vim_x_and_put_take_the_whole_prompt_field() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(
            app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), cursor=(0, 3), mode=EditorMode.VIM
        )

        await pilot.press("x")
        assert editor.text == "ab  cd"
        assert editor.prompt_fields == ()
        await pilot.press("u")
        assert editor.text == "ab field-one cd"
        assert editor.prompt_fields == ((3, 12),)
        await pilot.press("ctrl+r")
        assert editor.text == "ab  cd"
        assert editor.prompt_fields == ()
        await pilot.press("P")
        assert editor.text == "ab field-one cd"
        assert editor.prompt_fields == ()


@pytest.mark.asyncio
async def test_vim_visual_yank_of_a_field_puts_its_whole_text() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(
            app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), cursor=(0, 3), mode=EditorMode.VIM
        )

        await pilot.press("v")
        assert editor.selected_text == "field-one"
        await pilot.press("y")
        editor.move_cursor((0, 0))
        await pilot.press("P")

        assert editor.text == "field-oneab field-one cd"
        assert editor.prompt_fields == ((12, 21),)


@pytest.mark.asyncio
@pytest.mark.parametrize(("motion", "expected"), [("w", "ab cd"), ("dollar_sign", "ab ")])
async def test_vim_word_and_line_deletes_never_leave_part_of_a_field(motion: str, expected: str) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(
            app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), cursor=(0, 3), mode=EditorMode.VIM
        )

        await pilot.press("d", motion)

        assert editor.text == expected
        assert editor.prompt_fields == ()
        await pilot.press("u")
        assert editor.text == "ab field-one cd"
        assert editor.prompt_fields == ((3, 12),)


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [("dollar_sign", "dollar_sign"), ("l", "l", "l", "l", "l")])
async def test_vim_normal_mode_rests_at_the_start_of_a_field_at_line_end(keys: tuple[str, ...]) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab field-one", fields=((3, 12),)), mode=EditorMode.VIM)

        for key in keys:
            await pilot.press(key)
            assert all(not 3 < column < 12 for _row, column in editor.selection)

        assert editor.selection.start == editor.selection.end == (0, 3)


@pytest.mark.asyncio
async def test_vim_tilde_on_a_field_preserves_it_through_undo_and_redo() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), mode=EditorMode.VIM)
        await pilot.press("r", "B", "w")
        assert editor.text == "Bb field-one cd"
        assert editor.selection.start == editor.selection.end == (0, 3)

        await pilot.press("~")

        assert editor.text == "Bb field-one cd"
        assert editor.prompt_fields == ((3, 12),)
        assert editor.selection.start == editor.selection.end == (0, 12)

        await pilot.press("u")
        assert editor.text == "Bb field-one cd"
        assert editor.prompt_fields == ((3, 12),)

        await pilot.press("ctrl+r")
        assert editor.text == "Bb field-one cd"
        assert editor.prompt_fields == ((3, 12),)


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "toggled"), [("3", "A argument 1 z"), ("5", "A argument 1 Z")])
async def test_vim_counted_tilde_across_a_field_toggles_only_the_text_around_it(count: str, toggled: str) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("a argument 1 z", fields=((2, 12),)), mode=EditorMode.VIM)

        await pilot.press(count, "~")
        assert editor.text == toggled
        assert editor.prompt_fields == ((2, 12),)

        await pilot.press("u")
        assert editor.text == "a argument 1 z"
        assert editor.prompt_fields == ((2, 12),)

        await pilot.press("ctrl+r")
        assert editor.text == toggled
        assert editor.prompt_fields == ((2, 12),)

        result = await _commit(app, pilot)
        assert result.draft.text == toggled
        assert result.draft.fields == ((2, 12),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft",
    [ChatDraft("ab field-one cd", fields=((3, 12),))],
    ids=["field"],
)
async def test_vim_visual_steps_over_a_whole_unit_in_one_motion(draft: ChatDraft) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=EditorMode.VIM)
        unit_end = 12 if draft.fields else 13

        await pilot.press("v", "l", "l", "l")
        assert editor.selection == Selection((0, 0), (0, unit_end))
        await pilot.press("l")
        assert editor.selection == Selection((0, 0), (0, unit_end + 1))
        await pilot.press("h")
        assert editor.selection == Selection((0, 0), (0, unit_end))
        await pilot.press("d")
        assert editor.text == " cd"
        assert editor.prompt_fields == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "keys", "selected", "after_delete"),
    [
        ("ab field-one", ("v", "dollar_sign"), "ab field-one", ""),
        ("ab field-one cd", ("v", "w"), "ab field-one", " cd"),
        ("ab field-one cd", ("v", "w", "w"), "ab field-one c", "d"),
    ],
    ids=["dollar-onto-a-field-ending-the-line", "w-onto-a-field", "w-out-of-a-field"],
)
async def test_vim_visual_word_and_line_end_motions_take_a_field_whole(
    text: str, keys: tuple[str, ...], selected: str, after_delete: str
) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft(text, fields=((3, 12),)), mode=EditorMode.VIM)

        await pilot.press(*keys)
        assert editor.selected_text == selected
        await pilot.press("d")
        assert editor.text == after_delete
        assert editor.prompt_fields == ()


@pytest.mark.asyncio
async def test_vim_join_that_leaves_a_field_in_place_keeps_it() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab\nargument 1", fields=((3, 13),)), mode=EditorMode.VIM)

        await pilot.press("J")
        assert editor.text == "ab argument 1"
        assert editor.prompt_fields == ((3, 13),)

        await pilot.press("u")
        assert editor.text == "ab\nargument 1"
        assert editor.prompt_fields == ((3, 13),)


@pytest.mark.asyncio
async def test_emacs_word_motions_step_over_the_whole_prompt_field() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(
            app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), cursor=(0, 3), mode=EditorMode.EMACS
        )

        await pilot.press("alt+f")
        assert editor.selection.start == editor.selection.end == (0, 12)
        await pilot.press("alt+b")
        assert editor.selection.start == editor.selection.end == (0, 3)
        assert editor.prompt_fields == ((3, 12),)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cursor", "kill", "expected"),
    [((0, 3), "alt+d", "ab  cd"), ((0, 12), "ctrl+w", "ab  cd"), ((0, 3), "ctrl+k", "ab ")],
)
async def test_emacs_kill_and_yank_take_the_whole_prompt_field(
    cursor: tuple[int, int], kill: str, expected: str
) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(
            app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), cursor=cursor, mode=EditorMode.EMACS
        )

        await pilot.press(kill)
        assert editor.text == expected
        assert editor.prompt_fields == ()
        await pilot.press("ctrl+y")
        assert editor.text == "ab field-one cd"
        assert editor.prompt_fields == ()


@pytest.mark.asyncio
async def test_clicking_a_prompt_field_in_the_editor_selects_it_for_replacement() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)))
        content = editor.content_region

        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 6, content.y - editor.region.y))

        assert editor.selected_text == "field-one"
        assert sorted(editor.selection) == [(0, 3), (0, 12)]
        await pilot.press("X")
        assert editor.text == "ab X cd"
        assert editor.prompt_fields == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "keys", "text", "fields"),
    [
        (EditorMode.EMACS, ("X",), "ab X cd", ()),
        (EditorMode.VIM, ("i", "X"), "ab Xfield-one cd", ((4, 13),)),
    ],
    ids=["emacs-selects", "vim-normal-rests-on-start"],
)
async def test_clicking_a_prompt_field_selects_it_only_while_text_can_be_typed(
    mode: EditorMode, keys: tuple[str, ...], text: str, fields: tuple[tuple[int, int], ...]
) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), mode=mode)
        content = editor.content_region

        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 6, content.y - editor.region.y))

        if mode is EditorMode.VIM:
            assert editor.selection == Selection.cursor((0, 3))
        else:
            assert sorted(editor.selection) == [(0, 3), (0, 12)]
        await pilot.press(*keys)
        assert editor.text == text
        assert editor.prompt_fields == fields


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_first", [False, True], ids=["edit-outside-fields", "fill-one-field"])
async def test_editor_confirmation_keeps_the_unfilled_fields_in_the_input_box(fill_first: bool) -> None:
    app = _MainApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        input_bar.set_draft(ChatDraft("ab field-one then field-two cd", fields=((3, 12), (18, 27))))

        await pilot.press("ctrl+o")
        await wait_for(lambda: isinstance(app.screen, EditorDialog), pilot=pilot, description="editor open")
        editor = app.screen.query_one(DraftEditor)
        await wait_for(lambda: editor.has_focus, pilot=pilot, description="editor focused")
        editor.move_cursor((0, 0))
        if fill_first:
            await pilot.press("tab", "X")
            assert editor.text == "ab X then field-two cd"
            assert editor.prompt_fields == ((10, 19),)
        else:
            await pilot.press("X")
            assert editor.text == "Xab field-one then field-two cd"
            assert editor.prompt_fields == ((4, 13), (19, 28))

        await pilot.press("ctrl+o")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot, description="editor committed")

        assert input_bar.value == editor.text
        assert input_bar.draft().fields == (((10, 19),) if fill_first else ((4, 13), (19, 28)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix",
    [(), ("v",), ("V",), ("d",), ("d", "i"), ("r",), ("g",), ("Z",), ("3",), (":",)],
    ids=["normal", "visual-char", "visual-line", "operator", "text-object", "replace", "g", "Z", "count", "command"],
)
async def test_a_true_field_click_rests_on_its_start_in_non_typing_vim_states(prefix: tuple[str, ...]) -> None:
    draft = ChatDraft("ab field-one cd", fields=((3, 12),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=EditorMode.VIM)
        await pilot.press(*prefix)
        content = editor.content_region
        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 6, content.y - editor.region.y))
        assert editor.selection == Selection.cursor((0, 3))
        assert editor.chat_draft() == draft


@pytest.mark.asyncio
async def test_vim_normal_click_then_x_deletes_the_clicked_field_and_u_restores_it() -> None:
    draft = ChatDraft("ab field-one cd", fields=((3, 12),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=EditorMode.VIM)
        content = editor.content_region
        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 6, content.y - editor.region.y))
        assert editor.selection == Selection.cursor((0, 3))
        await pilot.press("x")
        assert editor.text == "ab  cd"
        assert editor.prompt_fields == ()
        await pilot.press("u")
        assert editor.chat_draft() == draft


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "count", "expected", "field", "cursor"),
    [
        ("ß argument 1 z", "3", "SS argument 1 z", (3, 13), 13),
        ("ﬃ argument 1 z", "3", "FFI argument 1 z", (4, 14), 14),
        ("İ argument 1 z", "3", "i̇ argument 1 z", (3, 13), 13),
        ("argument 1 ß z", "3", "argument 1 SS z", (0, 10), 13),
        ("argument 1 ﬃ z", "3", "argument 1 FFI z", (0, 10), 14),
        ("argument 1 İ z", "3", "argument 1 i̇ z", (0, 10), 13),
        ("ß argument 1 ß z", "5", "SS argument 1 SS z", (3, 13), 16),
        ("ﬃ argument 1 ﬃ z", "5", "FFI argument 1 FFI z", (4, 14), 18),
        ("İ argument 1 İ z", "5", "i̇ argument 1 i̇ z", (3, 13), 16),
    ],
)
async def test_vim_tilde_expanding_letters_keep_the_field_and_land_after_the_written_text(
    text: str, count: str, expected: str, field: tuple[int, int], cursor: int
) -> None:
    start = text.index("argument 1")
    draft = ChatDraft(text, fields=((start, start + 10),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=EditorMode.VIM)

        await pilot.press(count, "~")
        assert editor.text == expected
        assert editor.prompt_fields == (field,)
        assert editor.cursor_location == (0, cursor)

        await pilot.press("u")
        assert editor.chat_draft() == draft
        await pilot.press("ctrl+r")
        assert editor.text == expected
        assert editor.prompt_fields == (field,)
        result = await _commit(app, pilot)
        assert result.draft.text == expected
        assert result.draft.fields == (field,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "count", "expected", "cursor"),
    [
        ("ab \nargument 1", "2", "ab argument 1", 2),
        ("ab\n  argument 1", "2", "ab argument 1", 2),
        ("ab\n\targument 1", "2", "ab argument 1", 2),
        ("argument 1  \n  [Quote #1]  ", "2", "argument 1 [Quote #1]", 10),
        ("[Quote #1]  \n  argument 1  ", "2", "[Quote #1] argument 1", 10),
        ("ab\n\nargument 1", "3", "ab  argument 1", 2),
        ("ab\n  \nargument 1", "3", "ab  argument 1", 2),
        ("ab\n\t\nargument 1", "3", "ab  argument 1", 2),
        ("ab\n  argument 1  \n\t[Quote #1]  ", "3", "ab argument 1 [Quote #1]", 2),
        ("[Quote #1]\n  argument 1\n  cd  ", "3", "[Quote #1] argument 1 cd", 10),
        ("argument 1\n  [Quote #1]\n  cd  ", "3", "argument 1 [Quote #1] cd", 10),
        ("argument 1\n  \n\t  ", "3", "argument 1  ", 10),
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
async def test_vim_join_preserves_units_through_indentation_and_blank_lines(
    text: str, count: str, expected: str, cursor: int, newline: str
) -> None:
    text = text.replace("\n", newline)
    start = text.index("argument 1")
    draft = ChatDraft(text, fields=((start, start + 10),))
    expected_start = expected.index("argument 1")
    fields = ((expected_start, expected_start + 10),)
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=EditorMode.VIM)
        await pilot.press(count, "J")
        assert editor.text == expected
        assert editor.prompt_fields == fields
        assert editor.cursor_location == (0, cursor)
        await pilot.press("u")
        assert editor.chat_draft() == draft
        await pilot.press("ctrl+r")
        assert editor.text == expected
        assert editor.prompt_fields == fields
        result = await _commit(app, pilot)
        assert result.draft.text == expected
        assert result.draft.fields == fields


@pytest.mark.asyncio
async def test_vim_join_on_the_last_line_leaves_the_previous_edit_next_to_undo() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab\nargument 1", fields=((3, 13),)), mode=EditorMode.VIM)
        await pilot.press("r", "A", "j", "J", "u")
        assert editor.text == "ab\nargument 1"
        assert editor.prompt_fields == ((3, 13),)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
async def test_typing_and_pasting_at_a_field_edge_shift_it_in_every_typing_mode(mode: EditorMode) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab field-one cd", fields=((3, 12),)), mode=mode)
        if mode is EditorMode.VIM:
            await pilot.press("i")
        editor.move_cursor((0, 3))
        if mode is EditorMode.EMACS:
            await pilot.press("ctrl+space")
        await pilot.press("X")
        assert editor.text == "ab Xfield-one cd"
        assert editor.prompt_fields == ((4, 13),)
        editor.post_message(Paste("yz"))
        await wait_for(lambda: editor.text == "ab Xyzfield-one cd", pilot=pilot, description="inline paste")
        assert editor.prompt_fields == ((6, 15),)
        await pilot.press("tab")
        assert editor.selected_text == "field-one"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
@pytest.mark.parametrize("paste", [False, True], ids=["type", "paste"])
async def test_selected_field_replacement_and_replay_match_in_every_typing_mode(mode: EditorMode, paste: bool) -> None:
    draft = ChatDraft("ab field-one cd", fields=((3, 12),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=mode)
        if mode is EditorMode.VIM:
            await pilot.press("i")
        await pilot.press("tab")
        assert editor.selected_text == "field-one"
        if paste:
            editor.post_message(Paste("X"))
            await wait_for(lambda: editor.text == "ab X cd", pilot=pilot, description="selected paste")
        else:
            await pilot.press("X")
        assert editor.text == "ab X cd"
        assert editor.prompt_fields == ()
        await pilot.press("ctrl+/" if mode is EditorMode.EMACS else "ctrl+z")
        assert editor.chat_draft() == draft
        editor.redo()
        assert editor.text == "ab X cd"
        assert editor.prompt_fields == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
async def test_single_character_delete_and_cut_take_the_whole_field_in_every_typing_mode(mode: EditorMode) -> None:
    draft = ChatDraft("ab field-one cd", fields=((3, 12),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=mode)
        if mode is EditorMode.VIM:
            await pilot.press("i")
        editor.move_cursor((0, 12))
        await pilot.press("backspace")
        assert editor.text == "ab  cd"
        assert editor.prompt_fields == ()
        editor.undo()
        assert editor.chat_draft() == draft
        await pilot.press("tab", "ctrl+x")
        assert editor.text == "ab  cd"
        assert editor.prompt_fields == ()
        assert app.clipboard == "field-one"
        editor.undo()
        assert editor.chat_draft() == draft


@pytest.mark.asyncio
async def test_vim_replace_pending_replaces_a_field_whole_and_undo_restores_it() -> None:
    draft = ChatDraft("ab field-one cd", fields=((3, 12),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, cursor=(0, 3), mode=EditorMode.VIM)
        await pilot.press("r", "X")
        assert editor.text == "ab X cd"
        assert editor.prompt_fields == ()
        await pilot.press("u")
        assert editor.chat_draft() == draft
