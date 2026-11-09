# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The editor host applies a command's ranges through the ordinary write path as one undo step."""

from __future__ import annotations

import pytest
from textual.app import App

from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
from chrys.app.tui.widgets.editor import EditorKeyStroke, EditorMode, EditorTextRange, MessageEditor
from chrys.app.tui.widgets.editor import widget as editor_widget


def test_host_replaces_original_ranges_backwards_and_undoes_the_group_once() -> None:
    editor = MessageEditor(text="abc def")
    editor.replace_editor_range(EditorTextRange((0, 7), (0, 7)), "!")

    editor.replace_editor_ranges(
        [
            (EditorTextRange((0, 0), (0, 1)), "ALPHA"),
            (EditorTextRange((0, 4), (0, 7)), "D"),
        ]
    )

    assert editor.text == "ALPHAbc D!"
    editor.action_undo()
    assert editor.text == "abc def!"
    editor.action_redo()
    assert editor.text == "ALPHAbc D!"
    editor.action_undo()
    editor.action_undo()
    assert editor.text == "abc def"


@pytest.mark.parametrize("with_recorded_edit", [False, True])
def test_unrecorded_ranges_do_not_collect_an_earlier_undo_step(with_recorded_edit: bool) -> None:
    editor = MessageEditor(text="abc")
    editor.replace_editor_range(EditorTextRange((0, 3), (0, 3)), "!")
    replacements = [(EditorTextRange((0, 0), (0, 0)), "")]
    if with_recorded_edit:
        replacements.append((EditorTextRange((0, 1), (0, 2)), "B"))
    replacements.append((EditorTextRange((0, 3), (0, 3)), ""))

    editor.replace_editor_ranges(replacements)

    assert editor.text == ("aBc!" if with_recorded_edit else "abc!")
    if with_recorded_edit:
        editor.action_undo()
        assert editor.text == "abc!"
    editor.action_undo()
    assert editor.text == "abc"


@pytest.mark.parametrize("with_field", [False, True])
def test_toggle_cursor_on_a_crlf_line_stops_on_its_last_whole_character(with_field: bool) -> None:
    text = "ßargument 1\r\nlast" if with_field else "ß\r\nlast"
    fields = ((1, 11),) if with_field else ()
    editor = DraftEditor(ChatDraft(text, fields=fields), paste_cwd=".", clipboard_image_dir=None, mode=EditorMode.VIM)
    for key in "2~":
        editor.dispatch_keymap_stroke(EditorKeyStroke(key, key))

    assert editor.text == text.replace("ß", "SS")
    assert editor.cursor_location == ((0, 2) if with_field else (0, 1))
    assert editor.prompt_fields == (((2, 12),) if with_field else ())
    editor.dispatch_keymap_stroke(EditorKeyStroke("u", "u"))
    assert editor.chat_draft() == ChatDraft(text, fields=fields)
    editor.dispatch_keymap_stroke(EditorKeyStroke("ctrl+r"))
    assert editor.text == text.replace("ß", "SS")
    assert editor.prompt_fields == (((2, 12),) if with_field else ())


async def test_each_range_uses_the_remaining_character_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editor_widget, "MESSAGE_EDITOR_MAX_CHARACTERS", 20)
    editor = MessageEditor(text="abcdefghijklmnopqr")

    async with App().run_test():
        editor.replace_editor_ranges(
            [
                (EditorTextRange((0, 0), (0, 1)), "1111"),
                (EditorTextRange((0, 17), (0, 18)), "2222"),
            ]
        )

    assert editor.text == "1bcdefghijklmnopq222"
    editor.action_undo()
    assert editor.text == "abcdefghijklmnopqr"
    editor.action_redo()
    assert editor.text == "1bcdefghijklmnopq222"
