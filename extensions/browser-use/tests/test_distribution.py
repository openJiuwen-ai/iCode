# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from browser_use import distribution


def _entry_point(name: str = "browser") -> importlib.metadata.EntryPoint:
    return importlib.metadata.EntryPoint(
        name=name, value="browser_use.extension:extension", group=distribution.ENTRY_POINT_GROUP
    )


def _pack(root: Path) -> Path:
    pack = root / "extensions" / distribution.EXTENSION_ID
    (pack / "browser_use").mkdir(parents=True)
    (pack / "__init__.py").write_text("extension = None\n", encoding="utf-8")
    (pack / "browser_use" / "__init__.py").write_text("", encoding="utf-8")
    return pack


def _no_entry_points(monkeypatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: ())


def test_host_config_dir_follows_the_host(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(distribution, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert distribution.host_config_dir() == tmp_path / "roaming" / "chrys"

    monkeypatch.setattr(distribution, "sys", SimpleNamespace(platform="linux"))
    assert distribution.host_config_dir() == tmp_path / ".chrys"


def test_folder_pack_finds_both_host_layouts(tmp_path):
    assert distribution.folder_pack(tmp_path) is None
    module = tmp_path / "extensions" / "browser.py"
    module.parent.mkdir(parents=True)
    module.write_text("extension = None\n", encoding="utf-8")
    assert distribution.folder_pack(tmp_path) == module
    module.unlink()
    (tmp_path / "extensions" / "browser").mkdir()
    (tmp_path / "extensions" / "browser" / "__init__.py").write_text("", encoding="utf-8")
    assert distribution.folder_pack(tmp_path) == tmp_path / "extensions" / "browser" / "__init__.py"


def test_loaded_from_separates_the_two_loaders():
    assert distribution.loaded_from("browser_use.diagnose") == "distribution"
    assert distribution.loaded_from("_chrys_extension_browser.browser_use.cli") == "folder"


def test_conflict_needs_both_sources(tmp_path, monkeypatch):
    _no_entry_points(monkeypatch)
    _pack(tmp_path)
    assert distribution.conflict(tmp_path) is None
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: (_entry_point(),))
    folder, label = distribution.conflict(tmp_path)
    assert folder.parent.name == distribution.EXTENSION_ID
    assert label == "browser_use.extension:extension"


def test_broker_command_requires_a_python_interpreter_and_module_name():
    command = distribution.broker_command(module_name="browser_use.broker_client", executable="/usr/bin/python3.14")
    assert command == ["/usr/bin/python3.14", "-m", "browser_use.broker"]
    assert (
        distribution.broker_command(module_name="browser_use.broker_client", executable="C:/tools/icode.exe") is None
    )
    assert distribution.broker_command(module_name="_chrys_extension_browser.browser_use.x") is None


def test_install_pack_copies_and_uninstall_keeps_user_data(tmp_path, monkeypatch):
    _no_entry_points(monkeypatch)
    source = _pack(tmp_path / "export")
    config = tmp_path / "config"
    state = tmp_path / "state"
    profile = state / "chrome"
    profile.mkdir(parents=True)

    target = distribution.install_pack(source, config_dir=config)
    assert target == config / "extensions" / "browser"
    assert (target / "browser_use" / "__init__.py").is_file()
    assert not list(target.parent.glob(".browser.*.tmp"))

    with pytest.raises(ValueError, match="already exists"):
        distribution.install_pack(source, config_dir=config)
    assert distribution.install_pack(source, config_dir=config, replace=True) == target

    assert distribution.uninstall_pack(config_dir=config) == [target]
    assert not target.exists()
    assert profile.is_dir(), "uninstall must keep profiles and login data"
    assert distribution.uninstall_pack(config_dir=config) == []


def test_install_pack_refuses_a_second_source_for_one_id(tmp_path, monkeypatch):
    _pack(tmp_path)
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: (_entry_point(),))
    with pytest.raises(ValueError, match="already installed"):
        distribution.install_pack(tmp_path / "extensions" / "browser", config_dir=tmp_path / "config")


def test_install_pack_refuses_the_single_file_layout(tmp_path, monkeypatch):
    _no_entry_points(monkeypatch)
    source = _pack(tmp_path / "export")
    config = tmp_path / "config"
    single = config / "extensions" / "browser.py"
    single.parent.mkdir(parents=True)
    single.write_text("extension = None\n", encoding="utf-8")

    assert distribution.duplicate_folders(config) == []
    with pytest.raises(ValueError, match="single file"):
        distribution.install_pack(source, config_dir=config)
    assert distribution.folder_pack(config) == single

    _pack(config)
    assert distribution.duplicate_folders(config) == [single, config / "extensions" / "browser" / "__init__.py"]
    assert distribution.uninstall_pack(config_dir=config) == [
        config / "extensions" / "browser",
        single,
    ]
    assert distribution.folder_sources(config) == []


def test_install_pack_rejects_a_folder_that_is_not_a_pack(tmp_path, monkeypatch):
    _no_entry_points(monkeypatch)
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="not a browser extension folder package"):
        distribution.install_pack(tmp_path / "empty", config_dir=tmp_path / "config")


def test_distribution_version_reads_the_entry_point_distribution(monkeypatch):
    stub = SimpleNamespace(dist=SimpleNamespace(version="1.2.3"))
    assert distribution.distribution_version(stub) == "1.2.3"
    assert distribution.distribution_version(SimpleNamespace(dist=None)) is None

    monkeypatch.setattr(distribution, "wheel_entry_point", lambda: None)
    assert distribution.distribution_version() is None, "a folder or source-tree run has no wheel version"
