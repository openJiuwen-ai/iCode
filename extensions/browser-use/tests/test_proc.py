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


def test_launcher_read_port_file_requires_complete_line(tmp_path) -> None:
    """Wave 3 P2-14: half-written or out-of-range DevToolsActivePort must not parse prematurely."""
    from icode_browser_use.cdp import launcher

    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222", encoding="utf-8")
    assert launcher._read_port_file(port_file) is None

    port_file.write_text("70000\n/devtools/browser/abc\n", encoding="utf-8")
    assert launcher._read_port_file(port_file) is None

    port_file.write_text("9222\n/devtools/browser/abc\n", encoding="utf-8")
    assert launcher._read_port_file(port_file) == 9222


def test_launcher_windows_spawn_flags_and_terminate_fallback(tmp_path, monkeypatch) -> None:
    """Wave 3 P1-17 & P2-14: Windows spawn isolates console/group (never CREATE_NO_WINDOW) and job kill falls back."""
    from types import SimpleNamespace

    from icode_browser_use.cdp import launcher

    seen_calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            self.pid = 43210
            seen_calls.append({"argv": argv, **kwargs})

        def kill(self) -> None:
            pass

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(launcher, "_win_create_kill_job", lambda: None)
    monkeypatch.setattr(launcher, "_win_resume_main_thread", lambda _pid: True)

    log_path = tmp_path / "chrome.log"
    with log_path.open("wb") as log_file:
        launcher._spawn_windows(["chrome"], log_file, headless=True)
        launcher._spawn_windows(["chrome"], log_file, headless=False)

    assert len(seen_calls) == 2
    for call in seen_calls:
        flags = int(call["creationflags"])  # type: ignore[arg-type]
        assert flags & launcher._CREATE_SUSPENDED
        assert flags & launcher._CREATE_NEW_PROCESS_GROUP
        assert not (flags & 0x08000000), "CREATE_NO_WINDOW must never be passed"
    assert int(seen_calls[1]["creationflags"]) & launcher._DETACHED_PROCESS  # type: ignore[arg-type]

    # When TerminateJobObject returns 0 (failure), _terminate_windows must fall back to taskkill.
    run_calls: list[list[str]] = []

    class FakeKernel32:
        def TerminateJobObject(self, _job, _code) -> int:
            return 0

        def OpenProcess(self, *_args) -> int:
            return 0

    monkeypatch.setattr(launcher, "_kernel32", lambda: FakeKernel32())
    monkeypatch.setattr(launcher, "_win_process_exists", lambda _pid: False)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda cmd, **_kw: run_calls.append(list(cmd)) or SimpleNamespace(returncode=0),
    )
    launcher._terminate_windows(43210, job=99)
    assert any("taskkill" in str(cmd[0]).lower() for cmd in run_calls)
