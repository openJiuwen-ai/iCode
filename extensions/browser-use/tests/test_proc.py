# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import subprocess
import sys

from icode_browser_use._proc import windows_hidden_kwargs


def test_windows_hidden_kwargs_match_the_platform() -> None:
    kwargs = windows_hidden_kwargs()
    if sys.platform == "win32":
        assert "startupinfo" in kwargs
        assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE
        assert kwargs["creationflags"] == 0x00000010
    else:
        assert kwargs == {}


def test_owned_child_process_is_terminated_when_host_exits_normally(tmp_path) -> None:
    """P2 #4: Owned subprocesses spawned via launcher._spawn must not outlive a normal host exit (code 0)."""
    import time

    from icode_browser_use.cdp import launcher

    script = (
        "import sys; "
        "from pathlib import Path; "
        "from icode_browser_use.cdp import launcher; "
        "child = launcher._spawn([sys.executable, '-c', 'import time; time.sleep(60)'], "
        f"log_path=Path({str(tmp_path / 'child.log')!r})); "
        "print(child.pid, flush=True)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
        check=True,
    )
    grandchild_pid = int(proc.stdout.strip().splitlines()[-1])
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and launcher._process_exists(grandchild_pid):
        time.sleep(0.05)
    assert not launcher._process_exists(grandchild_pid), f"owned child pid={grandchild_pid} outlived host exit"
