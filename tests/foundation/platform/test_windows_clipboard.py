# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Win32 clipboard ownership and allocation cleanup, runnable on any host."""

from __future__ import annotations

import ctypes
from dataclasses import replace

import pytest

from chrys.foundation import platform


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_public_windows_copy_preserves_best_effort_contract(monkeypatch: pytest.MonkeyPatch, failure) -> None:
    info = replace(platform.get_platform(), os_name="windows")
    monkeypatch.setattr(platform, "get_platform", lambda: info)

    def busy(text: str) -> None:
        raise failure("clipboard unavailable")

    monkeypatch.setattr(platform, "_clipboard_copy_windows", busy)
    assert platform.clipboard_copy("new selection") is None


class _User32:
    def __init__(self, calls: list[tuple], failure: str) -> None:
        self.calls = calls
        self.failure = failure

    def CreateWindowExW(self, exstyle, class_name, title, style, x, y, width, height, parent, menu, instance, param):
        self.calls.append(("window", class_name, parent))
        return 0 if self.failure == "window" else 41

    def DestroyWindow(self, window: int) -> bool:
        self.calls.append(("destroy", window))
        return True

    def OpenClipboard(self, window: int | None) -> bool:
        self.calls.append(("open", window))
        return True

    def EmptyClipboard(self) -> bool:
        self.calls.append(("empty",))
        return self.failure != "empty"

    def SetClipboardData(self, format: int, handle: int) -> int:
        self.calls.append(("set", format, handle))
        return 0 if self.failure == "set" else handle

    def CloseClipboard(self) -> bool:
        self.calls.append(("close",))
        return True


class _Kernel32:
    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls
        self.buffer = ctypes.create_string_buffer(200)

    def GlobalAlloc(self, flags: int, size: int) -> int:
        self.calls.append(("alloc", flags, size))
        return 73

    def GlobalLock(self, handle: int) -> int:
        return ctypes.addressof(self.buffer)

    def GlobalUnlock(self, handle: int) -> bool:
        return True

    def GlobalFree(self, handle: int) -> int:
        self.calls.append(("free", handle))
        return 0


@pytest.mark.parametrize("failure", ["", "window", "empty", "set"])
def test_windows_copy_owns_window_and_transfers_or_frees_memory(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    calls: list[tuple] = []
    user32, kernel32 = _User32(calls, failure), _Kernel32(calls)
    api = platform._Win32ClipboardAPI(user32, kernel32)
    monkeypatch.setattr(platform, "_win32_clipboard_api", lambda: api)
    monkeypatch.setattr(platform, "_win32_error", lambda: OSError("native failure"))
    text = "中文😀\nnext line"
    expected_bytes = text.encode("utf-16-le") + b"\x00\x00"
    if failure:
        with pytest.raises(OSError, match="native failure"):
            platform._clipboard_copy_windows(text)
    else:
        platform._clipboard_copy_windows(text)

    expected = [("alloc", platform.GMEM_MOVEABLE, len(expected_bytes)), ("window", "STATIC", -3)]
    if failure != "window":
        expected.extend([("open", 41), ("empty",)])
        if failure != "empty":
            expected.append(("set", platform.CF_UNICODETEXT, 73))
        expected.extend([("close",), ("destroy", 41)])
    if failure:
        expected.append(("free", 73))
    assert calls == expected
    assert kernel32.buffer.raw[: len(expected_bytes)] == expected_bytes
