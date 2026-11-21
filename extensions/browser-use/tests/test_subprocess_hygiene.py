# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "icode_browser_use"


class _StdinRequirementAudit(ast.NodeVisitor):
    """Collect ``subprocess.run`` / ``subprocess.Popen`` call sites missing explicit ``stdin``."""

    _SPAWN_FUNCS = frozenset({"run", "Popen"})

    def __init__(self, rel_path: Path) -> None:
        self._rel_path = rel_path
        self.violations: list[str] = []

    def visit_Call(self, call: ast.Call) -> None:
        callee = call.func
        is_subprocess_spawn = (
            isinstance(callee, ast.Attribute)
            and callee.attr in self._SPAWN_FUNCS
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "subprocess"
        )
        if is_subprocess_spawn and "stdin" not in {kw.arg for kw in call.keywords}:
            self.violations.append(f"{self._rel_path}:{call.lineno}")
        self.generic_visit(call)


def test_every_subprocess_start_sets_stdin() -> None:
    """A child that inherits iCode's stdin can eat the ACP JSON-RPC pipe."""
    violations: list[str] = []
    for source_file in sorted(SRC.rglob("*.py")):
        module_ast = ast.parse(source_file.read_text(encoding="utf-8"))
        audit = _StdinRequirementAudit(source_file.relative_to(SRC))
        audit.visit(module_ast)
        violations.extend(audit.violations)
    assert violations == []
