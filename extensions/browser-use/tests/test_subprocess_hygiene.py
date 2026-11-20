# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "icode_browser_use"


def test_every_subprocess_start_sets_stdin() -> None:
    """A child that inherits iCode's stdin can eat the ACP JSON-RPC pipe."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in {"run", "Popen"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                continue
            if not any(keyword.arg == "stdin" for keyword in node.keywords):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
