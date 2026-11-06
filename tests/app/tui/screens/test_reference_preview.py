# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The preview a clicked label opens: a keyboard-closed viewer that reads image paths as the engine does."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.reference_preview import ReferencePreviewDialog
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from chrys.foundation.text.draft_references import DraftReference
from tests.support.tui_helpers import png_bytes, rich_plain
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
@pytest.mark.asyncio
async def test_the_preview_has_no_buttons_and_closes_by_key(tmp_path: Path, key: str) -> None:
    app = App()
    async with app.run_test() as pilot:
        dialog = ReferencePreviewDialog(DraftReference("paste", 1, "line one\nline two"), cwd=tmp_path)
        await app.push_screen(dialog)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="preview shown")
        assert not dialog.query(Button)

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
        assert rich_plain(body.render()) == "first line\nsecond line"
