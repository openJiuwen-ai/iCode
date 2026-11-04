# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The wheel build hook (hatch_build.py) that compiles the gitignored zh-Hans MO."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from babel.messages.pofile import PoFileError
from scripts import i18n_mo

from tests.foundation.i18n._catalog_helpers import _prepare_translated_catalog

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFLICT_MARKER = "<<<<<<< ours\n"


class _App:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def display_warning(self, message: str) -> None:
        self.warnings.append(message)


class _BuildHookInterface:
    """The members of hatchling's ``BuildHookInterface`` the hook reads (its ``root`` is a str)."""

    def __init__(self, root: Path, app: _App) -> None:
        self.root = str(root)
        self.app = app


@pytest.fixture
def hook_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # hatchling exists only in the isolated build environment; wheel builds run
    # the real one in tests/integration/test_distribution_artifacts.py.
    interface = ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = _BuildHookInterface
    for name in ("hatchling", "hatchling.builders", "hatchling.builders.hooks", "hatchling.builders.hooks.plugin"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, interface.__name__, interface)
    spec = importlib.util.spec_from_file_location("_hatch_build_under_test", REPO_ROOT / "hatch_build.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("hatch_build.py is not loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(tmp_path: Path) -> tuple[Path, bytes]:
    """A tree the hook builds from: a small translated PO and the real compiler."""
    _source_root, _pot_path, po_path, mo_path = _prepare_translated_catalog(tmp_path)
    if po_path != tmp_path / i18n_mo.PO_PATH:
        raise AssertionError(f"the catalog helper wrote the PO at {po_path}, not the build's PO path")
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "i18n_mo.py", tmp_path / "scripts" / "i18n_mo.py")
    return tmp_path, mo_path.read_bytes()


def _break_po(root: Path) -> None:
    po_path = root / i18n_mo.PO_PATH
    po_path.write_text(po_path.read_text(encoding="utf-8") + CONFLICT_MARKER, encoding="utf-8")


def test_editable_build_writes_the_copy_the_editable_install_reads(tmp_path: Path, hook_module: ModuleType) -> None:
    root, compiled = _project(tmp_path)
    app = _App()
    build_data: dict[str, Any] = {"force_include": {}}

    hook_module.CatalogBuildHook(root, app).initialize("editable", build_data)

    assert (root / i18n_mo.SOURCE_MO_PATH).read_bytes() == compiled
    assert build_data == {"force_include": {}}
    assert app.warnings == []


def test_editable_build_keeps_the_previous_catalog_when_the_po_cannot_compile(
    tmp_path: Path, hook_module: ModuleType
) -> None:
    # uv run rebuilds the editable install before any command starts, so a PO
    # mid-merge must not fail that build and lock out the commands that fix it.
    root, _compiled = _project(tmp_path)
    source_mo = root / i18n_mo.SOURCE_MO_PATH
    source_mo.parent.mkdir(parents=True)
    source_mo.write_bytes(b"previous catalog")
    _break_po(root)
    app = _App()

    hook_module.CatalogBuildHook(root, app).initialize("editable", {"force_include": {}})

    assert source_mo.read_bytes() == b"previous catalog"
    assert len(app.warnings) == 1
    assert "may be stale" in app.warnings[0]


def test_wheel_build_force_includes_a_scratch_copy_and_removes_it(tmp_path: Path, hook_module: ModuleType) -> None:
    root, compiled = _project(tmp_path)
    hook = hook_module.CatalogBuildHook(root, _App())
    build_data: dict[str, Any] = {"force_include": {}}

    hook.initialize("standard", build_data)

    [(source, target)] = build_data["force_include"].items()
    assert target == i18n_mo.PACKAGE_MO_PATH
    assert Path(source).read_bytes() == compiled
    assert not (root / i18n_mo.SOURCE_MO_PATH).exists()
    hook.finalize("standard", build_data, str(tmp_path / "artifact.whl"))
    assert not Path(source).parent.exists()


def test_wheel_build_fails_rather_than_ship_without_translations(
    tmp_path: Path, hook_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _compiled = _project(tmp_path)
    _break_po(root)
    scratch = tmp_path / "scratch"
    shadow_tempfile = ModuleType("tempfile")

    def mkdtemp(prefix: str) -> str:
        scratch.mkdir()
        return str(scratch)

    shadow_tempfile.mkdtemp = mkdtemp
    monkeypatch.setattr(hook_module, "tempfile", shadow_tempfile)
    build_data: dict[str, Any] = {"force_include": {}}

    with pytest.raises(PoFileError):
        hook_module.CatalogBuildHook(root, _App()).initialize("standard", build_data)

    assert build_data == {"force_include": {}}
    assert not scratch.exists()
