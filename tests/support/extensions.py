# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Install Python extensions for one test, the two ways users install them.

A file extension goes in the test's own ``<config_dir>/extensions``; a
distribution extension is a module plus a ``.dist-info`` with a
``chrys.extensions`` entry point on a temporary ``sys.path`` entry. Each install
forgets the process's earlier extension imports and removes the module it causes
at teardown, so tests never share one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.service import extensions

if TYPE_CHECKING:
    import pytest

EXAMPLE_EXTENSION = Path(__file__).with_name("acme_release_extension.py")
"""The example extension, which imports nothing from iCode but ``chrys.extensions``."""
EXAMPLE_EXTENSION_ID = "acme_release"


def forget_extension_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this test import every extension afresh."""
    monkeypatch.setattr(extensions, "_imported", {})


def _remove_module_at_teardown(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    # Undone in reverse: the module the test imported becomes None, then goes.
    monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, name)


def install_file_extension(
    monkeypatch: pytest.MonkeyPatch,
    extension_id: str = EXAMPLE_EXTENSION_ID,
    source: str | None = None,
    *,
    package: bool = False,
) -> Path:
    """Write an extension to ``<config_dir>/extensions/<id>.py`` (or ``<id>/__init__.py``); *source* defaults to the example."""
    forget_extension_imports(monkeypatch)
    extensions_dir = extensions.user_extensions_dir()
    target = extensions_dir / extension_id / "__init__.py" if package else extensions_dir / f"{extension_id}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source if source is not None else EXAMPLE_EXTENSION.read_text(encoding="utf-8"), encoding="utf-8")
    _remove_module_at_teardown(monkeypatch, f"_chrys_extension_{extension_id}")
    return target


def install_distribution_extension(
    monkeypatch: pytest.MonkeyPatch,
    site: Path,
    extension_id: str = EXAMPLE_EXTENSION_ID,
    source: str | None = None,
    *,
    distribution: str = "acme-icode-tools",
    version: str = "1.0",
    summary: str = "Acme tools for iCode",
    module: str = "acme_icode_tools",
) -> None:
    """Install a distribution under *site* whose ``chrys.extensions`` entry point *extension_id* names ``<module>:extension``."""
    forget_extension_imports(monkeypatch)
    site.mkdir(parents=True, exist_ok=True)
    text = source if source is not None else EXAMPLE_EXTENSION.read_text(encoding="utf-8")
    (site / f"{module}.py").write_text(text, encoding="utf-8")
    info = site / f"{distribution.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\nSummary: {summary}\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(
        f"[chrys.extensions]\n{extension_id} = {module}:extension\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(site))
    _remove_module_at_teardown(monkeypatch, module)
