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


def format_recent_path_label(path: str) -> str:
    """Return a compact display label for a recently used directory.

    ``PATH_DISPLAY_MAX`` is a budget in cells, not in characters: a CJK folder
    name is two cells a character, so counting characters lets a label of 26
    characters occupy 40 cells and push the column out of shape.  The separator
    is whatever the path itself uses, since ``…/a\\b`` would read as one level of
    a path that has two.
    """
    return middle_cut_path(path, PATH_DISPLAY_MAX)
