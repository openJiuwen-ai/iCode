# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The full-text editor pastes sendable text in every accepted input mode."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste

from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.widgets import text_area as text_area_module
from chrys.app.tui.widgets.chrome import draft_text_area
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
from chrys.app.tui.widgets.editor import EditorMode
from chrys.foundation.text.draft_references import EMPTY_REFERENCES
from chrys.foundation.text.mentions import format_file_mention
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.tui_helpers import png_bytes
from tests.support.waiting import wait_for


class _App(TuiVariableDefaultsMixin, App[None]):
    def __init__(self, mode: EditorMode, directory: Path) -> None:
        super().__init__()
        self.mode = mode
        self.directory = directory

    def compose(self) -> ComposeResult:
        yield DraftEditor(
            ChatDraft("left right"), mode=self.mode, paste_cwd=str(self.directory), clipboard_image_dir=self.directory
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["standard", "emacs", "vim-insert", "vim-normal", "vim-visual"])
@pytest.mark.parametrize("payload_kind", ["short", "long", "path", "bitmap"])
@pytest.mark.parametrize("route", ["terminal", "super+v", "ctrl+shift+v", "shift+insert"])
async def test_editor_paste_matrix_keeps_sendable_text_and_no_references(
    state: str, payload_kind: str, route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "a picture.png"
    plant_owner_only_bytes(image, png_bytes((10, 20, 30)))
    payload = {"short": "one\ntwo", "long": "long " * 180 + "\nlast", "path": str(image), "bitmap": ""}[payload_kind]
    saved: list[Path | None] = []

    def capture(directory: Path | None) -> Path | None:
        saved.append(directory)
        return image if payload_kind == "bitmap" else None

    monkeypatch.setattr(draft_text_area, "save_clipboard_image_to_file", capture)
    monkeypatch.setattr(text_area_module, "paste_text_from_clipboards", lambda _app: payload)
    mode = {"standard": EditorMode.STANDARD, "emacs": EditorMode.EMACS}.get(state, EditorMode.VIM)
    app = _App(mode, tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        editor = app.query_one(DraftEditor)
        editor.focus()
        await wait_for(lambda: editor.has_focus, pilot=pilot, description="editor focused")
        editor.move_cursor((0, 4))
        if state == "vim-insert":
            await pilot.press("i")
        elif state == "vim-visual":
            await pilot.press("v")
        before = editor.text
        if route == "terminal":
            await editor._on_paste(Paste(payload))
            await pilot.pause()
        else:
            await pilot.press(route)

        if state in {"vim-normal", "vim-visual"}:
            assert editor.text == before
            assert saved == []
        else:
            inserted = f" {format_file_mention(str(image))}" if payload_kind in {"path", "bitmap"} else payload
            assert editor.text == f"left{inserted} right"
            if payload_kind == "bitmap":
                assert saved == [tmp_path]
        assert editor.chat_draft().references == EMPTY_REFERENCES
        assert editor.editor_unit_spans() == []


@pytest.mark.asyncio
async def test_editor_paste_still_applies_the_token_cap_without_folding(tmp_path: Path) -> None:
    app = _App(EditorMode.STANDARD, tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        editor = app.query_one(DraftEditor)
        editor._max_paste_tokens = 30_000
        editor.move_cursor((0, 4))
        await editor._on_paste(Paste("long " * 30_000))
        await pilot.pause()
        assert len(editor.text) == 120_010
        assert editor.text.startswith("leftlong ")
        assert editor.chat_draft().references == EMPTY_REFERENCES


@pytest.mark.asyncio
async def test_editor_label_shaped_text_is_plain_and_can_be_partly_deleted(tmp_path: Path) -> None:
    app = _App(EditorMode.STANDARD, tmp_path)
    async with app.run_test():
        editor = app.query_one(DraftEditor)
        await editor._on_paste(Paste("[Image #1]"))
        editor.move_cursor((0, 4))
        assert editor.cursor_location == (0, 4)
        assert editor.editor_unit_spans() == []
        assert editor.chat_draft().references == EMPTY_REFERENCES
        # No reference styling is registered for an ordinary label-shaped substring.
        reference_style = editor.get_component_rich_style("draft--reference", partial=False)
        assert all(span.style != reference_style for span in editor.get_line(0).spans)
        editor.delete((0, 1), (0, 2))
        assert editor.text.startswith("[mage #1]")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "cursor", "expected"),
    [
        ("", 0, "{mention}"),
        ("right", 0, "{mention} right"),
        ("left", 4, "left {mention}"),
        ("leftright", 4, "left {mention} right"),
        ("left right", 5, "left {mention} right"),
    ],
    ids=["empty", "start", "end", "tight-middle", "space-before"],
)
@pytest.mark.parametrize("bitmap", [False, True], ids=["path", "bitmap"])
async def test_editor_image_mentions_add_only_needed_separating_spaces(
    text: str, cursor: int, expected: str, bitmap: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "picture.png"
    plant_owner_only_bytes(image, png_bytes((10, 20, 30)))
    monkeypatch.setattr(draft_text_area, "save_clipboard_image_to_file", lambda _directory: image if bitmap else None)
    app = _App(EditorMode.STANDARD, tmp_path)
    async with app.run_test():
        editor = app.query_one(DraftEditor)
        editor.load_text(text)
        editor.move_cursor((0, cursor))
        await editor._on_paste(Paste("" if bitmap else str(image)))
        assert editor.text == expected.format(mention=format_file_mention(str(image)))
        assert editor.chat_draft().references == EMPTY_REFERENCES
