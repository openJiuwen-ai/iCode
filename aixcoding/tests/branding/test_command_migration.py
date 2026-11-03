# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Direct behavior tests for the legacy-alias migration helper."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from chrys.kernel.aixcoding.command_migration import LEGACY_APP_COMMAND, remove_legacy_alias


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_remove_legacy_alias_uses_readlink_alone_for_a_dangling_symlink(tmp_path: Path) -> None:
    # The binary is gone, so neither exists() nor samefile() could prove ownership;
    # only the readlink match can. Guards the symlink branch of is_alias_of.
    binary = tmp_path / "chrys"
    legacy = tmp_path / LEGACY_APP_COMMAND
    legacy.symlink_to("chrys")
    assert not legacy.exists()
    warnings: list[str] = []

    removed = remove_legacy_alias(binary=binary, warn=warnings.append)

    assert removed is True
    assert not legacy.is_symlink()
    assert warnings == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_remove_legacy_alias_keeps_a_dangling_symlink_to_something_else(tmp_path: Path) -> None:
    binary = tmp_path / "chrys"
    legacy = tmp_path / LEGACY_APP_COMMAND
    legacy.symlink_to("removed")
    assert not legacy.exists()
    warnings: list[str] = []

    removed = remove_legacy_alias(binary=binary, warn=warnings.append)

    assert removed is False
    assert legacy.is_symlink()
    assert len(warnings) == 1 and "cannot prove it belongs" in warnings[0]
