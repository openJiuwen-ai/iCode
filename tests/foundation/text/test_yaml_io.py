# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""String fidelity tests for YAML serialisation."""

from __future__ import annotations

import yaml

from chrys.foundation.text.yaml_io import dump_yaml


def _roundtrip(value: str) -> str:
    """Serialise then parse back, returning the reloaded string."""
    return yaml.safe_load(dump_yaml({"text": value}))["text"]


def test_multiline_keeps_block_style() -> None:
    """Ordinary multi-line strings still use literal block style for hand editing."""
    dumped = dump_yaml({"text": "line1\nline2"})
    assert "text: |" in dumped
    assert _roundtrip("line1\nline2") == "line1\nline2"


def test_form_feed_roundtrips() -> None:
    """A form feed inside multi-line text must not be rewritten as a newline."""
    value = "line1\nline2\x0cline3"
    assert _roundtrip(value) == value


def test_vertical_tab_roundtrips() -> None:
    """A vertical tab inside multi-line text must not be rewritten as a newline."""
    value = "line1\nline2\x0bline3"
    assert _roundtrip(value) == value


def test_file_group_record_separators_roundtrip() -> None:
    """The file/group/record separators must survive instead of becoming newlines."""
    value = "line1\nline2\x1cline3\x1dline4\x1eline5"
    assert _roundtrip(value) == value


def test_nel_roundtrips() -> None:
    """NEL (U+0085) inside multi-line text must not be folded away as whitespace."""
    value = "line1\nline2\x85line3"
    assert _roundtrip(value) == value


def test_nel_without_newline_roundtrips() -> None:
    """A lone NEL in a single-line string must survive too."""
    value = "line1\x85line2"
    assert _roundtrip(value) == value


def test_unicode_separators_roundtrip() -> None:
    """U+2028/U+2029 line separators inside multi-line text must be preserved."""
    value = "line1\nline2\u2028line3\u2029line4"
    assert _roundtrip(value) == value


def test_trailing_newline_is_preserved() -> None:
    """A final newline must round-trip without gaining an extra blank line."""
    assert _roundtrip("line1\nline2\n") == "line1\nline2\n"


def test_trailing_whitespace_is_stripped() -> None:
    """Trailing whitespace per line is still stripped per existing convention."""
    assert _roundtrip("line1  \nline2") == "line1\nline2"


def test_single_line_unchanged() -> None:
    """Single-line strings bypass block style and keep their content intact."""
    assert _roundtrip("plain text") == "plain text"
