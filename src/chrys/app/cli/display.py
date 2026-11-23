# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How the CLI shows things: values from the user's files on one line, and the consoles that render them."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text

_UNUSABLE_TABLE_WIDTH = 79
"""Smallest usable ``Console.width``; on legacy Windows this is ``COLUMNS=80`` minus one."""

_FALLBACK_TABLE_WIDTH = 120
"""Wide enough for all current columns, including a 35-character profile ID."""


def stdout_console() -> Console:
    """A stdout console for human-readable tables, with a usable width floor.

    ``rich`` reads its width from ``COLUMNS``; a degenerate value either makes
    the table render as the empty string or leaves too little room for every
    column, so the listing exits zero while silently hiding data. Below the
    usable width we substitute a width that fits all current columns. The
    assignment is unconditional: it also freezes a normal width at construction
    time, so Rich cannot re-probe the terminal just before printing.
    """
    console = Console(file=sys.stdout, highlight=False)
    console.width = _FALLBACK_TABLE_WIDTH if console.width < _UNUSABLE_TABLE_WIDTH else console.width
    return console


def one_line(text: str) -> str:
    """*text* as one display line: lone surrogates made safe, controls and Unicode line breaks replaced."""
    return sanitize_legacy_scalar(surrogate_safe_text(text)).replace("\u2028", "\ufffd").replace("\u2029", "\ufffd")


def counted(number: int, noun: str) -> str:
    """*number* and *noun*, plural unless *number* is 1: ``1 file``, ``2 files``."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def shown_path(path: str, workspace: Path) -> str:
    """``./`` and the path below the current directory, or the absolute path, on one line."""
    try:
        relative = Path(path).relative_to(workspace)
    except ValueError:
        shown = path
    else:
        shown = "." if not relative.parts else f".{os.sep}{relative}"
    return one_line(shown)
