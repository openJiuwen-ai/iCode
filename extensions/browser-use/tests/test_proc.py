# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import subprocess
import sys

from browser_use._proc import windows_hidden_kwargs


def test_windows_hidden_kwargs_match_the_platform() -> None:
    kwargs = windows_hidden_kwargs()
    if sys.platform == "win32":
        assert "startupinfo" in kwargs
        assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE
        assert kwargs["creationflags"] == 0x00000010
    else:
        assert kwargs == {}
