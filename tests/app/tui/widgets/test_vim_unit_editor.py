# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real draft-editor coverage for Vim whole-unit commands and history."""

from __future__ import annotations

import pytest
from textual.widgets.text_area import Selection

from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
from chrys.app.tui.widgets.editor import EditorMode
from tests.app.tui.widgets.test_draft_editor import _App, _open


@pytest.fixture(autouse=True)
def _no_branch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


@pytest.mark.parametrize(
    ("command", "expected_text", "expected_fields"),
    [
        ("2x", "ab cd argument 2 ef", ((6, 16),)),
        ("d2w", "ab argument 2 ef", ((3, 13),)),
        ("d2aw", "ab argument 2 ef", ((3, 13),)),
        ("2rZ", "ab ZZcd argument 2 ef", ((8, 18),)),
    ],
)
@pytest.mark.asyncio
async def test_vim_counted_unit_edits_have_one_undo_redo_step(
    command: str, expected_text: str, expected_fields: tuple[tuple[int, int], ...]
) -> None:
    draft = ChatDraft("ab argument 1 cd argument 2 ef", fields=((3, 13), (17, 27)))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, cursor=(0, 3), mode=EditorMode.VIM)
        await pilot.press(*command)
        changed = editor.chat_draft()
        assert (changed.text, changed.fields) == (expected_text, expected_fields)
        await pilot.press("u")
        assert editor.chat_draft() == draft
        await pilot.press("ctrl+r")
        assert editor.chat_draft() == changed


@pytest.mark.parametrize(
    ("put", "expected", "cursor"), [("p", "field valueZZ rest", (0, 12)), ("P", "ZZfield value rest", (0, 1))]
)
@pytest.mark.asyncio
async def test_vim_character_put_uses_each_side_of_unit(put: str, expected: str, cursor: tuple[int, int]) -> None:
    draft = ChatDraft("Z field value rest", fields=((2, 13),))
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, mode=EditorMode.VIM)
        # A yank stores plain text; a put beside a field keeps its identity.
        await pilot.press("y", "l", "l", "l")
        assert editor.selection == Selection.cursor((0, 2))
        await pilot.press("2", put)
        assert editor.text == "Z " + expected
        assert editor.selection == Selection.cursor((cursor[0], cursor[1] + 2))
        assert editor.prompt_fields == (((2, 13),) if put == "p" else ((4, 15),))


@pytest.mark.parametrize("visual", (False, True))
@pytest.mark.parametrize("fields", (False, True))
@pytest.mark.asyncio
async def test_vim_sticky_column_survives_short_line_and_unit_landing(visual: bool, fields: bool) -> None:
    text = "abcdefghi\nx\nab field value zz\nabcdefghi"
    field_start = text.index("field value")
    draft = ChatDraft(text, fields=((field_start, field_start + 11),) if fields else ())
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, draft, cursor=(0, 5), mode=EditorMode.VIM)
        if visual:
            await pilot.press("v")
        await pilot.press("j", "j")
        if visual:
            assert editor.selection.end == ((2, 14) if fields else (2, 6))
        else:
            assert editor.selection == Selection.cursor((2, 3) if fields else (2, 5))
        await pilot.press("j")
        assert editor.selection.end == (3, 6 if visual else 5)


@pytest.mark.parametrize("visual", (False, True))
@pytest.mark.parametrize("reset", ("l", "q", "click"))
@pytest.mark.asyncio
async def test_vim_sticky_column_resets_after_other_key_or_external_cursor_move(visual: bool, reset: str) -> None:
    app = _App()
    async with app.run_test() as pilot:
        editor = await _open(app, pilot, ChatDraft("abcdefghi\nxyz\nabcdefghi"), cursor=(0, 6), mode=EditorMode.VIM)
        if visual:
            await pilot.press("v")
        await pilot.press("j")
        if reset == "click":
            editor.move_cursor((1, 1))
        else:
            await pilot.press(reset)
        await pilot.press("j")
        # Visual clicks leave its logical active end in place; only Normal
        # resumes movement from the clicked column.
        expected_column = 1 if reset == "click" and not visual else 2
        assert editor.selection.end == (2, expected_column + int(visual))


@pytest.mark.parametrize("visual", (False, True))
@pytest.mark.parametrize("key", ("ctrl+d", "ctrl+u"))
@pytest.mark.asyncio
async def test_vim_half_page_keeps_sticky_column(visual: bool, key: str) -> None:
    app = _App()
    async with app.run_test(size=(100, 24)) as pilot:
        rows = ["abcdefghi"] * 120
        editor = await _open(app, pilot, ChatDraft("\n".join(rows)), cursor=(60, 6), mode=EditorMode.VIM)
        distance = max(editor.editor_page_height // 2, 1)
        middle = 60 + (distance if key == "ctrl+d" else -distance)
        rows[middle] = "x"
        editor.load_text("\n".join(rows))
        editor.move_cursor((60, 6))
        if visual:
            await pilot.press("v")
        await pilot.press(key, key)
        target_row = 60 + (2 * distance if key == "ctrl+d" else -2 * distance)
        assert editor.selection.end == (target_row, 6 + int(visual and key == "ctrl+d"))
