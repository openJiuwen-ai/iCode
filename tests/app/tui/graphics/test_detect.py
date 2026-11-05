# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Choosing a graphics protocol from what the terminal answered, and when to ask it."""

from __future__ import annotations

import pytest

from chrys.app.tui.graphics import detect
from chrys.app.tui.graphics.detect import GraphicsProtocol, ask_terminal, choose_protocol
from chrys.app.tui.graphics.probe import TerminalReport

KITTY = GraphicsProtocol.KITTY
SIXEL = GraphicsProtocol.SIXEL
ITERM = GraphicsProtocol.ITERM
BLOCKS = GraphicsProtocol.BLOCKS
_SIXEL = frozenset({61, 4, 6, 22, 52})
_WT = {"WT_SESSION": "abc"}


@pytest.mark.parametrize(
    ("report", "env", "expected"),
    [
        pytest.param(TerminalReport("ghostty 1.4.0"), {}, KITTY, id="ghostty"),
        pytest.param(TerminalReport("ghostty 1.0.0"), {}, KITTY, id="ghostty-oldest"),
        pytest.param(TerminalReport("ghostty 0.9.9"), {}, BLOCKS, id="ghostty-too-old"),
        pytest.param(TerminalReport("ghostty 1.4.1-main+0123abc"), {}, KITTY, id="ghostty-dev-build"),
        pytest.param(TerminalReport("ghostty 1.4.0", _SIXEL), _WT, KITTY, id="ghostty-started-from-wt"),
        pytest.param(TerminalReport("kitty(0.49.2)"), {}, KITTY, id="kitty"),
        pytest.param(TerminalReport("kitty(0.28.0)"), {}, KITTY, id="kitty-oldest"),
        pytest.param(TerminalReport("kitty(0.27.1)"), {}, BLOCKS, id="kitty-too-old"),
        pytest.param(TerminalReport("iTerm2 3.7.6"), {}, KITTY, id="iterm2"),
        pytest.param(TerminalReport("iTerm2 3.7.5"), {}, KITTY, id="iterm2-oldest"),
        pytest.param(TerminalReport("iTerm2 3.7.4"), {}, BLOCKS, id="iterm2-too-old"),
        pytest.param(TerminalReport("iTerm2 3.6.11"), {}, BLOCKS, id="iterm2-3.6"),
        pytest.param(TerminalReport("iTerm2 unknown"), {}, BLOCKS, id="iterm2-unreadable-version"),
        pytest.param(TerminalReport("iTerm2 3.7.20261029-nightly"), {}, KITTY, id="iterm2-nightly"),
        pytest.param(TerminalReport("iTerm2 3.7.20260917-nightly"), {}, KITTY, id="iterm2-oldest-nightly"),
        pytest.param(TerminalReport("iTerm2 3.7.20260916-nightly"), {}, BLOCKS, id="iterm2-nightly-too-old"),
        pytest.param(TerminalReport("iTerm2 3.6.20250101-nightly"), {}, BLOCKS, id="iterm2-3.6-nightly"),
        pytest.param(TerminalReport("iTerm2 3.8.20270101-nightly"), {}, KITTY, id="iterm2-later-nightly"),
        pytest.param(TerminalReport("WezTerm 20240203-110809-5046fc22", _SIXEL), {}, ITERM, id="wezterm"),
        pytest.param(TerminalReport("WezTerm 20240203-110809-5046fc22"), _WT, ITERM, id="wezterm-started-from-wt"),
        pytest.param(TerminalReport("xterm.js(6.1.0-beta.304)", frozenset({62, 4, 9, 22})), {}, ITERM, id="vscode"),
        pytest.param(TerminalReport("xterm.js(6.1.0-beta.304)", frozenset({1, 2})), {}, BLOCKS, id="vscode-images-off"),
        pytest.param(TerminalReport("XTerm(390)", _SIXEL), _WT, BLOCKS, id="xterm-started-from-wt"),
        pytest.param(TerminalReport("tmux 3.8", frozenset({1, 2, 4})), _WT, BLOCKS, id="tmux-in-wt"),
        pytest.param(TerminalReport("", _SIXEL), _WT, SIXEL, id="windows-terminal"),
        pytest.param(TerminalReport("", frozenset({61, 6, 22})), _WT, BLOCKS, id="windows-terminal-1.21"),
        pytest.param(TerminalReport("", _SIXEL), {}, BLOCKS, id="unnamed-sixel-terminal"),
        pytest.param(
            TerminalReport("", frozenset({61, 4})), {**_WT, "TERM_PROGRAM": "vscode"}, BLOCKS, id="vscode-from-wt"
        ),
        pytest.param(TerminalReport("", frozenset({1, 2})), {}, BLOCKS, id="apple-terminal"),
        pytest.param(None, _WT, BLOCKS, id="not-asked"),
    ],
)
def test_protocol_follows_the_terminal_that_answered(
    report: TerminalReport | None, env: dict[str, str], expected: GraphicsProtocol
) -> None:
    assert choose_protocol(report, env, truecolor=True) is expected


def test_placeholders_need_truecolor_to_name_their_image() -> None:
    assert choose_protocol(TerminalReport("ghostty 1.4.0"), {}, truecolor=False) is BLOCKS
    assert choose_protocol(TerminalReport("", _SIXEL), _WT, truecolor=False) is SIXEL
    assert choose_protocol(TerminalReport("WezTerm 20240203-110809-5046fc22"), {}, truecolor=False) is ITERM


@pytest.fixture
def queries(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Each time the terminal would have been asked; the answer is Ghostty's."""
    asked: list[None] = []

    def query_terminal() -> TerminalReport:
        asked.append(None)
        return TerminalReport("ghostty 1.4.0")

    monkeypatch.setattr(detect, "query_terminal", query_terminal)
    monkeypatch.setenv("TEXTUAL_DRIVER", "")
    monkeypatch.delenv("TEXTUAL_DRIVER")
    return asked


@pytest.mark.parametrize(
    ("env", "asked"),
    [
        pytest.param({"TERM": "xterm-ghostty"}, True, id="ghostty"),
        pytest.param({"TERM": "xterm-256color"}, True, id="generic-xterm"),
        pytest.param({"TERM": "wezterm"}, True, id="wezterm"),
        pytest.param({}, True, id="windows"),
        pytest.param({"TERM": "xterm-256color", "TMUX": ""}, True, id="empty-multiplexer-variable"),
        pytest.param({"TERM": "xterm-ghostty", "NO_COLOR": ""}, False, id="no-color"),
        pytest.param({"TERM": "xterm-256color", "TMUX": "/tmp/tmux-501/default,1,0"}, False, id="tmux"),
        pytest.param({"TERM": "xterm-256color", "STY": "1.pts-0.host"}, False, id="screen"),
        pytest.param({"TERM": "xterm-256color", "ZELLIJ": "0"}, False, id="zellij"),
        pytest.param({"TERM": "tmux-256color"}, False, id="tmux-term"),
        pytest.param({"TERM": "screen-256color"}, False, id="screen-term"),
        pytest.param({"TERM": "alacritty"}, False, id="alacritty"),
        pytest.param({"TERM": "linux"}, False, id="linux-console"),
        pytest.param({"TERM": "dumb"}, False, id="dumb"),
    ],
)
def test_terminals_that_may_draw_images_are_asked(queries: list[None], env: dict[str, str], asked: bool) -> None:
    report = ask_terminal(env, enabled=True)

    assert queries == ([None] if asked else [])
    assert report == (TerminalReport("ghostty 1.4.0") if asked else None)


def test_nothing_is_asked_with_the_setting_off(queries: list[None]) -> None:
    assert ask_terminal({"TERM": "xterm-ghostty"}, enabled=False) is None
    assert queries == []


def test_nothing_is_asked_under_the_web_driver(queries: list[None], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")

    assert ask_terminal({"TERM": "xterm-ghostty"}, enabled=True) is None
    assert queries == []
