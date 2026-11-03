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
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
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


class _App(TuiVariableDefaultsMixin, App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[EditorDialogResult] = []
        self.opened: list[DraftReference] = []


def _quote(text: str = "said") -> DraftReferences:
    references, _entry = EMPTY_REFERENCES.with_new("quote", text)
    return references


def _live(editor: MessageEditor, references: DraftReferences | None = None) -> list[str]:
    table = references if references is not None else editor.chat_draft().references  # type: ignore[attr-defined]
    return [editor.text[label.start : label.end] for label in iter_reference_labels(editor.text, table)]


async def _open(
    app: _App, pilot, draft: ChatDraft, *, cursor: tuple[int, int] = (0, 0), mode: EditorMode = EditorMode.STANDARD
) -> DraftEditor:
    dialog = EditorDialog(
        EditorBufferSnapshot(draft.text, cursor),
        mode=mode,
        draft=draft,
        open_reference=app.opened.append,
    )
    app.push_screen(dialog, app.results.append)
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
        assert app.opened == [DraftReference("quote", 1, "it failed")]


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
