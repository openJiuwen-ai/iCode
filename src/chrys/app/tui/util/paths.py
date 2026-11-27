# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cell-aware path shortening for compact UI rows."""

from __future__ import annotations

from rich.cells import cell_len


def tail_fit(shown: str, cells: int) -> str:
    """The last of *shown* that fits *cells*, as written, ``…`` standing for what is cut off before it."""
    if cell_len(shown) <= cells:
        return shown
    if cells <= 0:
        return ""
    start = len(shown)
    # Check the whole candidate suffix: Rich renders a base glyph plus its VS16 as one two-cell glyph, so summing
    # per code point under-counts emoji variants and lets the label overrun its budget.  Starting at ``start - 1``
    # keeps the base together with a trailing variation selector when measuring.
    while start and cell_len("…" + shown[start - 1 :]) <= cells:
        start -= 1
    return f"…{shown[start:]}"


def middle_cut_path(path: str, cells: int, *, prefer_parent: bool = False) -> str:
    """*path* in at most *cells* cells: its file name, and as many of its folders from either end as fit, ``…``
    standing for those cut from the middle (``src/chrys/…/tui/app.py``).

    Where the file name alone does not fit, the end of it shows after ``…``, its spaces kept: they tell names apart.
    Empty where not even ``…`` fits.  With *prefer_parent*, a spare folder goes to the one nearest the file first,
    so same-named directories under different parents stay distinguishable (``…/branch-a/shared``).
    """
    if cell_len(path) <= cells:
        return path
    separator = "\\" if "\\" in path and "/" not in path else "/"
    *folders, name = path.split(separator)

    def shown(start: int, end: int) -> str:
        return separator.join([*folders[:start], "…", *folders[len(folders) - end :], name])

    if not folders or cell_len(shown(0, 0)) > cells:
        # ``…`` still tells of the folders cut off: a bare file name would read as one in the working folder.
        return tail_fit(shown(0, 0) if folders else name, cells)
    start = end = 0
    if prefer_parent and cell_len(shown(0, 1)) <= cells:  # Reserve …/{parent}/{name} first.
        start, end = 0, 1
    while start + end < len(folders):
        # Folders join from either end in turn: the root it hangs from, and those nearest the file. Once one end's next
        # folder no longer fits, the other end's still may.
        turns = ((start + 1, end), (start, end + 1)) if start <= end else ((start, end + 1), (start + 1, end))
        grown = next((size for size in turns if cell_len(shown(*size)) <= cells), None)
        if grown is None:
            break
        start, end = grown
    return shown(start, end)
