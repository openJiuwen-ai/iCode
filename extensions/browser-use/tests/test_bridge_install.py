# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

import pytest

import icode_browser_use.bridge.install as bridge_install
from icode_browser_use.bridge.install import (
    HOST_NAME,
    extension_id_from_key,
    extension_id_from_manifest,
    install_extension,
    install_native_host,
    native_host_manifest,
    packaged_extension_dir,
    uninstall_native_host,
    write_native_host_manifest,
)

PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxEr1O3xEwdhw3r0aeeyeBzFClZYbp2kS94GhnDIlkR8XPNSS"
    "VI4I7CfWCZnoqigG2wmCJiC7/LS2V9WpAujGUop1EAbbYIWdECoH90NYIh2el4YF+gSP4QV7+CJjQyJzWk6NjMUqx8weGg"
    "T0NNUv5wtHYeIX0bwU2fMuHemBKvxqzAY4PLgqD+2ZyweNop+3EMhq6Qe7OpQU215ztyvw4tPCnGtiuhlf3boTBIW34BSSZh"
    "Wj4FKqTn1KSw1KCQFIZX75Gq8I2ecLb2lHqkd/KvyBLAnXDnTWQnjDi3dUZLswIJvp+JYMaVyAX7fcQ7ywapWYO6P+lEGs7X"
    "C/OORIowIDAQAB"
)
EXPECTED_ID = "jmlpeablcffobekmjmpibdmcdfegmghf"


def test_extension_id_from_key_is_chrome_mapping() -> None:
    assert extension_id_from_key(PUBLIC_KEY) == EXPECTED_ID


def test_extension_id_from_manifest(tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "manifest.json").write_text(json.dumps({"key": PUBLIC_KEY}), encoding="utf-8")
    assert extension_id_from_manifest(extension) == EXPECTED_ID


def test_manifest_shape_and_origin() -> None:
    manifest = native_host_manifest(Path(r"C:\tools\icode-browser-use-host.exe"), "abcdefghijklmnop")
    assert manifest["name"] == HOST_NAME
    assert manifest["type"] == "stdio"
    assert manifest["path"] == r"C:\tools\icode-browser-use-host.exe"
    assert manifest["allowed_origins"] == ["chrome-extension://abcdefghijklmnop/"]


def test_manifest_rejects_a_bad_extension_id() -> None:
    with pytest.raises(ValueError):
        native_host_manifest(Path("host"), "")
    with pytest.raises(ValueError):
        native_host_manifest(Path("host"), "a/b")


def test_write_manifest_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "NativeMessagingHosts" / "com.chrys.browser.json"
    manifest = native_host_manifest(tmp_path / "host.exe", "abcdefghijklmnop")
    write_native_host_manifest(path, manifest)
    assert json.loads(path.read_text(encoding="utf-8")) == manifest


def test_packaged_extension_dir_is_complete() -> None:
    directory = packaged_extension_dir()
    assert (directory / "manifest.json").is_file()
    assert (directory / "background.js").is_file()
    assert len(extension_id_from_manifest(directory)) == 32


def test_install_extension_copies_files(tmp_path: Path) -> None:
    target = install_extension(packaged_extension_dir(), tmp_path / "extension")
    assert (target / "manifest.json").is_file()
    assert (target / "background.js").is_file()


def test_install_extension_removes_stale_files(tmp_path: Path) -> None:
    target = tmp_path / "extension"
    target.mkdir()
    (target / "stale.js").write_text("old", encoding="utf-8")
    install_extension(packaged_extension_dir(), target)
    assert not (target / "stale.js").exists()


def test_update_preserves_the_extension_id_and_replaces_files(tmp_path: Path, monkeypatch) -> None:
    target = install_extension(packaged_extension_dir(), tmp_path / "extension")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = "0.3.2"
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(bridge_install, "default_extension_install_dir", lambda: target)

    assert bridge_install.update_extension() == target
    assert extension_id_from_manifest(target) == EXPECTED_ID
    assert (target / "manifest.json").read_bytes() == (packaged_extension_dir() / "manifest.json").read_bytes()


def test_install_extension_reports_a_locked_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "extension"
    target.mkdir()

    def locked(_path):
        raise OSError("file in use")

    monkeypatch.setattr(bridge_install.shutil, "rmtree", locked)
    with pytest.raises(RuntimeError, match="close the browser"):
        install_extension(packaged_extension_dir(), target)


def test_install_registers_every_browser_and_writes_manifest(tmp_path: Path) -> None:
    calls: list[tuple[str, Path]] = []
    path = install_native_host(
        tmp_path / "host.exe",
        "abcdefghijklmnop",
        manifest_path=tmp_path / "com.chrys.browser.json",
        browsers=("chrome", "chromium", "edge"),
        registry_writer=lambda browser, manifest: calls.append((browser, manifest)),
    )
    assert calls == [("chrome", path), ("chromium", path), ("edge", path)]
    assert json.loads(path.read_text(encoding="utf-8"))["allowed_origins"] == ["chrome-extension://abcdefghijklmnop/"]


def test_uninstall_removes_manifest_and_registrations(tmp_path: Path) -> None:
    path = tmp_path / "com.chrys.browser.json"
    path.write_text("{}", encoding="utf-8")
    calls: list[tuple[str, Path]] = []
    uninstall_native_host(
        manifest_path=path,
        browsers=("chrome", "chromium", "edge"),
        registry_deleter=lambda browser, manifest: calls.append((browser, manifest)),
    )
    assert calls == [("chrome", path), ("chromium", path), ("edge", path)]
    assert not path.exists()


def test_uninstall_keeps_shared_manifest_until_last_browser_is_removed(tmp_path: Path) -> None:
    """Independent #9: uninstalling Edge while Chrome still points to the shared manifest must keep it."""
    path = tmp_path / "com.chrys.browser.json"
    registry: dict[str, Path] = {}

    install_native_host(
        tmp_path / "host.exe",
        "abcdefghijklmnop",
        manifest_path=path,
        browsers=("chrome", "edge"),
        registry_writer=lambda browser, manifest: registry.__setitem__(browser, manifest),
    )
    assert path.is_file()

    # Uninstall only Edge -> Chrome still points to path, so manifest must stay.
    uninstall_native_host(
        manifest_path=path,
        browsers=("edge",),
        registry_deleter=lambda browser, _manifest: registry.pop(browser, None),
        registry_reader=lambda browser: registry.get(browser),
    )
    assert "edge" not in registry
    assert "chrome" in registry
    assert path.is_file()

    # Uninstall Chrome (the last registration) -> shared manifest is now deleted.
    uninstall_native_host(
        manifest_path=path,
        browsers=("chrome",),
        registry_deleter=lambda browser, _manifest: registry.pop(browser, None),
        registry_reader=lambda browser: registry.get(browser),
    )
    assert registry == {}
    assert not path.exists()


def test_macos_edge_discovery_matches_between_owned_and_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Independent #9: on macOS with only Edge installed, both Owned and Bridge discovery find Edge."""
    from icode_browser_use.cdp import launcher

    edge_path = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.os.path, "isfile", lambda candidate: str(candidate) == edge_path)
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)

    assert launcher.discover_chrome() == str(Path(edge_path))
    assert bridge_install.default_browser_executable() == Path(edge_path)
    assert bridge_install.default_chrome_executable() is None
