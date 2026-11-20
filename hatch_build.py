# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wheel build hook: compile the zh-Hans catalog and vendor the sandbox engine.

The compiled ``chrys.mo`` is generated, never tracked (``scripts/i18n_mo.py``
says why). A standard wheel build compiles the PO into a scratch file and
force-includes it at the package path the runtime reads, so the build fails
rather than ship a wheel without translations. An editable build (``uv sync``)
writes the gitignored copy under ``src/`` that the editable install reads; a PO
it cannot compile, such as one with merge-conflict markers, keeps the previous
copy and warns, so ``uv run`` can still start the commands that repair the PO.

The same standard wheel force-includes ``third_party/jiuwenbox/src/jiuwenbox``
as a top-level ``jiuwenbox`` package, ``configs/*.yaml`` included. That tree
stays the only copy. An editable build does not copy it into site-packages,
where it would be found ahead of the source tree, and instead adds the source
directory to the import path.

``[tool.uv].cache-keys`` lists the PO, this hook and the compiler, so ``uv run``
rebuilds the editable install, and with it the catalog, whenever one changes.
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Source tree of the vendored sandbox engine, and where a standard wheel ships it.
_ENGINE_PACKAGE = Path("third_party") / "jiuwenbox" / "src" / "jiuwenbox"
_ENGINE_DISTRIBUTION_PATH = "jiuwenbox"
# Editable installs add this site-packages path file instead of copying the tree.
_ENGINE_PATH_NAME = "jiuwenbox-vendor.pth"


class CatalogBuildHook(BuildHookInterface):
    """Compile ``locales/zh-Hans/LC_MESSAGES/chrys.po`` and vendor ``jiuwenbox``."""

    PLUGIN_NAME = "custom"

    _scratch: Path | None = None
    _engine_scratch: Path | None = None

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        if version == "editable":
            try:
                compiler = _load_compiler(root)
                compiler.compile_mo(root / compiler.PO_PATH, root / compiler.SOURCE_MO_PATH)
            except Exception as error:  # an editable install must never fail over its catalog
                self.app.display_warning(f"The zh-Hans catalog was not rebuilt and may be stale: {error}")
            self._include_editable_engine(root, build_data)
            return
        compiler = _load_compiler(root)
        scratch = Path(tempfile.mkdtemp(prefix="chrys-catalog-"))
        mo_path = scratch / Path(compiler.PACKAGE_MO_PATH).name
        try:
            compiler.compile_mo(root / compiler.PO_PATH, mo_path)
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        self._scratch = scratch
        build_data["force_include"][str(mo_path)] = compiler.PACKAGE_MO_PATH
        _include_vendored_engine(root, build_data)

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None
        if self._engine_scratch is not None:
            shutil.rmtree(self._engine_scratch, ignore_errors=True)
            self._engine_scratch = None

    def _include_editable_engine(self, root: Path, build_data: dict[str, Any]) -> None:
        """Point the editable install at the live vendored tree."""
        package = _engine_package(root)
        scratch = Path(tempfile.mkdtemp(prefix="chrys-jiuwenbox-"))
        path_file = scratch / _ENGINE_PATH_NAME
        try:
            # site.py adds each existing directory line of a site-packages .pth.
            path_file.write_text(f"{package.parent}\n", encoding="utf-8")
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
        self._engine_scratch = scratch
        build_data["force_include"][str(path_file)] = _ENGINE_PATH_NAME


def _engine_package(root: Path) -> Path:
    package = root / _ENGINE_PACKAGE
    if not (package / "__init__.py").is_file():
        raise RuntimeError(f"Vendored jiuwenbox package is missing: {package}")
    return package


def _include_vendored_engine(root: Path, build_data: dict[str, Any]) -> None:
    build_data["force_include"][str(_engine_package(root))] = _ENGINE_DISTRIBUTION_PATH


def _load_compiler(root: Path) -> ModuleType:
    # The build environment has neither the project nor its root on sys.path.
    path = root / "scripts" / "i18n_mo.py"
    spec = importlib.util.spec_from_file_location("_chrys_build_i18n_mo", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load the catalog compiler from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
