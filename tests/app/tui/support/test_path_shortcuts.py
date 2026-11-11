# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the compact path labels shown in the recent-directory list.

``PATH_DISPLAY_MAX`` is a *cell* budget: the label sits in one column of the
file picker, and a CJK directory name spends two cells per character.  The
label also has to stay on one separator, since ``…/a\\b`` reads as a single
level of a path that has two.
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from chrys.app.tui.support.path_shortcuts import PATH_DISPLAY_MAX, format_recent_path_label

WINDOWS_LONG = r"C:\Users\Administrator\Documents\这是一个很长的中文项目目录名"
POSIX_LONG = "/home/user/项目资料/2026年度归档目录"
ASCII_LONG = "/home/user/a-very-long-english-directory-name-here"
WINDOWS_SHORT_PARENTS = r"C:\Users\Administrator\Documents"
POSIX_SHORT = "/home/user/short"
WINDOWS_TINY = r"D:\a\b\短"
WINDOWS_NAME_TOO_WIDE_ONLY = r"C:\w\这是一个很长的中文项目目录名"


@pytest.mark.parametrize(
    "path",
    [
        WINDOWS_LONG,
        POSIX_LONG,
        ASCII_LONG,
        WINDOWS_SHORT_PARENTS,
        POSIX_SHORT,
        WINDOWS_TINY,
        WINDOWS_NAME_TOO_WIDE_ONLY,
    ],
)
def test_a_recent_path_label_stays_within_its_cell_budget(path: str) -> None:
    label = format_recent_path_label(path)

    assert cell_len(label) <= PATH_DISPLAY_MAX, f"{label!r} is {cell_len(label)} cells, budget {PATH_DISPLAY_MAX}"


@pytest.mark.parametrize(
    "path",
    [WINDOWS_LONG, POSIX_LONG, WINDOWS_SHORT_PARENTS, WINDOWS_NAME_TOO_WIDE_ONLY],
)
def test_a_recent_path_label_never_mixes_separators(path: str) -> None:
    label = format_recent_path_label(path)

    assert not ("/" in label and "\\" in label), f"{label!r} mixes / and \\"


def test_a_windows_path_label_uses_the_separator_of_the_path() -> None:
    label = format_recent_path_label(WINDOWS_LONG)

    assert "…" in label
    assert not label.startswith("…/")


def test_a_path_that_already_fits_is_left_alone() -> None:
    assert format_recent_path_label(POSIX_SHORT) == POSIX_SHORT
    assert format_recent_path_label(WINDOWS_TINY) == WINDOWS_TINY


def test_a_narrow_budget_still_names_the_directory() -> None:
    """The tail of the last folder is what tells two sibling directories apart."""
    label = format_recent_path_label(WINDOWS_NAME_TOO_WIDE_ONLY)

    assert label.startswith("…")
    assert label.endswith("这是一个很长的中文项目目录名"[-4:])


def test_every_label_wraps_the_path_it_names() -> None:
    """A label is a shortening of its path, never a different path."""
    for path in (WINDOWS_LONG, POSIX_LONG, ASCII_LONG, WINDOWS_SHORT_PARENTS):
        label = format_recent_path_label(path)
        assert path.endswith(label.split("…")[-1].lstrip("/\\")), f"{label!r} does not tail {path!r}"
