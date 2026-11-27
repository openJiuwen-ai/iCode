# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the compact path labels shown in the recent-directory list.

``PATH_DISPLAY_MAX`` is a *cell* budget: the label sits in one column of the
file picker, and a CJK directory name spends two cells per character.  The label
also has to stay on the separator the path was written with, since ``…/a\\b``
reads as a single level of a path that has two — which is why the assertions here
are positive: an input whose label ends up with no separator at all would satisfy
"never mixes them" without testing anything.

A directory path may arrive with a trailing separator (``normpath`` keeps it on a
network share's root), so the labels below cover both spellings.
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len
from rich.text import Text

from chrys.app.tui.support.path_shortcuts import PATH_DISPLAY_MAX, format_recent_path_label

WINDOWS_LONG = r"C:\Users\Administrator\Documents\这是一个很长的中文项目目录名"
POSIX_LONG = "/home/user/项目资料/2026年度归档目录"
ASCII_LONG = "/home/user/a-very-long-english-directory-name-here"
WINDOWS_SHORT_PARENTS = r"C:\Users\Administrator\Documents"
POSIX_SHORT_PARENTS = "/home/user/repos/openJiuwen-iCode"
POSIX_SHORT = "/home/user/short"
WINDOWS_TINY = r"D:\a\b\短"
WINDOWS_NAME_TOO_WIDE_ONLY = r"C:\w\这是一个很长的中文项目目录名"
# A share root keeps its trailing separator through ``ntpath.normpath``.
UNC_SHORT_SHARE = r"\\server\another-long-share"
UNC_LONG_SHARE = r"\\server\ssssssssssssssssssssssssss"
UNC_OTHER_LONG_SHARE = r"\\server\tttttttttttttttttttttttttt"


@pytest.mark.parametrize(
    "path",
    [
        WINDOWS_LONG,
        POSIX_LONG,
        ASCII_LONG,
        WINDOWS_SHORT_PARENTS,
        WINDOWS_SHORT_PARENTS + "\\",
        POSIX_SHORT,
        WINDOWS_TINY,
        WINDOWS_NAME_TOO_WIDE_ONLY,
        UNC_SHORT_SHARE,
        UNC_SHORT_SHARE + "\\",
        UNC_LONG_SHARE + "\\",
        "C:\\",
    ],
)
def test_a_recent_path_label_stays_within_its_cell_budget(path: str) -> None:
    label = format_recent_path_label(path)

    assert cell_len(label) <= PATH_DISPLAY_MAX, f"{label!r} is {cell_len(label)} cells, budget {PATH_DISPLAY_MAX}"


def test_a_windows_label_keeps_the_separator_it_was_written_with() -> None:
    assert format_recent_path_label(WINDOWS_SHORT_PARENTS) == "C:\\…\\Administrator\\Documents"


def test_a_posix_label_keeps_the_separator_it_was_written_with() -> None:
    assert format_recent_path_label(POSIX_SHORT_PARENTS) == "/…/repos/openJiuwen-iCode"


@pytest.mark.parametrize(
    "path",
    [WINDOWS_SHORT_PARENTS + "\\", WINDOWS_LONG, UNC_SHORT_SHARE + "\\", UNC_LONG_SHARE + "\\"],
)
def test_a_label_never_ends_with_a_separator(path: str) -> None:
    label = format_recent_path_label(path)

    assert not label.endswith(("\\", "/")), f"{label!r} ends with a separator"


def test_a_share_root_drops_the_separator_it_ends_with() -> None:
    assert format_recent_path_label(UNC_SHORT_SHARE + "\\") == UNC_SHORT_SHARE


def test_a_share_root_shortens_like_the_same_path_without_its_separator() -> None:
    assert format_recent_path_label(UNC_LONG_SHARE + "\\") == format_recent_path_label(UNC_LONG_SHARE)


def test_two_shares_of_one_server_stay_apart() -> None:
    """A trailing separator left in place empties the last component, so both shares read ``\\\\server\\…\\``."""
    first = format_recent_path_label(UNC_LONG_SHARE + "\\")
    second = format_recent_path_label(UNC_OTHER_LONG_SHARE + "\\")

    assert first != second
    assert "ssss" in first
    assert "tttt" in second


def test_a_bare_root_is_left_as_written() -> None:
    assert format_recent_path_label("C:\\") == "C:\\"
    assert format_recent_path_label("/") == "/"


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
    for path in (WINDOWS_LONG, POSIX_LONG, ASCII_LONG, WINDOWS_SHORT_PARENTS, UNC_LONG_SHARE + "\\"):
        label = format_recent_path_label(path)
        tail = label.split("…")[-1].lstrip("/\\")
        assert path.rstrip("/\\").endswith(tail), f"{label!r} does not tail {path!r}"


def test_same_named_dirs_are_told_apart_by_their_parent() -> None:
    first = format_recent_path_label("/tmp/work/branch-a/shared-project-01")
    second = format_recent_path_label("/tmp/work/branch-b/shared-project-01")
    assert first == "…/branch-a/shared-project-01"
    assert second == "…/branch-b/shared-project-01"
    assert first != second
    assert cell_len(first) <= PATH_DISPLAY_MAX and cell_len(second) <= PATH_DISPLAY_MAX


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("release-notes-and-assets-2026❤️", "…ase-notes-and-assets-2026❤️"),
        ("2026-release-notes-and-design-assets❤️☀️✈️", "…tes-and-design-assets❤️☀️✈️"),
    ],
)
def test_an_emoji_variant_label_never_exceeds_the_cell_budget(name: str, shown: str) -> None:
    label = format_recent_path_label("/tmp/" + name)
    assert label == shown
    assert cell_len(label) <= PATH_DISPLAY_MAX


def test_a_posix_trailing_backslash_is_part_of_the_directory_name() -> None:
    assert format_recent_path_label("/home/user/demo\\") == "/home/user/demo\\"
    assert format_recent_path_label("/home/user/demo") != format_recent_path_label("/home/user/demo\\")


def test_a_tab_in_a_directory_name_does_not_push_the_label_past_its_budget() -> None:
    """``cell_len`` scores a tab as zero cells while the renderer expands it, so it must be neutralized."""
    path = "/tmp/abc\t12345678901234567890"
    label = format_recent_path_label(path)

    assert not any(ch < " " or ch == "\x7f" for ch in label), f"{label!r} still holds a control character"
    assert cell_len(label) <= PATH_DISPLAY_MAX
    assert Text(label).cell_len <= PATH_DISPLAY_MAX


def test_a_line_break_in_a_directory_name_keeps_the_label_on_one_line() -> None:
    label = format_recent_path_label("/tmp/a\nb")

    assert "\n" not in label
    assert not any(ch < " " or ch == "\x7f" for ch in label), f"{label!r} still holds a control character"
    assert cell_len(label) <= PATH_DISPLAY_MAX


def test_a_posix_colon_in_the_second_position_is_not_a_drive_letter() -> None:
    assert format_recent_path_label("/:cache/demo\\") == "/:cache/demo\\"
    assert format_recent_path_label("/:cache/demo") != format_recent_path_label("/:cache/demo\\")


def test_a_drive_letter_still_drops_a_trailing_separator() -> None:
    assert format_recent_path_label("C:/x/") == "C:/x"
