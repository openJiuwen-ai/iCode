# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The exported folder package: self-contained, host-loadable, honest about backends (W06)."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from icode_browser_use import __version__

ROOT = Path(__file__).resolve().parents[1]
BUILDER = runpy.run_path(str(ROOT / "scripts" / "export_extension_pack.py"))


def _build(tmp_path: Path) -> Path:
    return BUILDER["build"](tmp_path / "out", ROOT / "src" / "icode_browser_use")


def _load(pack: Path, monkeypatch) -> object:
    """Load the pack the way iCode's extension loader does: a package by file location."""
    name = "_chrys_extension_browser"
    spec = importlib.util.spec_from_file_location(name, pack / "__init__.py", submodule_search_locations=[str(pack)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    for loaded in list(sys.modules):
        if loaded.startswith(f"{name}."):
            monkeypatch.delitem(sys.modules, loaded, raising=False)
    return module


def test_export_layout_is_self_contained(tmp_path):
    pack = _build(tmp_path)

    for relative in (
        "__init__.py",
        "doctor.py",
        "README.md",
        "icode_browser_use/__init__.py",
        "icode_browser_use/extension.py",
        "icode_browser_use/extension/manifest.json",
        "icode_browser_use/extension/background.js",
        "icode_browser_use/extension/chunks.js",
        "icode_browser_use/extension/permissions.js",
    ):
        assert (pack / relative).is_file(), relative
    assert not list(pack.rglob("__pycache__"))
    assert not list(pack.rglob("*.test.js"))
    assert not list(pack.rglob("*.pyc"))
    assert not list(pack.rglob(".ruff_cache"))

    init = (pack / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{__version__}"' in init
    assert "from .icode_browser_use.extension import extension" in init


def test_exported_pack_installs_and_uninstalls_through_its_own_command(tmp_path, monkeypatch):
    """The pack's own CLI copies the *built* folder where the host reads it, and removes only that."""
    pytest.importorskip("chrys.extensions")
    pack = _build(tmp_path)
    _load(pack, monkeypatch)  # registers the package so its submodules import
    cli = importlib.import_module("_chrys_extension_browser.icode_browser_use.cli")
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **_: ())
    config = tmp_path / "config"
    state = tmp_path / "state"
    (state / "chrome").mkdir(parents=True)

    assert cli.main(["install", "--source", str(pack), "--config-dir", str(config)]) == 0
    installed = config / "extensions" / "browser"
    assert (installed / "doctor.py").is_file()
    assert (installed / "icode_browser_use" / "extension" / "manifest.json").is_file()

    assert cli.main(["uninstall", "--config-dir", str(config)]) == 0
    assert not installed.exists()
    assert (state / "chrome").is_dir(), "uninstall must keep profiles and login data"


def test_exported_pack_loads_and_declares_the_browser_tools(tmp_path, monkeypatch):
    pytest.importorskip("chrys.extensions")
    pack = _build(tmp_path)
    module = _load(pack, monkeypatch)

    from chrys.service.extensions import bind_extension

    extension = module.extension
    build = SimpleNamespace(
        runtime=SimpleNamespace(cwd=tmp_path, working_dirs=()),
        agent_name="pack-test",
        caller_name=None,
        session_id="pack-test",
        session_dir=tmp_path / "session",
    )
    names = {tool.name for tool in bind_extension("browser", extension, build).tools}

    assert len(names) == 31
    assert {
        "browser_open",
        "browser_navigate",
        "browser_tabs",
        "browser_tab_switch",
        "browser_click",
        "browser_drag",
        "browser_network_body",
        "browser_snapshot",
        "browser_press_key",
        "browser_select_option",
        "browser_wait_for",
        "browser_dialog",
        "browser_downloads",
        "browser_download_cancel",
    } <= names


def test_exported_pack_reports_its_folder_origin_and_unavailable_backends(tmp_path):
    pytest.importorskip("chrys.extensions")
    pack = _build(tmp_path)
    config = tmp_path / "config"
    chrome = tmp_path / "chrome"
    chrome.write_bytes(b"")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("ICODE_BROWSER_")}
    env.update(
        {
            "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
            "ICODE_BROWSER_STATE_DIR": str(tmp_path / "state"),
            "ICODE_BROWSER_EXECUTABLE": str(chrome),
            "PYTHONPATH": "",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-I", str(pack / "doctor.py"), "--json", "--config-dir", str(config)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["plugin"]["install"] == "folder"
    assert document["plugin"]["module"] == "_chrys_extension_browser.icode_browser_use.diagnose"
    assert document["plugin"]["import"] == "ok"
    assert document["host"]["extension_api"] == "ok"
    assert document["config"]["ok"] is True
    assert document["broker"]["launchable"] is False
    assert "broker_not_running" in [problem["code"] for problem in document["problems"]]


def test_export_refuses_an_absolute_package_import(tmp_path):
    source = tmp_path / "src" / "icode_browser_use"
    (source / "extension").mkdir(parents=True)
    (source / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (source / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
    (source / "extension" / "background.js").write_text("", encoding="utf-8")
    (source / "bad.py").write_text("from icode_browser_use.config import BrowserConfig\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="import icode_browser_use absolutely"):
        BUILDER["build"](tmp_path / "out", source)
