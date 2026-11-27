# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for TUI terminal launcher capability helpers."""

from __future__ import annotations

import shlex
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest

from chrys.app.tui.terminal import launcher
from chrys.app.tui.terminal.launcher import (
    TerminalLaunchError,
    build_chrys_tui_argv,
    can_access_local_desktop,
    can_open_new_chrys_window,
    is_remote_execution_environment,
    is_remote_ssh_session,
)
from chrys.foundation import guard
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message


@pytest.fixture(autouse=True)
def _sandbox_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """The argv these tests pin is the one a process without the sandbox builds."""
    monkeypatch.setattr(guard, "status", lambda: guard.GuardStatus(state="inactive", requested=False))


def test_a_sandboxed_window_opens_a_sandboxed_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "status", lambda: guard.GuardStatus(state="active", requested=True))
    argv = build_chrys_tui_argv("session-1")
    assert argv[-3:] == ["--session", "session-1", "--sandbox"]
    # The flag survives dispatch: the new process strips it before routing the TUI subcommand.
    rest, decision = guard.decide(argv[argv.index("--session") - 1 :], launcher.Path("/nonexistent"), env={})
    assert decision.enabled is True and rest[0] == launcher._TUI_SUBPROCESS_COMMAND


_MACOS_WINDOW_SERVER_PROBE = launcher._macos_window_server_session_is_available
_WINDOWS_DESKTOP_PROBE = launcher._windows_desktop_shell_is_visible
_TERMINAL_ENV_MARKERS = (
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "CODESPACES",
    "CLOUD_SHELL",
    "GITPOD_WORKSPACE_ID",
    "REPL_ID",
    "TEXTUAL_DRIVER",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "MIR_SOCKET",
    "PYAPP",
    "TERM",
    "TERM_PROGRAM",
    "LC_TERMINAL",
    "TERMINAL_EMULATOR",
    "GHOSTTY_BIN_DIR",
    "GHOSTTY_RESOURCES_DIR",
    "WEZTERM_EXECUTABLE",
    "WEZTERM_PANE",
    "WT_SESSION",
    "WT_PROFILE_ID",
    "KITTY_LISTEN_ON",
    "GNOME_TERMINAL_SCREEN",
    "GNOME_TERMINAL_SERVICE",
    "KONSOLE_VERSION",
    "KONSOLE_DBUS_SESSION",
    "KONSOLE_DBUS_WINDOW",
)


@pytest.fixture(autouse=True)
def _isolate_terminal_launcher_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _TERMINAL_ENV_MARKERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(launcher, "_macos_window_server_session_is_available", lambda: True)
    monkeypatch.setattr(launcher, "_windows_desktop_shell_is_visible", lambda: True)


def _mock_platform(monkeypatch: pytest.MonkeyPatch, *, macos: bool = False, windows: bool = False) -> None:
    monkeypatch.setattr(launcher, "get_platform", lambda: SimpleNamespace(is_macos=macos, is_windows=windows))


@pytest.mark.skipif(sys.platform != "darwin", reason="requires the macOS AppleScript interpreter")
@pytest.mark.parametrize("tab", (True, False), ids=("tab", "window"))
async def test_exiting_fork_dialog_reaps_pending_applescript(monkeypatch: pytest.MonkeyPatch, tab: bool) -> None:
    import psutil
    from textual.app import App

    from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog
    from chrys.app.tui.terminal import fork_tab
    from tests.support.tui_helpers import click_when_settled
    from tests.support.waiting import wait_for

    _mock_platform(monkeypatch, macos=True)
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    run_script = launcher.run_macos_terminal_script
    marker = "pending-fork-test"

    async def pending_script(*args: str) -> subprocess.CompletedProcess[str]:
        return await run_script("delay 30", marker)

    monkeypatch.setattr(fork_tab if tab else launcher, "run_macos_terminal_script", pending_script)
    target = fork_tab.ForkTabTarget("Terminal", "/dev/ttys001") if tab else None
    app = App()
    parent = psutil.Process()
    sender = None

    def find_sender() -> bool:
        nonlocal sender
        # A sandbox broker owns the real command; its stub is the direct child.
        for child in parent.children(recursive=True):
            if child.name() == "osascript" and marker in child.cmdline():
                sender = child
                return True
        return False

    try:
        async with app.run_test() as pilot:
            await app.push_screen(ForkSessionDialog("fork-id", tab_target=target))
            await click_when_settled(pilot, "#fork-session-open")
            await wait_for(find_sender, pilot=pilot, description="pending AppleScript sender")
            app.exit()
        assert sender is not None
        await wait_for(lambda: not sender.is_running(), description="AppleScript sender reaped on exit")
    finally:
        if sender is not None and sender.is_running():
            sender.kill()
            sender.wait(timeout=5)


class _FakeProcess:
    def __init__(self, return_code: int | None = 0) -> None:
        self._return_code = return_code

    def wait(self, timeout: float | None = None) -> int:
        if self._return_code is None:
            raise subprocess.TimeoutExpired("fake-terminal", 0 if timeout is None else timeout)
        return self._return_code


class _FakeCFunction:
    def __init__(self, result: object) -> None:
        self.result = result
        self.argtypes: object = None
        self.restype: object = None
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        if callable(self.result):
            return cast(Any, self.result)(*args)
        return self.result


def test_remote_ssh_session_detection() -> None:
    assert is_remote_ssh_session({"SSH_CONNECTION": "127.0.0.1 50000 127.0.0.1 22"})
    assert is_remote_ssh_session({"SSH_CLIENT": "127.0.0.1 50000 22"})
    assert is_remote_ssh_session({"SSH_TTY": "/dev/pts/1"})
    assert not is_remote_ssh_session({})


@pytest.mark.parametrize(
    "env",
    (
        {"SSH_CONNECTION": "127.0.0.1 50000 127.0.0.1 22"},
        {"TEXTUAL_DRIVER": "textual.drivers.web_driver:WebDriver"},
        {"CODESPACES": "true"},
        {"CLOUD_SHELL": "1"},
        {"GITPOD_WORKSPACE_ID": "workspace-1"},
        {"REPL_ID": "12345678-1234-1234-1234-123456789abc"},
    ),
)
def test_remote_execution_environment_detection(env: dict[str, str]) -> None:
    assert is_remote_execution_environment(env)


def test_remote_execution_environment_ignores_false_flags() -> None:
    assert not is_remote_execution_environment({"CODESPACES": "false", "CLOUD_SHELL": "0"})


def test_local_desktop_capability_uses_linux_display_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_platform(monkeypatch)

    assert can_access_local_desktop({"DISPLAY": ":0"})
    assert can_access_local_desktop({"WAYLAND_DISPLAY": "wayland-0"})
    assert not can_access_local_desktop({})


def test_local_desktop_capability_uses_windows_window_station(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_platform(monkeypatch, windows=True)
    monkeypatch.setattr(launcher, "_windows_desktop_shell_is_visible", lambda: True)
    assert can_access_local_desktop({})

    monkeypatch.setattr(launcher, "_windows_desktop_shell_is_visible", lambda: False)
    assert not can_access_local_desktop({})


@pytest.mark.parametrize(
    ("window_station", "info_ok", "flags", "shell_window", "expected"),
    (
        (None, True, 1, 1, False),
        (1, False, 1, 1, False),
        (1, True, 0, 1, False),
        (1, True, 1, None, False),
        (1, True, 1, 1, True),
    ),
)
def test_windows_desktop_probe_truth_table(
    monkeypatch: pytest.MonkeyPatch,
    window_station: object,
    info_ok: bool,
    flags: int,
    shell_window: object,
    expected: bool,
) -> None:
    import ctypes

    def write_flags(_station: object, _index: object, buffer: object, *_args: object) -> bool:
        cast(Any, buffer)._obj.flags = flags
        return info_ok

    user32 = SimpleNamespace(
        GetProcessWindowStation=_FakeCFunction(window_station),
        GetUserObjectInformationW=_FakeCFunction(write_flags),
        GetShellWindow=_FakeCFunction(shell_window),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: user32, raising=False)

    assert _WINDOWS_DESKTOP_PROBE() is expected


def test_windows_desktop_probe_handles_unavailable_user32(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    def fail_to_load(*_args: object, **_kwargs: object) -> object:
        raise OSError("user32 unavailable")

    monkeypatch.setattr(ctypes, "WinDLL", fail_to_load, raising=False)

    assert not _WINDOWS_DESKTOP_PROBE()


def test_local_desktop_capability_uses_macos_window_server_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_platform(monkeypatch, macos=True)
    monkeypatch.setattr(launcher, "_macos_window_server_session_is_available", lambda: True)
    assert can_access_local_desktop({})

    monkeypatch.setattr(launcher, "_macos_window_server_session_is_available", lambda: False)
    assert not can_access_local_desktop({})


@pytest.mark.parametrize(("session", "expected"), ((1234, True), (None, False)))
def test_macos_window_server_probe_releases_session(
    monkeypatch: pytest.MonkeyPatch, session: object, expected: bool
) -> None:
    import ctypes

    copy_session = _FakeCFunction(session)
    release = _FakeCFunction(None)
    libraries = {
        launcher._CORE_GRAPHICS_PATH: SimpleNamespace(CGSessionCopyCurrentDictionary=copy_session),
        launcher._CORE_FOUNDATION_PATH: SimpleNamespace(CFRelease=release),
    }
    monkeypatch.setattr(ctypes, "CDLL", libraries.__getitem__)

    assert _MACOS_WINDOW_SERVER_PROBE() is expected
    assert release.calls == ([(session,)] if expected else [])


def test_macos_window_server_probe_handles_unavailable_framework(monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes

    def fail_to_load(_path: str) -> object:
        raise OSError("framework unavailable")

    monkeypatch.setattr(ctypes, "CDLL", fail_to_load)

    assert not _MACOS_WINDOW_SERVER_PROBE()


def test_new_window_capability_is_disabled_remotely(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_platform(monkeypatch)
    assert not can_open_new_chrys_window({"SSH_CONNECTION": "127.0.0.1 50000 127.0.0.1 22"})
    assert can_open_new_chrys_window({"DISPLAY": ":0"})


def test_build_chrys_tui_argv_uses_python_module(monkeypatch) -> None:
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")

    assert build_chrys_tui_argv("session-1") == [
        "/usr/bin/python3",
        "-m",
        "chrys.app.cli.app",
        "__tui_subprocess__",
        "--session",
        "session-1",
    ]


def test_build_chrys_tui_argv_prefers_pyapp(monkeypatch) -> None:
    monkeypatch.setattr(launcher.Path, "is_file", lambda _path: True)
    monkeypatch.setenv("PYAPP", "/Applications/Chrys/chrys")

    assert build_chrys_tui_argv("session-1") == [
        "/Applications/Chrys/chrys",
        "__tui_subprocess__",
        "--session",
        "session-1",
    ]


def test_build_chrys_tui_argv_ignores_pyapp_sentinel(monkeypatch) -> None:
    monkeypatch.setenv("PYAPP", "1")
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")

    assert build_chrys_tui_argv("session-1") == [
        "/usr/bin/python3",
        "-m",
        "chrys.app.cli.app",
        "__tui_subprocess__",
        "--session",
        "session-1",
    ]


async def test_launch_new_window_rejects_unavailable_environment(monkeypatch) -> None:
    monkeypatch.setenv("SSH_TTY", "/dev/pts/1")

    try:
        await launcher.launch_new_chrys_window("session-1")
    except TerminalLaunchError as exc:
        assert str(exc) == f"Opening a new {APP_DISPLAY_NAME} window is not available in the current environment."
        assert exc.display_message is not None
        assert format_message(exc.display_message) == str(exc)
        assert Localizer("zh-Hans").render(exc.display_message) == f"当前环境无法打开新的 {APP_DISPLAY_NAME} 窗口。"
    else:
        raise AssertionError("expected TerminalLaunchError")


async def test_macos_launch_prefers_ghostty_when_running_in_ghostty(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    _mock_platform(monkeypatch, macos=True)
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    await launcher.launch_new_chrys_window("session-1", cwd="/repo")

    assert calls == [
        (
            [
                "/usr/bin/open",
                "-na",
                "Ghostty",
                "--args",
                "-e",
                *launcher.macos_login_shell_argv(launcher.build_macos_fork_argv("session-1"), cwd="/repo"),
            ],
            {
                "cwd": None,
                "stdin": launcher.subprocess.DEVNULL,
                "stdout": launcher.subprocess.DEVNULL,
                "stderr": launcher.subprocess.DEVNULL,
            },
        )
    ]


async def test_macos_launch_uses_wezterm_window_when_running_in_wezterm(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")
    _mock_platform(monkeypatch, macos=True)
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/local/bin/wezterm" if name == "wezterm" else None)

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    await launcher.launch_new_chrys_window("session-1", cwd="/repo")

    assert calls == [
        (
            [
                "/usr/local/bin/wezterm",
                "cli",
                "spawn",
                "--new-window",
                "--cwd",
                "/repo",
                "--",
                *launcher.macos_login_shell_argv(launcher.build_macos_fork_argv("session-1"), cwd="/repo"),
            ],
            {
                "cwd": None,
                "stdin": launcher.subprocess.DEVNULL,
                "stdout": launcher.subprocess.DEVNULL,
                "stderr": launcher.subprocess.DEVNULL,
            },
        )
    ]


async def test_macos_wezterm_failure_falls_back_to_terminal(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")
    _mock_platform(monkeypatch, macos=True)
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/local/bin/wezterm" if name == "wezterm" else None)

    def fake_popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        calls.append((argv, kwargs))
        if argv[0] == "/usr/local/bin/wezterm":
            return _FakeProcess(1)
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    scripts = []

    async def run_script(script: str, command: str):
        scripts.append(script)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(launcher, "run_macos_terminal_script", run_script)
    await launcher.launch_new_chrys_window("session-1", cwd="/repo")

    assert calls[0][0][:4] == ["/usr/local/bin/wezterm", "cli", "spawn", "--new-window"]
    assert len(scripts) == 1 and 'tell application "Terminal"' in scripts[0]


@pytest.mark.parametrize("host", ["Apple_Terminal", "iTerm.app"])
async def test_macos_window_uses_the_final_automation_result(monkeypatch, host) -> None:
    _mock_platform(monkeypatch, macos=True)
    monkeypatch.setenv("TERM_PROGRAM", host)

    async def denied(script, command):
        return subprocess.CompletedProcess([], 1, "", "Not authorized (-1743)")

    monkeypatch.setattr(launcher, "run_macos_terminal_script", denied)
    with pytest.raises(TerminalLaunchError, match="Not authorized"):
        await launcher.launch_new_chrys_window("session-1", cwd="/repo")


async def test_windows_launch_uses_new_windows_terminal_window(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("WT_SESSION", "window-session")
    _mock_platform(monkeypatch, windows=True)
    monkeypatch.setattr(launcher.sys, "executable", r"C:\Python\python.exe")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: r"C:\Windows\System32\wt.exe" if name == "wt" else None)

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    await launcher.launch_new_chrys_window("session-1", cwd=r"C:\repo")

    assert calls == [
        (
            [
                r"C:\Windows\System32\wt.exe",
                "-w",
                "new",
                "new-tab",
                "-d",
                r"C:\repo",
                r"C:\Python\python.exe",
                "-m",
                "chrys.app.cli.app",
                "__tui_subprocess__",
                "--session",
                "session-1",
            ],
            {
                "cwd": None,
                "stdin": launcher.subprocess.DEVNULL,
                "stdout": launcher.subprocess.DEVNULL,
                "stderr": launcher.subprocess.DEVNULL,
            },
        )
    ]


async def test_linux_launch_uses_kitty_window_when_remote_control_is_available(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("KITTY_LISTEN_ON", "unix:/tmp/kitty")
    _mock_platform(monkeypatch)
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/kitty" if name == "kitty" else None)

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    await launcher.launch_new_chrys_window("session-1", cwd="/repo")

    assert calls == [
        (
            [
                "/usr/bin/kitty",
                "@",
                "launch",
                "--type=os-window",
                "--cwd",
                "/repo",
                "--",
                "/usr/bin/python3",
                "-m",
                "chrys.app.cli.app",
                "__tui_subprocess__",
                "--session",
                "session-1",
            ],
            {
                "cwd": None,
                "stdin": launcher.subprocess.DEVNULL,
                "stdout": launcher.subprocess.DEVNULL,
                "stderr": launcher.subprocess.DEVNULL,
            },
        )
    ]


async def test_linux_launch_uses_gnome_terminal_window_when_running_in_gnome_terminal(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("GNOME_TERMINAL_SCREEN", "screen")
    _mock_platform(monkeypatch)
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None,
    )

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    await launcher.launch_new_chrys_window("session-1", cwd="/repo")

    assert calls == [
        (
            [
                "/usr/bin/gnome-terminal",
                "--window",
                "--",
                "/usr/bin/python3",
                "-m",
                "chrys.app.cli.app",
                "__tui_subprocess__",
                "--session",
                "session-1",
            ],
            {
                "cwd": "/repo",
                "stdin": launcher.subprocess.DEVNULL,
                "stdout": launcher.subprocess.DEVNULL,
                "stderr": launcher.subprocess.DEVNULL,
            },
        )
    ]


async def test_linux_launch_uses_available_terminal(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    _mock_platform(monkeypatch)
    monkeypatch.setattr(launcher.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None,
    )

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        calls.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    await launcher.launch_new_chrys_window("session-1", cwd="/repo")

    assert calls == [
        (
            [
                "/usr/bin/gnome-terminal",
                "--",
                "/usr/bin/python3",
                "-m",
                "chrys.app.cli.app",
                "__tui_subprocess__",
                "--session",
                "session-1",
            ],
            {
                "cwd": "/repo",
                "stdin": launcher.subprocess.DEVNULL,
                "stdout": launcher.subprocess.DEVNULL,
                "stderr": launcher.subprocess.DEVNULL,
            },
        )
    ]


async def test_iterm_window_runs_the_directory_change_in_a_shell(monkeypatch) -> None:
    commands = []

    async def run_script(script: str, command: str):
        assert "create window with default profile command startCommand" in script
        commands.append(command)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.setattr(launcher, "run_macos_terminal_script", run_script)
    argv = ["/path to/icode", "--session", "fork-id"]
    cwd = "/someone's project"
    await launcher._launch_macos(argv, cwd=cwd)
    assert shlex.split(shlex.split(commands[0])[-1]) == ["cd", cwd, "&&", *argv]


@pytest.mark.parametrize("source", ["workdir", "cli"])
def test_a_warned_parent_preserves_warning_policy_in_its_fork(tmp_path, monkeypatch, source):
    config, work = tmp_path / "config", tmp_path / "work"
    config.mkdir()
    (work / ".chrys").mkdir(parents=True)
    (config / "settings.yaml").write_text("workdir.config_enabled: true\n")
    (work / ".chrys/settings.yaml").write_text("sandbox.files.enabled: true\n")
    saved = guard.current()
    monkeypatch.setattr(guard, "status", lambda: guard.current().status())

    def missing():
        raise LookupError("missing test package")

    monkeypatch.setattr(guard, "_load_plugin", missing)
    monkeypatch.setenv(guard.ENV_ON_UNAVAILABLE, "warn" if source == "cli" else "refuse")
    try:
        parent = ["--sandbox"] if source == "cli" else []
        guard.install_at_startup([*parent, "-C", str(work)], config_dir=config)
        child = build_chrys_tui_argv("session-1")
        # A desktop terminal can launch independently, without the parent's environment.
        monkeypatch.delenv(guard.ENV_ON_UNAVAILABLE)
        _, decision = guard.decide(child, config, env={})
        assert decision.enabled is True
        assert decision.on_unavailable == "warn"
        _, status = guard.install_at_startup(child, config_dir=config)
        assert (status.state, status.requested) == ("inactive", True)
    finally:
        guard.set_current(saved)


def test_fork_cannot_weaken_a_refusing_parent_with_its_environment(tmp_path, monkeypatch):
    saved = guard.current()
    try:
        guard.set_current(
            guard._NoGuard(guard.GuardStatus(state="active", requested=True)),
            decision=guard.Decision(enabled=True, source="settings", on_unavailable="refuse"),
        )
        _, decision = guard.decide(build_chrys_tui_argv("session-1"), tmp_path, env={guard.ENV_ON_UNAVAILABLE: "warn"})
        assert decision.enabled is True
        assert decision.on_unavailable == "refuse"
    finally:
        guard.set_current(saved)
