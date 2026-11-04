# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wheel build hook: compile the zh-Hans catalog into every build.

The compiled ``chrys.mo`` is generated, never tracked (``scripts/i18n_mo.py``
says why). A standard wheel build compiles the PO into a scratch file and
force-includes it at the package path the runtime reads, so the build fails
rather than ship a wheel without translations. An editable build (``uv sync``)
writes the gitignored copy under ``src/`` that the editable install reads; a PO
it cannot compile, such as one with merge-conflict markers, keeps the previous
copy and warns, so ``uv run`` can still start the commands that repair the PO.

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


class CatalogBuildHook(BuildHookInterface):
    """Compile ``locales/zh-Hans/LC_MESSAGES/chrys.po`` for the wheel target."""

    PLUGIN_NAME = "custom"

    _scratch: Path | None = None

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        if version == "editable":
            try:
                compiler = _load_compiler(root)
                compiler.compile_mo(root / compiler.PO_PATH, root / compiler.SOURCE_MO_PATH)
            except Exception as error:  # an editable install must never fail over its catalog
                self.app.display_warning(f"The zh-Hans catalog was not rebuilt and may be stale: {error}")
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

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        if self._scratch is not None:
            shutil.rmtree(self._scratch, ignore_errors=True)
            self._scratch = None


def _load_compiler(root: Path) -> ModuleType:
    # The build environment has neither the project nor its root on sys.path.
    path = root / "scripts" / "i18n_mo.py"
    spec = importlib.util.spec_from_file_location("_chrys_build_i18n_mo", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load the catalog compiler from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
