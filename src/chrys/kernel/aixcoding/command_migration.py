# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Legacy ``icode`` alias migration for the AIxCoding command rename.

Single source of alias ownership proof shared by the upstream installer and
uninstaller hook points. Only the installer's own locations are inspected
(the alias sitting next to the installed ``chrys`` binary); nothing elsewhere
on PATH is scanned or removed.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

LEGACY_APP_COMMAND = "icode"


def is_alias_of(alias: Path, binary: Path) -> bool:
    """Whether ``alias`` is a product-named command alias made for ``binary``."""
    try:
        if alias.is_symlink():
            return Path(os.readlink(alias)) in (Path(binary.name), binary)
        return alias.is_file() and binary.is_file() and alias.samefile(binary)
    except OSError:
        return False


def remove_legacy_alias(*, binary: Path, warn: Callable[[str], None]) -> bool:
    """Remove the legacy ``icode`` alias next to ``binary`` when provably ours.

    Returns True when an alias was removed. Anything whose ownership cannot be
    proven (such as a Windows copy fallback, which is never the same file) and
    files still in use are kept with a warning instead of being force-deleted.
    """
    alias = binary.with_name(LEGACY_APP_COMMAND + binary.suffix)
    if not alias.is_symlink() and not alias.exists():
        return False
    if not is_alias_of(alias, binary):
        warn(f"Kept {alias}: cannot prove it belongs to this installation.")
        return False
    try:
        alias.unlink()
    except OSError as exc:
        warn(f"Could not remove the legacy '{LEGACY_APP_COMMAND}' command at {alias}: {exc}")
        return False
    return True
