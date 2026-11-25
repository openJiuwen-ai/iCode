# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "icode_browser_use"
SCRIPTS = ROOT / "scripts"


class _StdinRequirementAudit(ast.NodeVisitor):
    """Collect ``subprocess.run`` / ``subprocess.Popen`` call sites missing explicit non-None ``stdin``."""

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
        if is_subprocess_spawn:
            stdin_kw = next((kw for kw in call.keywords if kw.arg == "stdin"), None)
            if stdin_kw is None or (isinstance(stdin_kw.value, ast.Constant) and stdin_kw.value.value is None):
                self.violations.append(f"{self._rel_path}:{call.lineno}")
        self.generic_visit(call)


def test_every_subprocess_start_sets_stdin() -> None:
    """A child that inherits iCode's stdin can eat the ACP JSON-RPC pipe."""
    violations: list[str] = []
    for base in (SRC, SCRIPTS):
        for source_file in sorted(base.rglob("*.py")):
            module_ast = ast.parse(source_file.read_text(encoding="utf-8"))
            audit = _StdinRequirementAudit(source_file.relative_to(ROOT))
            audit.visit(module_ast)
            violations.extend(audit.violations)
    assert violations == []
