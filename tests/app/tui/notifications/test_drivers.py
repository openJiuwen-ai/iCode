# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native notification driver command and fail-soft coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest

from chrys.app.tui.notifications import drivers
from chrys.foundation.platform import process as process_mod


def _stub_notification_spawn(monkeypatch: pytest.MonkeyPatch, spawn: Callable[..., Awaitable[object]]) -> None:
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(drivers, "managed_subprocess", process_mod.managed_subprocess)


@pytest.mark.asyncio
async def test_macos_terminal_notifier_uses_title_body_and_sound(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], float]] = []
    monkeypatch.setattr(drivers.shutil, "which", lambda name: "/opt/bin/terminal-notifier" if name else None)

    async def _run(argv: list[str], *, timeout: float = 3.0) -> bool:
        calls.append((argv, timeout))
        return True

    monkeypatch.setattr(drivers, "_run_command", _run)

    result = await drivers.MacOSNotificationDriver().send(
        drivers.NotificationPayload(title="Chrys", body="Turn finished", sound=True)
    )

    assert result == drivers.NotificationDeliveryResult(desktop_sent=True, sound_sent=True)
    assert calls == [
        (["/opt/bin/terminal-notifier", "-title", "Chrys", "-message", "Turn finished", "-sound", "default"], 3.0)
    ]


@pytest.mark.asyncio
async def test_macos_applescript_fallback_escapes_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(drivers.shutil, "which", lambda _name: None)

    async def _run(argv: list[str], *, timeout: float = 3.0) -> bool:
        del timeout
        calls.append(argv)
        return True

    monkeypatch.setattr(drivers, "_run_command", _run)

    result = await drivers.MacOSNotificationDriver().send(
        drivers.NotificationPayload(title='Chrys "Agent"', body="path\\to\nnext", sound=False)
    )

    assert result == drivers.NotificationDeliveryResult(desktop_sent=True, sound_sent=False)
    assert calls == [
        [
            "/usr/bin/osascript",
            "-e",
            'display notification "path\\\\to\\nnext" with title "Chrys \\"Agent\\""',
        ]
    ]


@pytest.mark.asyncio
async def test_linux_notification_uses_canberra_then_paplay_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    binaries = {
        "notify-send": "/usr/bin/notify-send",
        "canberra-gtk-play": "/usr/bin/canberra-gtk-play",
        "paplay": "/usr/bin/paplay",
    }
    calls: list[list[str]] = []
    monkeypatch.setattr(drivers.shutil, "which", binaries.get)

    class _SoundPath:
        def is_file(self) -> bool:
            return True

        def __str__(self) -> str:
            return "/sounds/message.oga"

    monkeypatch.setattr(drivers, "Path", lambda _value: _SoundPath())

    async def _run(argv: list[str], *, timeout: float = 3.0) -> bool:
        del timeout
        calls.append(argv)
        return argv[0] != "/usr/bin/canberra-gtk-play"

    monkeypatch.setattr(drivers, "_run_command", _run)

    result = await drivers.LinuxNotificationDriver().send(
        drivers.NotificationPayload(title="Chrys", body="Done", sound=True)
    )

    assert result == drivers.NotificationDeliveryResult(desktop_sent=True, sound_sent=True)
    assert calls == [
        ["/usr/bin/notify-send", "Chrys", "Done"],
        ["/usr/bin/canberra-gtk-play", "-i", "message-new-instant"],
        ["/usr/bin/paplay", "/sounds/message.oga"],
    ]


@pytest.mark.asyncio
async def test_windows_powershell_balloon_and_missing_shell_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drivers.shutil, "which", lambda _name: None)
    missing = await drivers.WindowsNotificationDriver().send(drivers.NotificationPayload(title="Chrys", body="Done"))
    assert missing == drivers.NotificationDeliveryResult()

    calls: list[tuple[list[str], float]] = []
    monkeypatch.setattr(drivers.shutil, "which", lambda name: "C:\\pwsh.exe" if name == "pwsh" else None)

    async def _run(argv: list[str], *, timeout: float = 3.0) -> bool:
        calls.append((argv, timeout))
        return True

    monkeypatch.setattr(drivers, "_run_command", _run)
    sent = await drivers.WindowsNotificationDriver().send(
        drivers.NotificationPayload(title="O'Brien", body="Finished", sound=True)
    )

    assert sent == drivers.NotificationDeliveryResult(desktop_sent=True, sound_sent=True)
    argv, timeout = calls[0]
    assert argv[:3] == ["C:\\pwsh.exe", "-NoProfile", "-NonInteractive"]
    assert "BalloonTipTitle='O''Brien'" in argv[4]
    assert "SystemSounds]::Asterisk.Play()" in argv[4]
    assert timeout == 5.0


def test_default_notification_driver_dispatches_by_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        ((True, False, False), drivers.MacOSNotificationDriver),
        ((False, True, False), drivers.LinuxNotificationDriver),
        ((False, False, True), drivers.WindowsNotificationDriver),
        ((False, False, False), drivers.NullNotificationDriver),
    ]
    for flags, expected in cases:
        platform = SimpleNamespace(is_macos=flags[0], is_linux=flags[1], is_windows=flags[2])
        monkeypatch.setattr(drivers, "get_platform", lambda platform=platform: platform)
        assert isinstance(drivers.default_notification_driver(), expected)


async def test_mocking_the_native_subprocess_is_required() -> None:
    with pytest.raises(AssertionError, match="Stub the notification subprocess"):
        await drivers._run_command(["nonexistent-test-notification-command"])


@pytest.mark.asyncio
async def test_run_command_swallows_spawn_nonzero_and_timeout_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise_oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("missing")

    _stub_notification_spawn(monkeypatch, _raise_oserror)
    assert await drivers._run_command(["missing"]) is False

    class _NonzeroProcess:
        pid = None
        returncode = 7

        async def wait(self) -> int:
            return self.returncode

    async def _nonzero(*_args: object, **_kwargs: object) -> _NonzeroProcess:
        return _NonzeroProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _nonzero)
    assert await drivers._run_command(["fails"]) is False

    class _HungProcess:
        pid = None
        returncode = None

        def __init__(self) -> None:
            self.killed = False

        async def wait(self) -> int:
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.killed = True

    hung = _HungProcess()

    async def _hung(*_args: object, **_kwargs: object) -> _HungProcess:
        return hung

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _hung)
    assert await drivers._run_command(["hangs"], timeout=0.001) is False
    assert hung.killed is True


@pytest.mark.asyncio
async def test_run_command_spawns_hidden_windows_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows hidden-console kwargs must reach the actual spawn call.

    Regression: a bare ``create_subprocess_exec`` flashed a blank PowerShell
    console window on every desktop notification.
    """
    captured: dict[str, object] = {}

    class _Proc:
        pid = None
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def _capture(*argv: str, **kwargs: object) -> _Proc:
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return _Proc()

    hidden = {"creationflags": process_mod._CREATE_NEW_CONSOLE, "startupinfo": "hidden-startupinfo"}
    monkeypatch.setattr(process_mod.sys, "platform", "win32")
    monkeypatch.setattr(process_mod, "windows_hidden_subprocess_kwargs", lambda: dict(hidden))
    _stub_notification_spawn(monkeypatch, _capture)

    assert await drivers._run_command(["C:\\pwsh.exe", "-NoProfile", "-Command", "notify"]) is True

    assert captured["argv"] == ["C:\\pwsh.exe", "-NoProfile", "-Command", "notify"]
    kwargs = captured["kwargs"]
    assert kwargs["creationflags"] == process_mod._CREATE_NEW_CONSOLE
    assert kwargs["startupinfo"] == "hidden-startupinfo"
    assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
    assert kwargs["stdout"] is asyncio.subprocess.DEVNULL
    assert kwargs["stderr"] is asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_run_command_timeout_cleanup_hides_taskkill_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung notification's taskkill cleanup spawn must be hidden too.

    Regression: timeout cleanup ran ``taskkill.exe`` via bare
    ``subprocess.run``, flashing a console after the 5s notification timeout.
    """
    taskkill = "C:\\Windows\\System32\\taskkill.exe"
    taskkill_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _TaskkillProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    class _HungProcess:
        pid = 4242
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False

        async def wait(self) -> int:
            if not self.killed:
                await asyncio.Event().wait()
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.killed = True

    hung = _HungProcess()

    async def _spawn(*argv: object, **kwargs: object) -> _HungProcess | _TaskkillProcess:
        if argv[0] == taskkill:
            taskkill_calls.append((argv, kwargs))
            return _TaskkillProcess()
        return hung

    hidden = {"creationflags": process_mod._CREATE_NEW_CONSOLE, "startupinfo": "hidden-startupinfo"}
    monkeypatch.setattr(process_mod.sys, "platform", "win32")
    monkeypatch.setattr(process_mod, "windows_hidden_subprocess_kwargs", lambda: dict(hidden))
    monkeypatch.setattr(process_mod.shutil, "which", lambda name: taskkill if name == "taskkill" else None)
    _stub_notification_spawn(monkeypatch, _spawn)

    assert await drivers._run_command(["C:\\pwsh.exe", "-Command", "notify"], timeout=0.001) is False

    assert hung.killed is True
    [(argv, kwargs)] = taskkill_calls
    assert argv == (taskkill, "/PID", "4242", "/T", "/F")
    assert kwargs["creationflags"] == process_mod._CREATE_NEW_CONSOLE
    assert kwargs["startupinfo"] == "hidden-startupinfo"
