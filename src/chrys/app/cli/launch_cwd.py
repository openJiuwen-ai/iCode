# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Check that the directory a command was launched from still exists.

A shell can keep a directory as its cwd after it was deleted or moved. Commands
that use the cwd to resolve a workspace or working directory settings check it after
parsing their arguments. A valid absolute ``-C`` path also passes this check.
Otherwise, a missing cwd stops the command with a single error message.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.service.workspaces.paths import MissingWorkspacePrimary
from chrys.service.workspaces.registry import (
    RegistrySnapshot,
    WorkspaceRegistry,
    workspace_not_found,
    workspace_selector_ambiguous,
)

if TYPE_CHECKING:
    from chrys.foundation.models.workspace import Workspace

LAUNCH_CWD_MISSING_CODE = "working_dir_missing"


def launch_cwd_missing_message(*, workdir_flag: bool, workdir: str = "") -> str | None:
    """Return the error line when the process cwd no longer exists, else ``None``.

    *workdir_flag* says whether the command accepts ``-C``, so the hint only
    names options the user can actually pass. *workdir* is the ``-C`` value
    given, if any: with the launch directory gone only an existing absolute
    directory replaces it, because a relative one (``-C .``) resolves against
    the missing directory.
    """
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    if cwd is not None and os.path.isdir(cwd):
        return None
    expanded = os.path.expanduser(workdir) if workdir else ""
    if expanded and os.path.isabs(expanded) and os.path.isdir(expanded):
        return None
    hint = (
        "cd to an existing directory or pass -C with an absolute path."
        if workdir_flag
        else "cd to an existing directory."
    )
    return f"The current directory no longer exists; {hint}"


def resolve_workspace_selector(selector: str) -> tuple[Workspace, RegistrySnapshot]:
    """Resolve a workspace by exact ID, case-insensitive name or ID prefix."""
    if not selector.strip():
        raise workspace_not_found(selector)
    registry = WorkspaceRegistry()
    snapshot = registry.snapshot()
    if snapshot.error:
        raise snapshot.error
    selected = snapshot.workspace(selector)
    if selected is None:
        matches = [
            workspace
            for workspace in snapshot.workspaces
            if workspace.name.casefold() == selector.casefold() or workspace.id.startswith(selector)
        ]
        if len(matches) > 1:
            raise workspace_selector_ambiguous(selector)
        if not matches:
            raise workspace_not_found(selector)
        selected = matches[0]
    return selected, snapshot


def open_launch_workspace(selector: str) -> tuple[Workspace, RegistrySnapshot, str]:
    """Enter the selected folder before loading working directory configuration."""
    selected, snapshot = resolve_workspace_selector(selector)
    try:
        # Windows can keep the link spelling as the cwd. Enter the target so
        # relative paths still resolve there if the registry's link is retargeted.
        os.chdir(Path(selected.primary_cwd).resolve(strict=True))
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise MissingWorkspacePrimary(selected.primary_cwd) from exc
    return selected, snapshot, os.getcwd()
