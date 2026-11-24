# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registered working folders are resolved when the workspace opens."""

from __future__ import annotations

from chrys.foundation.events.types import WorkspaceOperation
from chrys.orchestration.engine.workspaces import SessionWorkspaces
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.symlinks import symlink_or_skip


def test_opening_workspace_resolves_primary_and_marks_missing_reference(tmp_path):
    primary, alias, reference = (tmp_path / name for name in ("primary", "alias", "reference"))
    primary.mkdir()
    reference.mkdir()
    symlink_or_skip(alias, primary, target_is_directory=True)
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    selected = registry.create("Team", str(alias), [str(reference)])
    reference.rmdir()

    plan = SessionWorkspaces.plan(
        workspace=selected,
        snapshot=registry.snapshot(),
        operation=WorkspaceOperation.SELECT,
        workspace_id=selected.id,
    )

    assert plan.workspace.primary_cwd == str(primary.resolve())
    assert plan.workspace.working_dirs == []
    assert plan.missing_working_dirs == (str(reference),)
