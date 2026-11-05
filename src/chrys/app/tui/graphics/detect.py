# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Choose how the terminal can show images, from what it answers when asked at start (``probe.py``).

- Ghostty 1.0+, kitty 0.28+ and iTerm2 3.7.5+, named by their XTVERSION reply: the kitty graphics
  protocol with Unicode placeholders, where the app writes 24-bit color (over SSH, only where the host
  sets ``COLORTERM``). Before 3.7.5, and in nightlies built before that fix, iTerm2 draws nothing for a
  placeholder that, like these, gives a row and a column but no third diacritic.
- WezTerm, and VS Code's terminal (xterm.js) with images turned on, which then lists Sixel (``4``) in
  its device attributes: iTerm2 inline images, one per cell row, drawn over the half-blocks as Sixel
  is, in full color.
- Windows Terminal, which answers no XTVERSION but sets ``WT_SESSION`` (also inside WSL), and lists
  Sixel (``4``) in its device attributes from 1.22: Sixel. A terminal started from a Windows Terminal
  shell inherits ``WT_SESSION``; one that names itself in XTVERSION or ``TERM_PROGRAM`` is not taken
  for it.
- Anything else, and any terminal not asked: half-blocks. The terminal is asked only when the setting
  is on, Textual draws to a real terminal, colors are on, no multiplexer sits between (tmux, screen
  and zellij hide the terminal outside them, and placeholders would need passthrough), and ``TERM`` is
  unset (Windows) or an xterm one, as every terminal above sets it (WezTerm may set its own).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import TYPE_CHECKING, Final

from chrys.app.tui.graphics.probe import query_terminal
from chrys.app.tui.terminal.title import running_under_textual_web_driver

if TYPE_CHECKING:
    from collections.abc import Mapping

    from chrys.app.tui.graphics.probe import TerminalReport

_PLACEHOLDER_TERMINALS: Final = (
    ("ghostty ", (1, 0), None),
    ("kitty(", (0, 28), None),
    ("iTerm2 ", (3, 7, 5), (3, 7, 20260917)),
)
"""XTVERSION prefixes of terminals that draw kitty Unicode placeholders, with the oldest release that does
and, for nightlies, which number their third part by build date (``iTerm2 3.7.20261029-nightly``), the
oldest nightly."""
_BUILD_DATE: Final = 10_000_000
"""A third version part this large is a build date."""
_WEZTERM: Final = "WezTerm "
_XTERM_JS: Final = "xterm.js("
_SIXEL_ATTRIBUTE: Final = 4
_ASKED_TERM_PREFIXES: Final = ("xterm", "wezterm")
_MULTIPLEXER_VARIABLES: Final = ("TMUX", "STY", "ZELLIJ")
_LEADING_VERSION = re.compile(r"\d+(?:\.\d+)*")


class GraphicsProtocol(Enum):
    """How images are drawn."""

    BLOCKS = "blocks"
    """Half-block characters, two pixels per cell; works everywhere."""
    KITTY = "kitty"
    """Kitty graphics protocol with Unicode placeholders."""
    SIXEL = "sixel"
    """Sixel images drawn over the half-blocks after each frame."""
    ITERM = "iterm"
    """iTerm2 inline images (OSC 1337) drawn over the half-blocks after each frame."""


def ask_terminal(env: Mapping[str, str], *, enabled: bool) -> TerminalReport | None:
    """Ask the terminal what it is, where its answer could give images better than half-blocks."""
    if not enabled or running_under_textual_web_driver() or not _worth_asking(env):
        return None
    return query_terminal()


def _worth_asking(env: Mapping[str, str]) -> bool:
    if "NO_COLOR" in env or any(env.get(name) for name in _MULTIPLEXER_VARIABLES):
        return False
    term = env.get("TERM", "")
    return not term or term.startswith(_ASKED_TERM_PREFIXES)


def choose_protocol(report: TerminalReport | None, env: Mapping[str, str], *, truecolor: bool) -> GraphicsProtocol:
    """The protocol the terminal that gave ``report`` is known to draw correctly.

    ``truecolor`` is whether the app writes 24-bit colors: placeholder cells name their image by color.
    """
    if report is None:
        return GraphicsProtocol.BLOCKS
    for prefix, oldest, oldest_nightly in _PLACEHOLDER_TERMINALS:
        if report.version.startswith(prefix):
            version = _leading_version(report.version.removeprefix(prefix))
            if oldest_nightly is not None and version[2:3] >= (_BUILD_DATE,):
                oldest = oldest_nightly
            if truecolor and version >= oldest:
                return GraphicsProtocol.KITTY
            return GraphicsProtocol.BLOCKS
    if report.version.startswith(_WEZTERM) or (
        report.version.startswith(_XTERM_JS) and _SIXEL_ATTRIBUTE in report.attributes
    ):
        return GraphicsProtocol.ITERM
    if (
        not report.version
        and env.get("WT_SESSION")
        and not env.get("TERM_PROGRAM")
        and _SIXEL_ATTRIBUTE in report.attributes
    ):
        return GraphicsProtocol.SIXEL
    return GraphicsProtocol.BLOCKS


def _leading_version(text: str) -> tuple[int, ...]:
    """The numbers a version starts with: ``1.4.1-main+abc`` is (1, 4, 1); none is ()."""
    match = _LEADING_VERSION.match(text)
    return () if match is None else tuple(int(part) for part in match[0].split("."))
