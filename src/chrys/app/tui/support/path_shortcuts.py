# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared quick-access path helpers for workspace surfaces.

Owns the built-in favorite locations (Home, Desktop, …) and the compact
label formatting used for recently-used directory lists.  Shared by the
file picker UI, the workspace MRU store, and the recent-dirs provider so
write-side filtering and display-side filtering can never disagree.
"""

from __future__ import annotations

import os
from pathlib import Path

from chrys.app.tui.util.paths import middle_cut_path

PATH_DISPLAY_MAX = 28
"""Max visible cells for a recent directory path label."""


def get_default_favorites() -> list[tuple[str, str]]:
    """Return the built-in favorite entries as ``(label, path)`` tuples."""
    home = str(Path.home())
    home_name = Path.home().name or "Home"
    favorites: list[tuple[str, str]] = [(home_name, home)]
    for name in ("Desktop", "Documents", "Downloads"):
        p = Path.home() / name
        if p.is_dir():
            favorites.append((name, str(p)))
    return favorites


def get_default_favorite_paths() -> set[str]:
    """Return the default favorite paths, keyed for path comparison.

    Keys are ``normcase(normpath(path))`` so callers can test membership
    against paths normalized the same way regardless of case sensitivity.
    """
    return {os.path.normcase(os.path.normpath(path)) for _, path in get_default_favorites()}


def _without_trailing_separator(path: str) -> str:
    """*path* without the separator a directory may end in, keeping a bare root as written."""
    windows_style = (
        path.startswith("\\\\")  # UNC share root
        # A drive letter must be an ASCII letter before the colon: ``/:cache/demo`` is a valid POSIX path whose
        # colon sits at index 1, so a bare ``path[1] == ":"`` would mistake it for a Windows drive.
        or (len(path) >= 2 and path[1] == ":" and path[0].isascii() and path[0].isalpha())
        or ("\\" in path and "/" not in path)  # backslash-only relative or absolute path
    )
    # POSIX paths drop only '/': a trailing '\' is a valid character in a directory name there.  Deciding from the
    # string rather than os.name keeps Windows/UNC cases correct when the tests run on POSIX CI.
    trimmed = path.rstrip("/\\" if windows_style else "/")
    if not trimmed or trimmed.endswith(":"):
        return path
    return trimmed


def _single_line(path: str) -> str:
    """*path* with control characters replaced by spaces, so one label stays one line.

    ``cell_len`` scores a tab as zero cells while the renderer expands it to the
    next eight-column stop, so an unnormalized name overruns the cell budget and
    wraps; a line separator would split the row outright.
    """
    return "".join(" " if ch < " " or ch == "\x7f" or ch in "\u2028\u2029" else ch for ch in path)


def format_recent_path_label(path: str) -> str:
    """Return a compact display label for a recently used directory.

    ``PATH_DISPLAY_MAX`` is a budget in cells, not in characters: a CJK folder
    name is two cells a character, so counting characters lets a label of 26
    characters occupy 40 cells and push the column out of shape.  The separator
    is whatever the path itself uses, since ``…/a\\b`` would read as one level of
    a path that has two.

    A trailing separator in the path's own dialect is dropped first.  ``normpath`` keeps it on
    a network share's root, and left in place it makes the last component empty: the share
    name becomes a folder the budget cannot fit, so ``\\\\server\\<share>\\``
    truncated to ``\\\\server\\…\\`` and every share of that server read the same.

    Control characters become spaces before truncation, so the cell budget and the renderer
    agree: a tab scores zero cells here but the renderer expands it, wrapping the row.
    """
    return middle_cut_path(_without_trailing_separator(_single_line(path)), PATH_DISPLAY_MAX, prefer_parent=True)
