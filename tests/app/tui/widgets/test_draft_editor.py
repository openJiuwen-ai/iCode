# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The message editor edits a chat draft by the input box's rules: labels, prompt fields, pastes, a whole round trip."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste

from chrys.app.tui.screens.dialogs.editor import EditorDialog, EditorDialogResult
from chrys.app.tui.screens.dialogs.reference_preview import ReferencePreviewDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.draft_text_area import DraftTextArea
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar
from chrys.app.tui.widgets.chrome.prompt_draft import ShortcutIntent
from chrys.app.tui.widgets.editor import EditorBufferSnapshot, EditorMode, MessageEditor
from chrys.foundation.events.bus import EventBus
from chrys.foundation.text.draft_references import (
    EMPTY_REFERENCES,
    DraftReference,
    DraftReferences,
    iter_reference_labels,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

_FOUR_LINES = "def f():\n    return 1\n\nprint(f())"


@pytest.fixture(autouse=True)
def _no_branch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


class _App(TuiVariableDefaultsMixin, App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[EditorDialogResult] = []
        self.opened: list[tuple[DraftReference, DraftTextArea]] = []


def _quote(text: str = "said") -> DraftReferences:
    references, _entry = EMPTY_REFERENCES.with_new("quote", text)
    return references


def _live(editor: MessageEditor, references: DraftReferences | None = None) -> list[str]:
    table = references if references is not None else editor.chat_draft().references  # type: ignore[attr-defined]
    return [editor.text[label.start : label.end] for label in iter_reference_labels(editor.text, table)]


async def _open(
    app: _App, pilot, draft: ChatDraft, *, cursor: tuple[int, int] = (0, 0), mode: EditorMode = EditorMode.STANDARD
) -> DraftEditor:
    def record_result(result: EditorDialogResult | None) -> None:
        if result is not None:
            app.results.append(result)

    def open_reference(reference: DraftReference, area: DraftTextArea) -> None:
        app.opened.append((reference, area))

    dialog = EditorDialog(
        EditorBufferSnapshot(draft.text, cursor),
        mode=mode,
        draft=draft,
        open_reference=open_reference,
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
    draft = ChatDraft(
        "fix [Quote #1] X now", fields=((15, 16),), owner=ShortcutIntent("fix", "", ""), references=_quote()
    )
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft)
        assert _live(editor) == ["[Quote #1]"]
        assert editor.prompt_fields == ((15, 16),)

        result = await _commit(app, pilot)

        assert result.accepted
        assert result.draft == draft


@pytest.mark.asyncio
async def test_backspace_after_a_label_removes_all_of_it() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("see [Quote #1]", references=_quote()), cursor=(0, 14))

        await pilot.press("backspace")

        assert editor.text == "see "


@pytest.mark.asyncio
async def test_vim_x_on_a_label_deletes_it_whole_and_its_register_puts_it_back_live() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab [Quote #1] cd", references=_quote()), mode=EditorMode.VIM)
        editor.move_cursor((0, 3))

        await pilot.press("x")
        assert editor.text == "ab  cd"

        await pilot.press("P")
        assert editor.text == "ab [Quote #1] cd"
        assert _live(editor) == ["[Quote #1]"]


@pytest.mark.asyncio
async def test_vim_tilde_toggles_text_and_steps_over_a_label_unchanged() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab [Quote #1] cd", references=_quote()), mode=EditorMode.VIM)
        editor.move_cursor((0, 0))

        # a, b, the space, the label as one, the space, c.
        for _ in range(6):
            await pilot.press("~")

        assert editor.text == "AB [Quote #1] Cd"
        assert _live(editor) == ["[Quote #1]"]


@pytest.mark.asyncio
async def test_vim_r_on_a_label_replaces_the_whole_label() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab [Quote #1] cd", references=_quote()), mode=EditorMode.VIM)
        editor.move_cursor((0, 3))

        await pilot.press("r", "z")

        assert editor.text == "ab z cd"


@pytest.mark.asyncio
async def test_a_long_paste_in_the_editor_folds_into_a_label_that_comes_back_live() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        editor = await _open(app, pilot, ChatDraft("x "), cursor=(0, 2))

        await editor._on_paste(Paste(_FOUR_LINES))

        assert editor.text == "x [Pasted text #1 +3 lines]"
        result = await _commit(app, pilot)
        assert result.draft.sent_text() == f"x {_FOUR_LINES}"


@pytest.mark.parametrize(("mode", "jumps"), [(EditorMode.STANDARD, True), (EditorMode.VIM, False)])
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
async def test_clicking_a_label_in_the_editor_opens_what_it_stands_for() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("[Quote #1] tail", references=_quote("it failed")))
        content = editor.content_region

        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 3, content.y - editor.region.y))

        await wait_for(lambda: bool(app.opened), pilot=pilot, description="label click reported")
        assert app.opened == [(DraftReference("quote", 1, "it failed"), editor)]


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

        await editor._on_paste(Paste(_FOUR_LINES))

        assert editor.text == f"[Quote #1] {_FOUR_LINES}"


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
        assert _live(editor) == ["[Quote #1]"]

        # A label clicked in the editor opens over it, as from the input box.
        content = editor.content_region
        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 10, content.y - editor.region.y))
        await wait_for(lambda: isinstance(app.screen, ReferencePreviewDialog), pilot=pilot, description="preview")
        await pilot.press("escape")
        await wait_for(lambda: isinstance(app.screen, EditorDialog), pilot=pilot, description="back in the editor")

        editor.move_cursor((0, len(editor.text)))
        await pilot.press("n", "o", "w")
        await pilot.press("ctrl+o")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot, description="editor committed")

        assert input_bar.value == "explain [Quote #1] now"
        assert input_bar.draft().sent_text() == "explain “it failed” now"


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [("dollar_sign", "dollar_sign", "dollar_sign"), ("l", "l", "l", "l", "l", "l")])
async def test_vim_normal_mode_rests_on_a_label_that_ends_the_line(keys: tuple[str, ...]) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab [Quote #1]", references=_quote()), mode=EditorMode.VIM)
        editor.move_cursor((0, 0))

        seen: list[tuple[int, int]] = []
        for key in keys:
            await pilot.press(key)
            seen.append(editor.cursor_location)

        # The label is one character to Normal mode: the cursor rests on its start, never past the line.
        assert seen[-1] == (0, 3)
        assert seen[-2] == (0, 3)


@pytest.mark.asyncio
async def test_vim_visual_on_a_label_that_ends_the_line_shows_the_label_selected() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab [Quote #1]", references=_quote()), mode=EditorMode.VIM)
        editor.move_cursor((0, 0))
        await pilot.press("dollar_sign", "v")

        # The highlight is what a delete takes: the whole label.
        assert editor.selected_text == "[Quote #1]"
        await pilot.press("d")
        assert editor.text == "ab "


@pytest.mark.asyncio
@pytest.mark.parametrize("append", ["a", "A"])
async def test_vim_append_on_a_label_that_ends_the_line_types_after_it(append: str) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab [Quote #1]", references=_quote()), mode=EditorMode.VIM)
        editor.move_cursor((0, 0))
        await pilot.press("dollar_sign")
        assert editor.cursor_location == (0, 3)

        await pilot.press(append, "x")

        assert editor.text == "ab [Quote #1]x"
        assert _live(editor) == ["[Quote #1]"]


@pytest.mark.asyncio
async def test_vim_p_after_a_label_puts_after_it_and_the_next_edit_hits_what_was_put() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("ab [Quote #1] cd", references=_quote()), mode=EditorMode.VIM)
        editor.move_cursor((0, 0))
        await pilot.press("y", "l")  # yank "a"
        editor.move_cursor((0, 3))

        await pilot.press("p")
        assert editor.text == "ab [Quote #1]a cd"
        assert editor.cursor_location == (0, 13)

        await pilot.press("x")
        assert editor.text == "ab [Quote #1] cd"
        assert _live(editor) == ["[Quote #1]"]


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
        ((0, 3), "w", (0, 12)),
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
        await pilot.press("P")
        assert editor.text == "ab field-one cd"
        assert editor.prompt_fields == ()


@pytest.mark.asyncio
async def test_vim_visual_yank_of_part_of_a_field_puts_the_whole_text() -> None:
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
@pytest.mark.parametrize(("motion", "expected"), [("w", "ab  cd"), ("dollar_sign", "ab ")])
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
@pytest.mark.parametrize("keys", [("dollar_sign", "dollar_sign"), ("l", "l", "l", "l")])
async def test_vim_line_end_with_interleaved_label_and_field_rests_outside_both(keys: tuple[str, ...]) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(
            app,
            pilot,
            ChatDraft("ab[Quote #1]xyz", fields=((9, 15),), references=_quote()),
            mode=EditorMode.VIM,
        )

        for key in keys:
            await pilot.press(key)
            assert all(not (2 < column < 12 or 9 < column < 15) for _row, column in editor.selection)

        assert editor.selection.start == editor.selection.end == (0, 2)
        assert editor.text == "ab[Quote #1]xyz"
        assert editor.prompt_fields == ((9, 15),)


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
async def test_vim_undo_of_tilde_on_a_label_keeps_the_earlier_edit() -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("a [Quote #1]", references=_quote()), mode=EditorMode.VIM)

        await pilot.press("r", "B", "w", "~", "u")

        assert editor.text == "B [Quote #1]"
        assert editor.prompt_fields == ()
        assert _live(editor) == ["[Quote #1]"]


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
async def test_a_paste_edited_inside_the_editor_has_one_undo_and_commits_the_changed_table() -> None:
    old = "one\ntwo\nthree\nfour"
    new = "new\ntext\nwith\nfive\nlines"
    references, _reference = EMPTY_REFERENCES.with_new("paste", old)
    app = _MainApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        input_bar.set_draft(ChatDraft("see [Pasted text #1 +3 lines]", references=references))
        await pilot.press("X")
        original = input_bar.draft()

        await pilot.press("ctrl+o")
        await wait_for(lambda: isinstance(app.screen, EditorDialog), pilot=pilot, description="draft editor open")
        dialog = app.screen
        editor = dialog.query_one(DraftEditor)
        content = editor.content_region
        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 7, content.y - editor.region.y))
        await wait_for(
            lambda: isinstance(app.screen, ReferencePreviewDialog), pilot=pilot, description="paste editor open"
        )
        paste_editor = app.screen.query_one(MessageEditor)
        await wait_for(lambda: paste_editor.has_focus, pilot=pilot, description="paste editor focused")
        paste_editor.select_all()
        paste_editor.post_message(Paste(new))
        await wait_for(lambda: paste_editor.text == new, pilot=pilot, description="paste changed")
        await pilot.press("escape")
        await wait_for(lambda: app.screen is dialog, pilot=pilot, description="back in draft editor")

        assert editor.text == "see [Pasted text #1 +4 lines]X"
        assert editor.chat_draft().sent_text() == f"see {new}X"
        assert input_bar.draft() == original
        await pilot.press("ctrl+z")
        assert editor.text == original.text
        assert editor.chat_draft().sent_text() == f"see {old}X"
        await pilot.press("ctrl+y")
        assert editor.chat_draft().sent_text() == f"see {new}X"

        await pilot.press("ctrl+o")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot, description="draft editor committed")
        assert input_bar.value == "see [Pasted text #1 +4 lines]X"
        assert input_bar.draft().sent_text() == f"see {new}X"
        await pilot.press("ctrl+z")
        assert input_bar.value == "see [Pasted text #1 +4 lines]X"
        assert input_bar.draft().sent_text() == f"see {new}X"


@pytest.mark.asyncio
async def test_a_paste_editor_uses_the_draft_editors_current_uncommitted_vim_mode() -> None:
    references, _reference = EMPTY_REFERENCES.with_new("paste", "one\ntwo\nthree\nfour")
    app = _MainApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        input_bar.set_draft(ChatDraft("see [Pasted text #1 +3 lines]", references=references))
        await pilot.press("ctrl+o")
        await wait_for(lambda: isinstance(app.screen, EditorDialog), pilot=pilot, description="draft editor open")
        dialog = app.screen
        editor = dialog.query_one(DraftEditor)
        await pilot.press("f3", "f3")
        assert editor.mode is EditorMode.VIM

        content = editor.content_region
        await click_when_settled(pilot, editor, offset=(content.x - editor.region.x + 7, content.y - editor.region.y))
        await wait_for(
            lambda: isinstance(app.screen, ReferencePreviewDialog), pilot=pilot, description="paste editor open"
        )
        preview = app.screen
        paste_editor = preview.query_one(MessageEditor)
        assert paste_editor.mode is EditorMode.VIM
        await pilot.press("escape")
        assert app.screen is preview
        assert paste_editor.mode is EditorMode.VIM

        await pilot.press("ctrl+o")
        await wait_for(lambda: app.screen is dialog, pilot=pilot, description="paste editor closed")
        assert editor.mode is EditorMode.VIM
