# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for SessionJsonPanel: border-click clipboard copy and post-remove render safety."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from textual.app import App, ComposeResult
from textual.color import Color

from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.theme_loader import load_user_themes
from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
from tests.support.tui_helpers import click_widget
from tests.support.waiting import wait_for


class SessionJsonPanelApp(App):
    def compose(self) -> ComposeResult:
        yield SessionJsonPanel()


async def test_session_json_border_click_copies_to_terminal_and_os_clipboards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with SessionJsonPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(SessionJsonPanel)
        session_path = "/tmp/chrys/sessions/12345678/session.json"
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        panel._session_path = session_path
        click_widget(panel)

        assert pilot.app.clipboard == session_path
        await wait_for_clipboard(pilot.app)
        assert copied == [session_path]


async def test_session_json_render_line_after_remove_returns_blank() -> None:
    async with SessionJsonPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(SessionJsonPanel)

        await panel.remove()
        await pilot.pause()

        rendered = panel.render_line(0)

    assert rendered.cell_length == 0


@pytest.mark.parametrize("secondary", ["#FF0000", "ansi_red", "hsl(20, 90%, 50%)", "rgba(255, 0, 0, 0.5)"])
async def test_session_json_loads_and_rehighlights_with_user_theme_colors(tmp_path: Path, secondary: str) -> None:
    (tmp_path / "custom.yaml").write_text(f'primary: "#875FAF"\nsecondary: "{secondary}"\n', encoding="utf-8")
    session_file = tmp_path / "session.json"
    session_file.write_text('{"sample": "readable JSON"}', encoding="utf-8")
    themes, warnings = load_user_themes(tmp_path)
    assert warnings == []
    app = SessionJsonPanelApp()
    app.register_theme(themes[0])
    app.theme = "custom"

    with patch.object(SessionJsonPanel, "_resolve_session_path", autospec=True, return_value=session_file):
        async with app.run_test() as pilot:
            panel = app.query_one(SessionJsonPanel)
            panel.display = True
            panel.load_session("sample")
            await app.workers.wait_for_complete()

            assert panel._status == ""
            assert "readable JSON" in "\n".join(panel._plain_lines)
            console = Console(force_terminal=False, _environ={})
            assert panel._text_lines[0].get_style_at_offset(console, 1).color == Color.parse(secondary).rich_color

            # A real theme switch must also repaint cached JSON through the
            # rehighlight worker, without requiring the file to be re-opened.
            app.theme = "textual-dark"
            await wait_for(lambda: panel._last_theme_key == (True, "#004578"), pilot=pilot)
            assert panel._text_lines[0].get_style_at_offset(console, 1).color == Color.parse("#004578").rich_color
            app.theme = "custom"
            await wait_for(lambda: panel._last_theme_key == (True, secondary), pilot=pilot)
            assert panel._text_lines[0].get_style_at_offset(console, 1).color == Color.parse(secondary).rich_color
