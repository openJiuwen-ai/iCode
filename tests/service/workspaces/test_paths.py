# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Installed plans retain the workspace paths for each consumer to resolve."""

from __future__ import annotations

from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.components import plan_workspace
from tests.support.symlinks import symlink_or_skip


def test_plan_keeps_a_missing_reference_and_workspace_path_spelling(tmp_path):
    primary, alias, reference = (tmp_path / name for name in ("primary", "alias", "reference"))
    primary.mkdir()
    reference.mkdir()
    symlink_or_skip(alias, primary, target_is_directory=True)
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    selected = registry.create("Team", str(alias), [str(reference)])
    reference.rmdir()

    plan = plan_workspace(registry.snapshot().workspace(selected.id))

    assert plan.workspace.primary_cwd == str(alias)
    assert [directory.path for directory in plan.workspace.working_dirs] == [str(reference)]
