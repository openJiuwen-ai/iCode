# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Stop sandbox commands when the workspace filesystem is nearly full.

This is not a project quota. The kernel does not cap how many bytes a bind
mount of an existing directory may grow. The runtime watches free space on
each writable bind and stops commands once a filesystem drops below the floor.
A single write can still cross the floor between checks.
"""

from __future__ import annotations

import os
import sys

# Leave this much free on the workspace filesystem. Below it, commands stop.
DISK_FREE_FLOOR_BYTES = 1024 * 1024 * 1024


def free_bytes(path: str) -> int | None:
    """Available bytes on the filesystem that holds ``path``, or None."""
    if sys.platform == "win32":
        return None
    try:
        stat = os.statvfs(path)
    except OSError:
        return None
    return stat.f_bavail * stat.f_frsize


def any_below_floor(paths: list[str], floor: int = DISK_FREE_FLOOR_BYTES) -> bool:
    """Whether any path's filesystem has fewer than ``floor`` bytes free."""
    for path in paths:
        free = free_bytes(path)
        if free is not None and free < floor:
            return True
    return False
