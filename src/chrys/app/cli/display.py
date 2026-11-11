# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How the CLI shows things: values from the user's files on one line, and the consoles that render them."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text

_MIN_TABLE_WIDTH = 40
"""Below this a tabular listing has no room left for a column, and rich prints nothing at all."""


def stdout_console() -> Console:
    """A stdout console for human-readable tables, with a usable width floor.

    ``rich`` reads its width from ``COLUMNS``; a degenerate value yields a
    non-positive width and the table renders as the empty string, so the command
    exits zero having printed nothing. The floor keeps the listing visible.
    """
    console = Console(file=sys.stdout, highlight=False)
    if console.width >= _MIN_TABLE_WIDTH:
        return console
    return Console(file=sys.stdout, highlight=False, width=_MIN_TABLE_WIDTH)


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
