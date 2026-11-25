# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace plans are constructed by the planner and installed by the session."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from chrys.foundation.models.workspace import Workspace
from tests.support.components import make_session

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "chrys"
_ISSUER = _SOURCE / "orchestration" / "engine" / "workspaces.py"


def test_only_planner_constructs_workspace_plans() -> None:
    violations = []
    for path in _SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        plan_types = {"WorkspacePlan", "_WorkspacePlan"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "chrys.orchestration.engine.workspaces":
                plan_types.update(alias.asname for alias in node.names if alias.name in plan_types and alias.asname)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name not in plan_types:
                continue
            owner = parents.get(node)
            while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents.get(owner)
            planner = parents.get(owner) if owner is not None else None
            if (
                path != _ISSUER
                or owner is None
                or owner.name != "plan"
                or not isinstance(planner, ast.ClassDef)
                or planner.name != "SessionWorkspaces"
            ):
                violations.append(f"{path.relative_to(_SOURCE)}:{node.lineno}: unauthorized plan construction")
    assert violations == []


def test_raw_workspace_cannot_replace_the_installed_plan(tmp_path: Path) -> None:
    workspace = Workspace.from_cwd(str(tmp_path))
    session = make_session(workspace=workspace)
    installed = session.workspace_plan
    with pytest.raises(TypeError, match="WorkspacePlan"):
        session.install_workspace(workspace)
    assert session.workspace_plan is installed
