# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Order workspace choices by working-directory match, then recent use."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.service.workspaces.paths import contains

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from chrys.foundation.models.workspace import Workspace
    from chrys.service.workspaces.registry import RegistrySnapshot, WorkspaceRegistry


def sorted_workspaces(
    workspaces: Iterable[Workspace],
    cwd: str,
    last_used_at: Mapping[str, str],
) -> list[Workspace]:
    def order(workspace: Workspace) -> tuple[bool, str]:
        folders = (workspace.primary_cwd, *(directory.path for directory in workspace.working_dirs))
        return any(contains(folder, cwd) for folder in folders), last_used_at.get(workspace.id, "")

    return sorted(workspaces, key=order, reverse=True)


@dataclass(frozen=True, slots=True)
class WorkspaceOrder:
    """One background read and its physical-path ordering, scoped to the active chat."""

    snapshot: RegistrySnapshot
    cwd: str
    current_id: str | None
    workspaces: tuple[Workspace, ...]

    def matches(self, snapshot: RegistrySnapshot, cwd: str, current_id: str | None) -> bool:
        return self.snapshot is snapshot and self.cwd == cwd and self.current_id == current_id


def read_workspace_order(
    registry: WorkspaceRegistry, cwd: str, current_id: str | None, cached: WorkspaceOrder | None = None
) -> WorkspaceOrder:
    """Read and order together in the caller's background thread."""
    snapshot = registry.snapshot()
    if cached is not None and cached.matches(snapshot, cwd, current_id):
        return cached
    return WorkspaceOrder(
        snapshot, cwd, current_id, tuple(sorted_workspaces(snapshot.workspaces, cwd, snapshot.last_used_at))
    )
