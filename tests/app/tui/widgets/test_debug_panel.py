# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for DebugPanel and its clickable log: localized title, event logging, and click-to-copy."""

from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.sidebar.debug import DebugPanel, _ClickableLog
from chrys.foundation.config.settings import Settings
from tests.support.tui_helpers import (
    LocalizedApp,
    click_widget,
)


class DebugPanelApp(LocalizedApp):
    def __init__(self, locale: str = "en") -> None:
        self.locale_controller = LocaleController(Settings(locale=locale))
        super().__init__()

    def compose(self) -> ComposeResult:
        yield DebugPanel()


@pytest.mark.parametrize(("locale", "title"), [("en", "Event Stream"), ("zh-Hans", "事件流")])
async def test_debug_panel_title_uses_mount_locale(locale: str, title: str) -> None:
    async with DebugPanelApp(locale).run_test() as pilot:
        panel = pilot.app.query_one(DebugPanel)
        assert panel.query_one("DebugPanel > Static", Static).render().plain == title


async def test_debug_panel_log_event() -> None:
    async with DebugPanelApp().run_test() as pilot:
        dp = pilot.app.query_one(DebugPanel)
        dp.log_event("ToolCallStart", "read_file")
        dp.log_event("Usage[Explore]", "4,291")
        dp.log_event("Error", "something broke")
        dp.log_raw("raw debug text")
        log = pilot.app.query_one("#debug-log", _ClickableLog)
        assert any("Usage[Explore]" in line for line in log._plain_lines)
        # No crash = passes


async def test_debug_log_click_copies_to_terminal_and_os_clipboards(monkeypatch: pytest.MonkeyPatch) -> None:
    async with DebugPanelApp().run_test() as pilot:
        log = pilot.app.query_one("#debug-log", _ClickableLog)
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        log.write_with_text(Text("visible event"), "plain event")
        click_widget(log)

        assert pilot.app.clipboard == "plain event"
        await wait_for_clipboard(pilot.app)
        assert copied == ["plain event"]
