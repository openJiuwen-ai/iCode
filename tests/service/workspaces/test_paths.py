# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registered working folders are resolved when the workspace opens."""

from __future__ import annotations

import os
import sys

import pytest

from chrys.foundation.events.types import WorkspaceOperation
from chrys.orchestration.engine.workspaces import SessionWorkspaces
from chrys.service.workspaces import paths
from chrys.service.workspaces.paths import contains
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.symlinks import symlink_or_skip


@pytest.mark.parametrize(("ancestor_alias", "tail"), [("Exist", "gone"), ("exist", "Gone")])
def test_contains_missing_tails_with_case_variants(tmp_path, ancestor_alias, tail):
    ancestor = tmp_path / "Exist"
    ancestor.mkdir()
    candidate_ancestor = tmp_path / ancestor_alias
    if sys.platform in ("darwin", "win32") and not candidate_ancestor.exists():
        pytest.skip("The existing ancestor needs a case-insensitive volume")
    folder = ancestor / "Gone"
    directory = candidate_ancestor / tail / "x"

    assert contains(str(folder), str(directory)) is (sys.platform in ("darwin", "win32"))


def test_contains_only_folds_missing_path_parts(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    present = root / "foo"
    present.mkdir()
    missing = root / "Foo"
    real_identity = paths.path_identity
    realpath = os.path.realpath

    def case_sensitive_identity(path):
        if os.fspath(path) == str(missing):
            return str(missing)
        return real_identity(path)

    def case_sensitive_realpath(path, *, strict=False):
        if os.fspath(path) == str(missing):
            return str(missing)
        return realpath(path, strict=strict)

    monkeypatch.setattr(paths, "path_identity", case_sensitive_identity)
    monkeypatch.setattr(os.path, "realpath", case_sensitive_realpath)

    assert not contains(str(missing), str(present))
    assert not contains(str(missing), str(present / "gone"))


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
