# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The update hint on the welcome screen: where it sits, how it looks, and how long it stays."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.color import Color

from chrys.app.install_flavor import InstallFlavor
from chrys.app.tui import clipboard as tui_clipboard
from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.util.logo import CHAT_LOGO
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.welcome import WelcomeWidget, copy_on_click
from chrys.app.update_check import UpdateCheck
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import WidgetApp, click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from rich.color import Color as RichColor
    from textual.app import App

_COMMAND = "uv tool upgrade iCode-TUI"
_FOUND = "New version found: v0.30.0"
_RUN = f"Run: {_COMMAND} to upgrade"
_NOTICE = f"{_FOUND}\n{_RUN}"


def _notice() -> Text:
    text = Text(_NOTICE)
    for value, style in (("v0.30.0", Style(bold=True)), (_COMMAND, Style(bold=True) + copy_on_click(_COMMAND))):
        start = _NOTICE.index(value)
        text.stylize(style, start, start + len(value))
    return text


def _rows(app: App, welcome: WelcomeWidget) -> list[list[Segment]]:
    return list(Segment.split_lines(app.console.render(welcome.render())))


def _plain(row: list[Segment]) -> str:
    return "".join(segment.text for segment in row).strip()


def _styles(row: list[Segment]) -> list[Style]:
    """The style of every visible piece of ``row``."""
    return [segment.style or Style.null() for segment in row if segment.text.strip()]


def _warning(app: App) -> RichColor | None:
    return Color.parse(app.theme_variables["warning"]).rich_color


async def test_the_notice_sits_one_blank_line_below_the_working_directory() -> None:
    welcome = WelcomeWidget(CHAT_LOGO, title="Code", cwd="/workspace", notice=_notice())

    async with WidgetApp(lambda: welcome).run_test(size=(80, 30)) as pilot:
        rows = _rows(pilot.app, welcome)
        plain = [_plain(row) for row in rows]
        cwd = plain.index("/workspace")

        assert plain[cwd + 1] == ""
        assert plain[cwd + 2 : cwd + 4] == [_FOUND, _RUN]
        # Each line is centered on its own.
        for row, line in ((rows[cwd + 2], _FOUND), (rows[cwd + 3], _RUN)):
            text = "".join(segment.text for segment in row)
            assert text.index(line) == (80 - len(line)) // 2
        # The rows above keep their own look.
        assert all(style.bold for row in rows[: cwd - 2] for style in _styles(row))
        assert all(style.bold for style in _styles(rows[cwd - 1]))
        assert all(style.dim for style in _styles(rows[cwd]))
        warning = _warning(pilot.app)
        styles = {
            segment.text: segment.style for row in rows[cwd + 2 : cwd + 4] for segment in row if segment.text.strip()
        }
        assert styles.keys() == {"New version found: ", "v0.30.0", "Run: ", _COMMAND, " to upgrade"}
        assert all(style is not None and style.color == warning for style in styles.values())
        assert [text for text, style in styles.items() if style is not None and style.bold] == ["v0.30.0", _COMMAND]
        # Only the command is a link, and a link keeps the notice color where Textual styles it.
        assert [text for text, style in styles.items() if style is not None and style.underline] == [_COMMAND]
        assert welcome.link_style.color == warning
        assert welcome.link_style.bold and welcome.link_style.underline


async def test_a_narrow_welcome_wraps_the_notice_instead_of_cutting_the_command() -> None:
    welcome = WelcomeWidget(CHAT_LOGO, title="Code", cwd="/workspace", notice=_notice())
    welcome.styles.height = "auto"

    async with WidgetApp(lambda: welcome).run_test(size=(30, 30)) as pilot:
        await wait_for(lambda: welcome.size.width == 30, pilot=pilot, description="welcome laid out")
        rows = _rows(pilot.app, welcome)
        plain = [_plain(row) for row in rows]
        first = plain.index("/workspace") + 2

        assert len(plain) - first > 2
        assert " ".join(plain[first:]) == f"{_FOUND} {_RUN}"
        # Every wrapped line keeps the notice color, also one without a bold part.
        assert all(style.color == _warning(pilot.app) for row in rows[first:] for style in _styles(row))
        # The height fits the wrapped notice: no blank rows pad it at the top or bottom.
        assert plain[0] and plain[-1]


async def test_a_short_welcome_gives_up_the_logo_before_the_text_below_it() -> None:
    welcome = WelcomeWidget(CHAT_LOGO, title="Code", cwd="/workspace", notice=_notice())

    async with WidgetApp(lambda: welcome).run_test(size=(40, 8)) as pilot:
        await wait_for(lambda: welcome.size.height == 8, pilot=pilot, description="welcome laid out")
        plain = [_plain(row) for row in _rows(pilot.app, welcome)]
        shown = [row for row in plain if row]

        assert shown[:2] == ["Code", "/workspace"]
        assert " ".join(shown[2:]) == f"{_FOUND} {_RUN}"


async def test_removing_the_notice_leaves_the_welcome_as_it_was() -> None:
    welcome = WelcomeWidget(CHAT_LOGO, title="Code", cwd="/workspace")

    async with WidgetApp(lambda: welcome).run_test(size=(80, 30)) as pilot:
        before = [_plain(row) for row in _rows(pilot.app, welcome)]
        welcome.set_notice(_notice())
        assert _RUN in [_plain(row) for row in _rows(pilot.app, welcome)]
        welcome.set_notice(None)

        assert [_plain(row) for row in _rows(pilot.app, welcome)] == before


async def test_clicking_the_command_copies_it_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    host_copies: list[str] = []
    monkeypatch.setattr(tui_clipboard, "clipboard_copy", host_copies.append)
    welcome = WelcomeWidget(CHAT_LOGO, title="Code", cwd="/workspace", notice=_notice())
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(welcome, "notify", lambda message, *, title, **_kwargs: notices.append((message, title)))

    async with WidgetApp(lambda: welcome).run_test(size=(80, 30)) as pilot:
        rows = ["".join(segment.text for segment in row) for row in _rows(pilot.app, welcome)]
        y = next(index for index, row in enumerate(rows) if _COMMAND in row)
        # Clicking the words around the command does nothing.
        await click_when_settled(pilot, welcome, offset=(rows[y].index("Run:"), y))
        assert notices == []

        await click_when_settled(pilot, welcome, offset=(rows[y].index(_COMMAND) + 3, y))

        assert pilot.app.clipboard == _COMMAND
        await wait_for_clipboard(pilot.app)
        assert host_copies == [_COMMAND]
        assert notices == [(_COMMAND, "Copied")]


def _shows(app: App, text: str) -> bool:
    """Whether the chat welcome shows ``text``, however the panel's width wraps it."""
    welcomes = list(app.screen.query_one(ChatPanel).query(WelcomeWidget))
    shown = "".join(_plain(row) for row in _rows(app, welcomes[0])) if welcomes else ""
    return "".join(text.split()) in "".join(shown.split())


async def test_the_chat_welcome_shows_what_the_check_found_after_clears_and_in_the_new_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    pypi = httpx.MockTransport(lambda _request: httpx.Response(200, json={"info": {"version": "0.30.0"}}))
    check = UpdateCheck(
        flavor=InstallFlavor.UV, cache_path=tmp_path / "update-check.json", current_version="0.29.1", transport=pypi
    )
    app = make_chrys_app(tmp_path / "state", update_check=check)

    async with app.run_test(size=(100, 40)) as pilot:
        await wait_for(lambda: _shows(app, _NOTICE), pilot=pilot, description="update hint on the welcome")

        await app.screen.query_one(ChatPanel).clear()
        await wait_for(lambda: _shows(app, _NOTICE), pilot=pilot, description="update hint on the new welcome")

        app.locale_controller.switch_locale("zh-Hans")
        await wait_for(
            lambda: _shows(app, f"发现新版本：v0.30.0\n运行：{_COMMAND} 指令升级"),  # noqa: RUF001
            pilot=pilot,
            description="update hint in Chinese",
        )
