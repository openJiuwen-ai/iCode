# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the picker description layout shared by the profile pickers."""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from chrys.app.tui.util.picker_text import wrap_description

_CJK = "这是一个用于代码审查的智能体配置，支持多语言项目分析与结构化报告输出，并给出可执行的修改建议。"  # noqa: RUF001 - full-width punctuation is the text under test

_FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"

_ROCKET = "\U0001f680"

_ACCENTED = "e\u0301"

_FLAGS = ("\U0001f1e8\U0001f1f3", "\U0001f1fa\U0001f1f8", "\U0001f1ef\U0001f1f5")

_FITS = "Answers questions about the repository"


def _lines(wrapped: str) -> list[str]:
    return wrapped.split("\n")


def test_wrap_description_measures_double_width_text_in_cells() -> None:
    lines = _lines(wrap_description(_CJK, 60))

    assert all(cell_len(line) <= 60 for line in lines)
    assert lines[0].startswith("  - ")
    assert all(line.startswith("    ") for line in lines[1:])
    assert "".join(line[4:] for line in lines) == _CJK


@pytest.mark.parametrize(
    ("width", "description", "longest"),
    [
        (4, "中文描述", 6),
        (4, "abcd", 5),
        (10, _ROCKET * 6, 10),
    ],
)
def test_wrap_description_never_widens_a_narrow_budget(width: int, description: str, longest: int) -> None:
    lines = _lines(wrap_description(description, width))

    assert all(line for line in lines)
    assert max(cell_len(line) for line in lines) <= longest


def test_wrap_description_keeps_wide_graphemes_whole() -> None:
    wrapped = wrap_description(_FAMILY * 3, 6)

    assert _lines(wrapped) == [f"  - {_FAMILY}", f"    {_FAMILY}", f"    {_FAMILY}"]


def test_wrap_description_keeps_flag_pairs_together() -> None:
    lines = _lines(wrap_description("".join(_FLAGS), 7))

    assert lines == [f"  - {_FLAGS[0]}", *(f"    {flag}" for flag in _FLAGS[1:])]
    assert all(cell_len(line) <= 7 for line in lines)


def test_wrap_description_keeps_combining_marks_with_their_base() -> None:
    lines = _lines(wrap_description(_ACCENTED * 8, 8))

    assert lines == ["  - " + _ACCENTED * 4, "    " + _ACCENTED * 4]
    assert all(not line[4:].startswith("\u0301") for line in lines)


def test_wrap_description_leaves_text_that_fits_unchanged() -> None:
    assert wrap_description(_FITS, 60) == f"  - {_FITS}"
    assert wrap_description("hello world", 60) == "  - hello world"


def test_wrap_description_returns_empty_for_blank_text() -> None:
    assert wrap_description("", 60) == ""
    assert wrap_description("   \t\n ", 60) == ""


def test_wrap_description_keeps_every_word() -> None:
    description = "Runs read-only exploration across the repository and reports findings"
    lines = _lines(wrap_description(description, 24))

    assert all(cell_len(line) <= 24 for line in lines)
    assert " ".join(line[4:] for line in lines).split() == description.split()
