# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace data model — session-scoped working directory configuration.

A workspace groups the primary cwd, additional working directories,
and reference files for a single session.  Changes trigger a SessionEnvironment
rebuild but preserve conversation history.
"""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path


def control_chars_as_spaces(text: str) -> str:
    """*text* with each control character (Unicode category Cc) as a space.

    A workspace name is rejected on save when this changes it; a name written
    into the workspace list by hand reaches the model this way.
    """
    return "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)


@dataclass(frozen=True, slots=True)
class WorkspaceDeparture:
    """The workspace a session just left, identified by name in the runtime reminder."""

    name: str


def path_identity(path: str | os.PathLike[str]) -> tuple[int, int] | str:
    """Compare present filesystem objects by identity, with a path fallback."""
    try:
        stat = os.stat(path)
    except OSError:
        pass
    else:
        if stat.st_ino:
            return stat.st_dev, stat.st_ino
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def same_directory(left: str, right: str, *, realpath_fallback: bool = False) -> bool:
    """Compare directory identity, optionally resolving paths when stat fails."""
    if realpath_fallback:
        return left == right or path_identity(left) == path_identity(right)
    try:
        return os.path.samefile(left, right)
    except OSError:
        return left == right


@dataclass(frozen=True, slots=True)
class WorkspaceConfirmation:
    """The source membership and physical destination authorized by one action."""

    source_workspace_id: str | None
    target_cwd: str
    resolved_cwd: str
    identity: tuple[int, int] | str

    @classmethod
    def capture(cls, source_workspace_id: str | None, target_cwd: str) -> WorkspaceConfirmation:
        # Keep `..` for symlink-aware resolution, as the workspace registry does.
        target_cwd = str(Path(target_cwd).absolute())
        return cls(source_workspace_id, target_cwd, os.path.realpath(target_cwd), path_identity(target_cwd))

    def target_is_current(self) -> bool:
        return (
            os.path.isdir(self.target_cwd)
            and os.path.normcase(os.path.realpath(self.target_cwd)) == os.path.normcase(self.resolved_cwd)
            and path_identity(self.target_cwd) == self.identity
        )

    def matches(self, source_workspace_id: str | None, target_cwd: str) -> bool:
        return (
            self.source_workspace_id == source_workspace_id
            and self.target_is_current()
            and path_identity(target_cwd) == self.identity
        )


@dataclass
class WorkingDir:
    """A single working directory within a workspace."""

    path: str  # Absolute path
    label: str = ""  # Optional user label (e.g. "frontend", "api")
    is_primary: bool = False  # Exactly one must be True per workspace

    @property
    def exists(self) -> bool:
        """Check if the directory still exists on disk."""
        return os.path.isdir(self.path)


@dataclass
class Workspace:
    """Workspace — working directory configuration for a session.

    Groups the primary cwd, additional working directories, and reference
    files.  Can be updated mid-session (e.g. changing cwd, adding reference
    dirs/files).  Changes trigger a SessionEnvironment rebuild but preserve
    conversation history.
    """

    primary_cwd: str
    working_dirs: list[WorkingDir] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)
    id: str = ""
    name: str = ""

    @classmethod
    def from_cwd(cls, cwd: str | None = None) -> Workspace:
        """Create from a working directory (defaults to os.getcwd())."""
        raw = cwd or os.getcwd()
        return cls(primary_cwd=os.path.abspath(os.path.expanduser(raw)))

    def missing_primary(self) -> str | None:
        """Return the primary cwd when it is no longer a directory, else ``None``.

        New work (turns, retries, restores, workflow runs) refuses to start
        while this returns a path; additional working dirs are optional and
        never block. The raw path is returned for display and filesystem use.
        """
        if self.primary_cwd and not os.path.isdir(self.primary_cwd):
            return self.primary_cwd
        return None


def session_workdirs(workspace: Workspace | None) -> list[WorkingDir]:
    """Return a conversation's own directories, excluding registry folders."""
    return workspace.working_dirs if workspace is not None and not workspace.id else []


def workspace_scope_fingerprint(workspace: Workspace | None) -> str:
    """Bind a provider session to the registered workspace and the roots its build used."""
    if workspace is None or not workspace.id:
        return ""
    scope = (workspace.id, workspace.primary_cwd, [directory.path for directory in workspace.working_dirs])
    return sha256(json.dumps(scope, ensure_ascii=True).encode("ascii")).hexdigest()
