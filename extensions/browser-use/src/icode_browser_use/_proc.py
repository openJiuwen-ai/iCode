# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess launch hygiene.

Every non-interactive child gets ``stdin=subprocess.DEVNULL`` at its call site
(iCode's stdin is an ACP JSON-RPC pipe). On Windows the child must get its own
hidden console: sharing the parent's console lets a child reset console modes
and corrupt the TUI.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CREATE_NEW_CONSOLE = 0x00000010


def windows_hidden_kwargs() -> dict[str, object]:
    """``subprocess`` keyword arguments that hide the child window on Windows."""
    if sys.platform != "win32":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": info, "creationflags": _CREATE_NEW_CONSOLE}


def pid_alive(pid: int) -> bool:
    """Whether that process is running; a read-only probe that starts nothing."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
