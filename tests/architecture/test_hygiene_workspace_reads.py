# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reads of workspace folders and the registry never join the default executor, so exit never waits for them.

Reference folders may sit on a mount that stops answering; such reads go through
``chrys.foundation.util.abandonable.run_abandonable``. Registry writes still run to completion.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Reads that can reach workspace folders (reference folders may sit on a hung mount) or the registry.
_WORKSPACE_READS = frozenset(
    {
        "_folder_fits",
        "_load_shortcuts",
        "_discover",
        "_prepare_skill_paths",
        "capture",
        "compute_preview_counts",
        "discover_file_skills",
        "find_missing_tooltip_folders",
        "list_workspaces",
        "load_prompt_shortcuts",
        "manage_workspace",
        "missing_folders",
        "preview_update",
        "prompt_shortcut_sources",
        "select_workspace_for_acp",
        "snapshot",
    }
)


def _callee(node: ast.expr) -> ast.expr:
    """The function a ``partial(...)`` or ``lambda: f(...)`` wraps, else *node*."""
    if isinstance(node, ast.Lambda) and isinstance(node.body, ast.Call):
        return _callee(node.body.func)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        if name == "partial" and node.args:
            return _callee(node.args[0])
    return node


def _joined_reads(tree: ast.Module) -> list[str]:
    """Workspace reads handed to ``asyncio.to_thread``, through local partials and arguments from this module."""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}

    def enclosing(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                return node
        return None

    def names(node: ast.expr, seen: frozenset[int] = frozenset()) -> set[str]:
        node = _callee(node)
        if isinstance(node, ast.Attribute):
            return {node.attr}
        if not isinstance(node, ast.Name) or id(node) in seen:
            return set()
        seen |= {id(node)}
        function = enclosing(node)
        if function is None:
            return {node.id}
        bound = [
            assign.value
            for assign in ast.walk(function)
            if isinstance(assign, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == node.id for target in assign.targets)
        ]
        if bound:
            return set().union(*(names(value, seen) for value in bound))
        nested = [
            inner
            for inner in ast.walk(function)
            if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef) and inner.name == node.id
        ]
        if nested:
            return {
                called.func.attr if isinstance(called.func, ast.Attribute) else called.func.id
                for inner in nested
                for called in ast.walk(inner)
                if isinstance(called, ast.Call) and isinstance(called.func, ast.Name | ast.Attribute)
            }
        parameters = [arg.arg for arg in function.args.args]
        if node.id not in parameters:
            return {node.id}
        index = parameters.index(node.id) - (parameters[:1] == ["self"])
        found: set[str] = set()
        for call in ast.walk(tree):
            if (
                not isinstance(call, ast.Call)
                or functions.get(getattr(call.func, "attr", getattr(call.func, "id", ""))) is not function
            ):
                continue
            given = [kw.value for kw in call.keywords if kw.arg == node.id] or call.args[index : index + 1]
            for value in given:
                found |= names(value, seen)
        return found

    def creates_only(node: ast.AST) -> bool:
        """Whether *node* sits in the body of an ``if <x>.action == "new":``."""
        child = node
        while child in parents:
            branch = parents[child]
            test = branch.test if isinstance(branch, ast.If) else None
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Attribute)
                and test.left.attr == "action"
                and [type(op) for op in test.ops] == [ast.Eq]
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "new"
                and child in branch.body
            ):
                return True
            child = branch
        return False

    joined = []
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "to_thread"
            and call.args
        ):
            for name in names(call.args[0]) & _WORKSPACE_READS:
                if not (name == "manage_workspace" and creates_only(call)):
                    joined.append(f"{call.lineno}: {name}")
    return joined


def test_workspace_reads_never_join_the_default_executor() -> None:
    """Reads of workspace folders and the registry must not hold up exit; only registry writes are joined."""
    src = Path(__file__).resolve().parents[2] / "src" / "chrys"
    joined = {
        path.relative_to(src).as_posix(): found
        for path in sorted(src.rglob("*.py"))
        if (found := _joined_reads(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert joined == {}
