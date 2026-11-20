# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
from pathlib import Path

from browser_use.downloads import (
    NAME_LIMIT,
    display_url,
    file_size,
    remove_quiet,
    reserve_path,
    sanitize_filename,
)


def test_sanitize_strips_directories_and_traversal() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Windows\\System32\\evil.exe") == "evil.exe"
    assert sanitize_filename("/absolute/name.txt") == "name.txt"
    assert sanitize_filename("..\\..\\boom") == "boom"


def test_sanitize_replaces_invalid_and_control_characters() -> None:
    assert sanitize_filename('a<b>c:d"e|f?g*h.txt') == "a_b_c_d_e_f_g_h.txt"
    assert sanitize_filename("line\nbreak\x00.txt") == "line_break_.txt"


def test_sanitize_handles_reserved_names_and_edges() -> None:
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("lpt1.txt") == "_lpt1.txt"
    assert sanitize_filename("CON.tar.gz") == "_CON.tar.gz"
    assert sanitize_filename("NUL.txt.bak") == "_NUL.txt.bak"
    assert sanitize_filename("   ") == "download"
    assert sanitize_filename("trailing. ") == "trailing"
    assert sanitize_filename("") == "download"


def test_sanitize_truncates_but_keeps_a_safe_suffix() -> None:
    name = sanitize_filename("x" * 300 + ".txt")
    assert len(name) <= NAME_LIMIT
    assert name.endswith(".txt")
    assert not name.endswith((" ", "."))


def test_reserve_path_never_overwrites_and_stays_inside(tmp_path: Path) -> None:
    first = reserve_path(tmp_path / "downloads", "file.txt")
    second = reserve_path(tmp_path / "downloads", "file.txt")
    third = reserve_path(tmp_path / "downloads", "file.txt")
    assert first.name == "file.txt"
    assert second.name == "file-2.txt"
    assert third.name == "file-3.txt"
    assert {first.parent, second.parent, third.parent} == {(tmp_path / "downloads").resolve()}
    assert all(path.exists() for path in (first, second, third))


def test_file_size_and_remove_quiet_tolerate_missing_files(tmp_path: Path) -> None:
    target = tmp_path / "x.bin"
    assert file_size(target) is None
    remove_quiet(target)
    remove_quiet(None)
    target.write_bytes(b"abc")
    assert file_size(target) == 3
    remove_quiet(target)
    assert not target.exists()


def test_display_url_drops_query_and_fragment() -> None:
    assert display_url("https://example.com/path/file.bin?token=secret#frag") == "https://example.com/path/file.bin"


def test_reserved_file_is_owner_only_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    directory = tmp_path / "downloads"
    path = reserve_path(directory, "file.txt")
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    reserve_path(shared, "file.txt")
    assert shared.stat().st_mode & 0o777 == 0o755, "an existing directory must keep its mode"
