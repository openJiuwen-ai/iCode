# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared TUI clipboard helpers."""

from __future__ import annotations

from dataclasses import replace
from types import ModuleType

import pytest

from chrys.app.tui import clipboard as clipboard_module
from chrys.app.tui.clipboard import BROWSER_CLIPBOARD_META_TYPE, copy_text_to_clipboards, paste_text_from_clipboards


class _ClipboardApp:
    def __init__(self) -> None:
        self._clipboard = ""

    @property
    def clipboard(self) -> str:
        return self._clipboard

    @clipboard.setter
    def clipboard(self, text: str) -> None:
        self._clipboard = text

    def copy_to_clipboard(self, text: str) -> None:
        self.clipboard = text


class _BrowserClipboardDriver:
    def __init__(self) -> None:
        self.meta: list[dict[str, str]] = []

    def write_meta(self, data: dict[str, str]) -> None:
        self.meta.append(data)


class _BrowserClipboardApp(_ClipboardApp):
    def __init__(self) -> None:
        super().__init__()
        self._driver = _BrowserClipboardDriver()


class _FailingClipboardApp:
    _clipboard = ""

    def copy_to_clipboard(self, _text: str) -> None:
        raise RuntimeError("terminal clipboard unavailable")


@pytest.fixture(autouse=True)
def _local_posix_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    # These existing cases exercise the POSIX OS fallback independently of
    # the runner; Windows routing has its own asynchronous regression cases.
    platform = clipboard_module.platform_helpers
    shadow = ModuleType(platform.__name__)
    shadow.__dict__.update(platform.__dict__)
    info = replace(platform.get_platform(), os_name="macos")
    shadow.get_platform = lambda: info
    monkeypatch.setattr(clipboard_module, "platform_helpers", shadow)


def test_paste_text_from_clipboards_prefers_os_clipboard_for_native_tui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: "fresh-os-text")
    app = _ClipboardApp()
    app.clipboard = "stale-app-text"

    assert paste_text_from_clipboards(app) == "fresh-os-text"


def test_paste_text_from_clipboards_falls_back_to_app_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: "")
    app = _ClipboardApp()
    app.clipboard = "app-text"

    assert paste_text_from_clipboards(app) == "app-text"


def test_paste_text_from_clipboards_does_not_read_server_clipboard_in_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_server_clipboard_read() -> str:
        raise AssertionError("server clipboard must stay isolated from browser sessions")

    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", fail_server_clipboard_read)
    app = _ClipboardApp()
    app.clipboard = "browser-session-text"

    assert paste_text_from_clipboards(app) == "browser-session-text"


def test_copy_text_to_clipboards_replaces_unencodable_surrogates(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
    app = _ClipboardApp()

    assert copy_text_to_clipboards(app, "bad\udcfftext")

    assert app.clipboard == r"bad\udcfftext"
    assert copied == [r"bad\udcfftext"]


def test_copy_text_to_clipboards_sizes_sanitized_text(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
    app = _ClipboardApp()

    assert not copy_text_to_clipboards(app, "\udcff", max_terminal_bytes=5)

    assert app.clipboard == ""
    assert copied == [r"\udcff"]


def test_copy_text_to_clipboards_suppresses_terminal_copy_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

    assert not copy_text_to_clipboards(_FailingClipboardApp(), "text")

    assert copied == ["text"]


def test_copy_text_to_clipboards_suppresses_os_copy_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_os_copy(_text: str) -> None:
        raise RuntimeError("os clipboard unavailable")

    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", fail_os_copy)
    app = _ClipboardApp()

    assert copy_text_to_clipboards(app, "text")

    assert app.clipboard == "text"


@pytest.mark.parametrize("os_name", ["windows", "macos", "linux"])
@pytest.mark.parametrize("terminal_path", ["normal", "too-large", "unavailable"])
def test_copy_text_to_clipboards_dispatches_browser_meta_without_touching_server_clipboard(
    monkeypatch: pytest.MonkeyPatch, os_name: str, terminal_path: str
) -> None:
    copied: list[str] = []
    platform = clipboard_module.platform_helpers
    info = replace(platform.get_platform(), os_name=os_name)
    monkeypatch.setattr(platform, "get_platform", lambda: info)
    monkeypatch.setattr(platform, "clipboard_copy", copied.append)
    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")
    app = _BrowserClipboardApp()

    def reject(text: str) -> None:
        raise RuntimeError("terminal unavailable")

    if terminal_path == "unavailable":
        monkeypatch.setattr(app, "copy_to_clipboard", reject)

    assert copy_text_to_clipboards(app, "browser text", max_terminal_bytes=1 if terminal_path == "too-large" else None)

    assert app.clipboard == "browser text"
    assert app._driver.meta == [{"type": BROWSER_CLIPBOARD_META_TYPE, "text": "browser text"}]
    assert copied == []
