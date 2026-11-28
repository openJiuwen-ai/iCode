# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The core meets the file-access sandbox only through ``chrys.foundation.guard``.

The sandbox lives in its own distribution (``packages/icode-sandbox``); iCode
loads it by entry point. These guards keep it that way: no core module names
the package, and the facade itself holds no sandbox logic (paths, platforms,
kernel calls), so with no guard installed the core is what upstream ships.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "chrys"
FACADE = SRC / "foundation" / "guard.py"


def test_no_core_module_names_the_sandbox_package() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 500, "scanned nothing"
    # The facade names the entry point's target to check it, and never imports it.
    naming = [
        str(f.relative_to(SRC)) for f in files if f != FACADE and "icode_sandbox" in f.read_text(encoding="utf-8")
    ]
    assert naming == [], f"core modules import the sandbox directly; go through chrys.foundation.guard: {naming}"
    facade_imports = [
        node.lineno
        for node in ast.walk(ast.parse(FACADE.read_text(encoding="utf-8")))
        if (isinstance(node, ast.ImportFrom) and (node.module or "").startswith("icode_sandbox"))
        or (isinstance(node, ast.Import) and any(a.name.startswith("icode_sandbox") for a in node.names))
    ]
    assert facade_imports == [], f"the facade imports the package at lines {facade_imports}; it loads it by entry point"


def test_the_facade_holds_no_sandbox_logic() -> None:
    tree = ast.parse(FACADE.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")])
    }
    assert not imported & {"ctypes", "fcntl", "socket", "subprocess", "signal", "platform"}, imported
    platform_checks = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "platform" and getattr(node.value, "id", "") == "sys"
    ]
    assert platform_checks == [], f"the facade branches on the platform at lines {platform_checks}"
    text = FACADE.read_text(encoding="utf-8").lower()
    for word in ("seatbelt", "landlock", "sandbox_init", "broker"):
        assert word not in text, f"the facade mentions {word!r}: that belongs in the package"
