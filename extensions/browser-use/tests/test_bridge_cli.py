# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import icode_browser_use.bridge.install as bridge_install

PUBLIC_KEY = "dGVzdC1rZXk="


def _extension(tmp_path: Path) -> Path:
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text(
        json.dumps({"key": PUBLIC_KEY, "version": bridge_install.__version__}), encoding="utf-8"
    )
    return extension


def test_cli_install_uses_the_pinned_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    extension = _extension(tmp_path)
    expected = bridge_install.extension_id_from_manifest(extension)
    host = tmp_path / "host.exe"
    host.write_text("", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_install(host_path: Path, extension_id: str, **kwargs):
        captured.update(host_path=host_path, extension_id=extension_id, **kwargs)
        return tmp_path / "native.json"

    monkeypatch.setattr(bridge_install, "install_native_host", fake_install)
    copied = tmp_path / "copied"
    bridge_install.main(
        [
            "install",
            "--host-path",
            str(host),
            "--extension-dir",
            str(extension),
            "--extension-copy",
            str(copied),
            "--browser",
            "chromium",
        ]
    )
    assert captured["host_path"] == host
    assert captured["extension_id"] == expected
    assert captured["browsers"] == ("chromium",)
    assert (copied / "manifest.json").is_file()
    assert "native.json" in capsys.readouterr().out


def test_cli_uninstall_passes_browsers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_uninstall(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(bridge_install, "uninstall_native_host", fake_uninstall)
    bridge_install.main(["uninstall", "--browser", "chrome"])
    assert captured["browsers"] == ("chrome",)


def test_cli_doctor_reports_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    extension = _extension(tmp_path)
    host = tmp_path / "host.exe"
    host.write_text("", encoding="utf-8")
    manifest = tmp_path / "native.json"
    extension_id = bridge_install.extension_id_from_manifest(extension)
    manifest.write_text(json.dumps(bridge_install.native_host_manifest(host, extension_id)), encoding="utf-8")
    discovery = tmp_path / "bridge.json"
    discovery.write_text(json.dumps({"pid": os.getpid(), "generation": "abcdef"}), encoding="utf-8")

    monkeypatch.setattr(bridge_install, "default_browser_executable", lambda: chrome)
    monkeypatch.setattr(bridge_install, "default_extension_install_dir", lambda: extension)
    monkeypatch.setattr(
        bridge_install,
        "registered_manifest",
        lambda name: manifest if name == "chrome" else None,
    )
    monkeypatch.setattr("icode_browser_use.bridge.host.bridge_file", lambda: discovery)
    with pytest.raises(SystemExit) as exit_info:
        bridge_install.main(["doctor", "--manifest-path", str(manifest)])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "browser:" in output
    assert "extension:" in output
    assert "doctor: ok" in output


def test_cli_doctor_reports_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    extension = _extension(tmp_path)
    monkeypatch.setattr(bridge_install, "default_browser_executable", lambda: None)
    monkeypatch.setattr(bridge_install, "default_extension_install_dir", lambda: extension)
    monkeypatch.setattr(bridge_install, "registered_manifest", lambda _name: None)
    with pytest.raises(SystemExit) as exit_info:
        bridge_install.main(["doctor", "--manifest-path", str(tmp_path / "missing.json")])
    assert exit_info.value.code == 1
    output = capsys.readouterr().out
    assert "not installed" in output
    assert "problem(s)" in output


def test_cli_doctor_handles_missing_extension_with_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    host = tmp_path / "host.exe"
    host.write_text("", encoding="utf-8")
    manifest = tmp_path / "native.json"
    manifest.write_text(json.dumps({"path": str(host), "allowed_origins": []}), encoding="utf-8")
    monkeypatch.setattr(bridge_install, "default_extension_install_dir", lambda: tmp_path / "missing")
    monkeypatch.setattr(bridge_install, "registered_manifest", lambda _name: manifest)
    with pytest.raises(SystemExit) as exit_info:
        bridge_install.main(["doctor", "--manifest-path", str(manifest)])
    assert exit_info.value.code == 1
    assert "not installed" in capsys.readouterr().out


def test_cli_doctor_reports_a_stale_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    extension = _extension(tmp_path)
    host = tmp_path / "host.exe"
    host.write_text("", encoding="utf-8")
    extension_id = bridge_install.extension_id_from_manifest(extension)
    manifest = tmp_path / "native.json"
    manifest.write_text(json.dumps(bridge_install.native_host_manifest(host, extension_id)), encoding="utf-8")
    discovery = tmp_path / "bridge.json"
    discovery.write_text(json.dumps({"pid": 999999, "generation": "abc"}), encoding="utf-8")
    monkeypatch.setattr(bridge_install, "default_extension_install_dir", lambda: extension)
    monkeypatch.setattr(bridge_install, "registered_manifest", lambda _name: manifest)
    monkeypatch.setattr("icode_browser_use.bridge.host.bridge_file", lambda: discovery)
    with pytest.raises(SystemExit) as exit_info:
        bridge_install.main(["doctor", "--manifest-path", str(manifest)])
    assert exit_info.value.code == 1
    assert "stale pid" in capsys.readouterr().out


def test_cli_doctor_handles_a_non_object_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    extension = _extension(tmp_path)
    manifest = tmp_path / "native.json"
    manifest.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(bridge_install, "default_extension_install_dir", lambda: extension)
    monkeypatch.setattr(bridge_install, "registered_manifest", lambda _name: manifest)
    with pytest.raises(SystemExit) as exit_info:
        bridge_install.main(["doctor", "--manifest-path", str(manifest)])
    assert exit_info.value.code == 1
    assert "unreadable manifest" in capsys.readouterr().out


def test_cli_requires_a_host_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extension = _extension(tmp_path)
    monkeypatch.setattr(bridge_install.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit):
        bridge_install.main(["install", "--extension-dir", str(extension)])


def test_cli_install_opens_setup_when_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    extension = _extension(tmp_path)
    host = tmp_path / "host.exe"
    host.write_text("", encoding="utf-8")
    copied = tmp_path / "copied"
    opened: dict[str, object] = {}

    monkeypatch.setattr(
        bridge_install,
        "install_native_host",
        lambda _host, _extension_id, **_kwargs: tmp_path / "native.json",
    )
    monkeypatch.setattr(
        bridge_install,
        "open_browser_setup",
        lambda extension_dir: opened.update(path=extension_dir),
    )
    bridge_install.main(
        [
            "install",
            "--host-path",
            str(host),
            "--extension-dir",
            str(extension),
            "--extension-copy",
            str(copied),
            "--open",
        ]
    )
    assert opened["path"] == copied
    assert (copied / "manifest.json").is_file()


def test_open_browser_setup_launches_chrome_and_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []
    folders: list[Path] = []
    monkeypatch.setattr(bridge_install, "_launch", lambda command: launched.append(command))
    monkeypatch.setattr(bridge_install, "_open_folder", lambda path: folders.append(path))

    bridge_install.open_browser_setup(tmp_path, chrome_path=Path("chrome.exe"))
    assert launched and "chrome://extensions" in launched[0]
    assert folders == [tmp_path]


def test_chrome_devtools_ws_reads_devtools_active_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/abc\n", encoding="utf-8")
    monkeypatch.setattr(bridge_install, "_default_chrome_user_data_dir", lambda: tmp_path)
    assert bridge_install.chrome_devtools_ws() == "ws://127.0.0.1:12345/devtools/browser/abc"


def test_load_extension_via_cdp_calls_load_unpacked(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class FakeClient:
        async def send(self, method, params=None, session_id=None, timeout=None):
            calls.append((method, params))
            return {"id": "jmlpeablcffobekmjmpibdmcdfegmghf"}

        async def aclose(self) -> None:
            calls.append(("closed", None))

    async def factory(_ws: str) -> FakeClient:
        return FakeClient()

    loaded = bridge_install.load_extension_via_cdp(
        tmp_path / "extension", ws_url="ws://127.0.0.1:1/devtools/browser/x", client_factory=factory
    )
    assert loaded == "jmlpeablcffobekmjmpibdmcdfegmghf"
    assert calls[0] == ("Extensions.loadUnpacked", {"path": str(tmp_path / "extension")})
    assert calls[-1][0] == "closed"


def test_cli_install_loads_via_chrome_cdp_when_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extension = _extension(tmp_path)
    host = tmp_path / "host.exe"
    host.write_text("", encoding="utf-8")
    copied = tmp_path / "copied"
    loaded: dict[str, object] = {}

    monkeypatch.setattr(
        bridge_install,
        "install_native_host",
        lambda _host, _extension_id, **_kwargs: tmp_path / "native.json",
    )
    monkeypatch.setattr(
        bridge_install,
        "load_extension_via_cdp",
        lambda extension_dir: loaded.update(path=extension_dir) or "abc",
    )
    bridge_install.main(
        [
            "install",
            "--host-path",
            str(host),
            "--extension-dir",
            str(extension),
            "--extension-copy",
            str(copied),
            "--chrome-cdp",
        ]
    )
    assert loaded["path"] == copied
