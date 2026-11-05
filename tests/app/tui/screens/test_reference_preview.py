# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A clicked label opens a read-only reference or a paste editor that saves on exit."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.containers import VerticalGroup
from textual.events import Paste
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.reference_preview import ReferencePreviewDialog
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from chrys.app.tui.widgets.editor import EditorMode, MessageEditor
from chrys.foundation.text.draft_references import DraftReference, ReferenceKind
from tests.support.tui_helpers import click_when_settled, png_bytes
from tests.support.waiting import wait_for


@pytest.mark.parametrize(
    ("mention", "relative_to_home"),
    [("shot.png", False), ("~/shot.png", True)],
    ids=["workspace-relative", "home-relative"],
)
@pytest.mark.asyncio
async def test_a_recalled_image_mention_previews_from_the_workspace_or_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mention: str, relative_to_home: bool
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home if relative_to_home else workspace).joinpath("shot.png").write_bytes(png_bytes((10, 20, 30)))

    app = App()
    async with app.run_test() as pilot:
        dialog = ReferencePreviewDialog(DraftReference("image", 1, mention), cwd=workspace)
        await app.push_screen(dialog)

        await wait_for(
            lambda: bool(dialog.query(TerminalImages)),
            pilot=pilot,
            description="the image was read and shown",
        )


@pytest.mark.parametrize("key", ["escape", "q"])
@pytest.mark.parametrize("kind", ["image", "quote"])
@pytest.mark.asyncio
async def test_the_preview_has_no_buttons_and_closes_by_key(tmp_path: Path, key: str, kind: ReferenceKind) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog = ReferencePreviewDialog(DraftReference(kind, 1, "line one\nline two"), cwd=tmp_path)
        await app.push_screen(dialog)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="preview shown")
        assert not dialog.query(Button)
        assert not dialog.query(MessageEditor)

        await pilot.press(key)

        await wait_for(lambda: app.screen is not dialog, pilot=pilot, description="preview closed by its key")


@pytest.mark.asyncio
async def test_a_quote_previews_as_its_plain_text(tmp_path: Path) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog = ReferencePreviewDialog(DraftReference("quote", 1, "first line\nsecond line"), cwd=tmp_path)
        await app.push_screen(dialog)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="preview shown")

        body = dialog.query_one("#reference-preview-text", Static)
        assert str(body.content) == "first line\nsecond line"


async def _open_paste(
    app: App[None], pilot, tmp_path: Path, *, mode: EditorMode = EditorMode.STANDARD, content: str = "draft"
) -> tuple[ReferencePreviewDialog, MessageEditor, list[str | None]]:
    results: list[str | None] = []
    dialog = ReferencePreviewDialog(DraftReference("paste", 1, content), cwd=tmp_path, mode=mode)
    app.push_screen(dialog, results.append)
    await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="paste editor shown")
    editor = dialog.query_one(MessageEditor)
    await wait_for(lambda: editor.has_focus, pilot=pilot, description="paste editor focused")
    return dialog, editor, results


@pytest.mark.parametrize("kind", ["image", "quote"])
@pytest.mark.asyncio
async def test_read_only_previews_accept_ordinary_keys_and_close_on_the_backdrop(
    tmp_path: Path, kind: ReferenceKind
) -> None:
    app = App()
    results: list[str | None] = []
    async with app.run_test(size=(120, 30)) as pilot:
        dialog = ReferencePreviewDialog(DraftReference(kind, 1, "original"), cwd=tmp_path)
        app.push_screen(dialog, results.append)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="preview shown")

        await pilot.press("a", "space")

        assert app.screen is dialog
        assert not dialog.query(MessageEditor)
        assert results == []
        await click_when_settled(pilot, dialog, offset=(0, 0))
        await wait_for(lambda: bool(results), pilot=pilot, description="read-only preview closed")
        assert results == [None]


@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
@pytest.mark.parametrize("exit_key", ["escape", "ctrl+o", "ctrl+enter", "f3", "backdrop"])
@pytest.mark.asyncio
async def test_paste_editor_modes_and_exit_keys_save_without_a_discard_path(
    tmp_path: Path, mode: EditorMode, exit_key: str
) -> None:
    app = App()
    async with app.run_test(size=(120, 30)) as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path, mode=mode)
        assert editor.text == "draft"
        assert editor.cursor_location == (0, 0)
        assert not dialog.query(Button)
        if mode is EditorMode.VIM:
            await pilot.press("i", "X", "escape")
        else:
            await pilot.press("X")
        assert editor.text == "Xdraft"

        if exit_key == "backdrop":
            await click_when_settled(pilot, dialog, offset=(0, 0))
        else:
            await pilot.press(exit_key)

        if exit_key == "f3" or (exit_key == "escape" and mode is EditorMode.VIM):
            assert app.screen is dialog
            assert results == []
            assert editor.mode is mode
            assert editor.text == "Xdraft"
        else:
            await wait_for(lambda: bool(results), pilot=pilot, description="edited paste saved")
            assert app.screen is not dialog
            assert results == ["Xdraft"]


@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS])
@pytest.mark.asyncio
async def test_unchanged_paste_closes_without_a_new_value(tmp_path: Path, mode: EditorMode) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path, mode=mode)
        await pilot.press("escape")
        await wait_for(lambda: bool(results), pilot=pilot, description="unchanged paste closed")
        assert results == [None]
        assert app.screen is not dialog
        assert editor.text == "draft"


@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
@pytest.mark.parametrize("key", ["escape", "ctrl+o"])
@pytest.mark.asyncio
async def test_paste_exit_keys_work_when_focus_is_on_the_dialog(tmp_path: Path, mode: EditorMode, key: str) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path, mode=mode)
        if mode is EditorMode.VIM:
            await pilot.press("i", "X", "escape")
        else:
            await pilot.press("X")
        dialog.set_focus(None)
        await wait_for(lambda: not editor.has_focus, pilot=pilot, description="focus moved to the dialog")

        await pilot.press(key)

        if key == "escape" and mode is EditorMode.VIM:
            assert app.screen is dialog
            assert results == []
            assert editor.text == "Xdraft"
        else:
            await wait_for(lambda: bool(results), pilot=pilot, description="paste saved from dialog focus")
            assert app.screen is not dialog
            assert results == ["Xdraft"]


@pytest.mark.parametrize(
    "prefix",
    [(), ("i",), ("v",), ("V",), (":",), ("d",), ("d", "i"), ("r",), ("g",), ("Z",), ("3",)],
    ids=[
        "normal",
        "insert",
        "visual-character",
        "visual-line",
        "command",
        "operator",
        "text-object",
        "replace",
        "g",
        "Z",
        "count",
    ],
)
@pytest.mark.asyncio
async def test_vim_escape_never_closes_a_paste_editor(tmp_path: Path, prefix: tuple[str, ...]) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path, mode=EditorMode.VIM)

        await pilot.press(*prefix, "escape")

        assert app.screen is dialog
        assert results == []
        assert editor.text == "draft"
        assert editor.selected_text == ""
        await pilot.press("i", "X", "escape", "ctrl+o")
        await wait_for(lambda: bool(results), pilot=pilot, description="paste saved after Vim Escape")
        assert results == ["Xdraft"]


@pytest.mark.parametrize(
    "keys",
    [("Z", "Z"), ("Z", "Q"), (":", "w", "q", "enter"), (":", "x", "enter"), (":", "q", "!", "enter")],
    ids=["ZZ", "ZQ", "wq", "x", "q-bang"],
)
@pytest.mark.asyncio
async def test_every_vim_exit_command_saves_the_edited_paste(tmp_path: Path, keys: tuple[str, ...]) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, _editor, results = await _open_paste(app, pilot, tmp_path, mode=EditorMode.VIM)
        await pilot.press("i", "X", "escape", *keys)
        await wait_for(lambda: bool(results), pilot=pilot, description="Vim command saved the paste")
        assert app.screen is not dialog
        assert results == ["Xdraft"]


@pytest.mark.asyncio
async def test_vim_q_closes_an_unchanged_paste(tmp_path: Path) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, _editor, results = await _open_paste(app, pilot, tmp_path, mode=EditorMode.VIM)
        await pilot.press(":", "q", "enter")
        await wait_for(lambda: bool(results), pilot=pilot, description="unchanged paste closed with q")
        assert app.screen is not dialog
        assert results == [None]


@pytest.mark.parametrize(
    ("command", "warning"),
    [("q", "No write since last change; use :q!"), ("w", "Not an editor command: w")],
)
@pytest.mark.asyncio
async def test_vim_command_warnings_are_visible_and_keep_the_paste_open(
    tmp_path: Path, command: str, warning: str
) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path, mode=EditorMode.VIM)
        assert not list(app._notifications)
        await pilot.press("i", "X", "escape", ":", *command, "enter")
        await wait_for(
            lambda: any(notification.message == warning for notification in app._notifications),
            pilot=pilot,
            description="Vim warning shown",
        )
        assert app.screen is dialog
        assert results == []
        assert editor.text == "Xdraft"
        assert [(note.message, note.severity) for note in app._notifications] == [(warning, "warning")]


@pytest.mark.asyncio
async def test_vim_normal_paste_is_refused_with_a_visible_warning(tmp_path: Path) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path, mode=EditorMode.VIM)
        editor.post_message(Paste("PASTED"))
        await wait_for(
            lambda: any(note.message == "Paste requires Insert mode" for note in app._notifications),
            pilot=pilot,
            description="paste rejection shown",
        )
        assert app.screen is dialog
        assert results == []
        assert editor.text == "draft"
        assert [(note.message, note.severity) for note in app._notifications] == [
            ("Paste requires Insert mode", "warning")
        ]


@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
@pytest.mark.asyncio
async def test_q_is_text_in_a_paste_editor(tmp_path: Path, mode: EditorMode) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path, mode=mode)
        if mode is EditorMode.VIM:
            await pilot.press("i")
        await pilot.press("q")
        assert app.screen is dialog
        assert results == []
        assert editor.text == "qdraft"
        await pilot.press("ctrl+o")
        await wait_for(lambda: bool(results), pilot=pilot, description="q saved as ordinary text")
        assert results == ["qdraft"]


@pytest.mark.asyncio
async def test_a_long_paste_stays_plain_text_in_the_paste_editor(tmp_path: Path) -> None:
    app = App()
    async with app.run_test(size=(120, 30)) as pilot:
        dialog, editor, results = await _open_paste(app, pilot, tmp_path)
        editor.select_all()
        pasted = "one\r\ntwo\rthree\nfour [Quote #1]\n" + "z" * 900
        expected = "one\ntwo\nthree\nfour [Quote #1]\n" + "z" * 900
        editor.post_message(Paste(pasted))
        await wait_for(lambda: editor.text == expected, pilot=pilot, description="long paste kept inline")
        assert editor.text == expected
        assert app.screen is dialog
        await pilot.press("escape")
        await wait_for(lambda: bool(results), pilot=pilot, description="plain pasted text saved")
        assert results == [expected]


@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
@pytest.mark.asyncio
async def test_paste_editor_keeps_its_opening_title_and_shows_one_exit_hint(tmp_path: Path, mode: EditorMode) -> None:
    app = App()
    async with app.run_test(size=(120, 30)) as pilot:
        dialog, editor, _results = await _open_paste(app, pilot, tmp_path, mode=mode, content="one\ntwo")
        container = dialog.query_one("#reference-preview-container", VerticalGroup)
        assert container.border_title == "[Pasted text #1 +1 lines]"
        assert container.border_subtitle == (
            "ZZ / Ctrl+O saves and closes" if mode is EditorMode.VIM else "Esc saves and closes"
        )
        if mode is EditorMode.VIM:
            await pilot.press("i")
        await pilot.press("enter", "X")
        assert editor.text == "\nXone\ntwo"
        assert container.border_title == "[Pasted text #1 +1 lines]"
        assert not dialog.query(Button)


@pytest.mark.parametrize("long_content", [False, True], ids=["short", "long"])
@pytest.mark.asyncio
async def test_paste_editor_fits_short_content_and_keeps_the_last_line_visible(
    tmp_path: Path, long_content: bool
) -> None:
    content = "\n".join(f"line {number}" for number in range(100)) if long_content else "one line"
    app = App()
    async with app.run_test(size=(110, 32)) as pilot:
        dialog, editor, _results = await _open_paste(app, pilot, tmp_path, content=content)
        container = dialog.query_one("#reference-preview-container", VerticalGroup)
        assert editor.cursor_location == (0, 0)
        assert editor.soft_wrap
        assert not editor.show_line_numbers
        if long_content:
            editor.move_cursor((99, 7))
            await wait_for(
                lambda: (
                    editor.cursor_location == (99, 7) and editor.content_region.contains(*editor.cursor_screen_offset)
                ),
                pilot=pilot,
                description="last line cursor is visible",
            )
            assert editor.cursor_location == (99, 7)
            assert 1 < editor.region.height <= 23
            assert container.region.height <= 25
            assert editor.vertical_scrollbar.region.width == 1
            assert editor.vertical_scrollbar.region.right == container.content_region.right
        else:
            assert editor.region.height == 3
            assert container.region.height == 5
