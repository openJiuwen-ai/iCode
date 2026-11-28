# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SessionEnvironment — immutable environment snapshot for a Chrys session.

Captured when an agent is built and rebuilt after workspace/profile changes.
Passed to tools and context providers so session-scoped code uses one explicit
environment snapshot instead of ad-hoc process globals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from chrys.foundation.models.workspace import control_chars_as_spaces
from chrys.foundation.platform import PlatformInfo, get_platform

if TYPE_CHECKING:
    from chrys.foundation.models.workspace import WorkingDir, Workspace


@dataclass(frozen=True)
class SessionEnvironment:
    """Immutable snapshot of the current session environment.

    Passed to instance tools (e.g. ShellTools) and context providers so they
    can reference the workspace cwd, platform info, and other session-scoped
    state without relying on module-level globals.
    """

    # Working directory (primary cwd from workspace, or launch dir)
    cwd: str

    # Platform detection result
    platform: PlatformInfo

    # Session metadata
    session_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    # Filtered environment snapshot (safe subset)
    env: dict[str, str] = field(default_factory=dict)

    # Current time at capture
    local_time: str = ""  # e.g. "2026-03-18 20:30:45 PDT"
    utc_time: str = ""  # e.g. "2026-03-19 03:30:45 UTC"

    # Working directories from Workspace
    working_dirs: tuple[WorkingDir, ...] = ()  # tuple for frozen dataclass
    workspace_name: str = ""
    workspace_folders: tuple[str, ...] = ()
    """A registered workspace's reference folders; empty for Unspecified and workflow nodes."""
    workspace_index: tuple[str, ...] = ()
    """The workspace section's lines, computed at build time from the loaded memory files."""
    missing_working_dirs: tuple[str, ...] = ()
    """Unavailable registry paths for list_workspace_dirs."""

    @classmethod
    def capture(
        cls,
        session_id: str = "",
        workspace: Workspace | None = None,
        *,
        workspace_folders: tuple[str, ...] = (),
        workspace_index: tuple[str, ...] = (),
        missing_working_dirs: tuple[str, ...] = (),
    ) -> SessionEnvironment:
        """Capture current environment into an immutable context.

        If *workspace* is provided, uses its cwd and working dirs.
        Otherwise falls back to ``os.getcwd()``.
        The workspace plan supplies the effective registered skill roots.
        """
        if workspace is not None:
            cwd = workspace.primary_cwd
            working_dirs = tuple(workspace.working_dirs)
        else:
            cwd = os.getcwd()
            working_dirs = ()

        now_local = datetime.now().astimezone()
        now_utc = datetime.now(tz=UTC)

        return cls(
            cwd=cwd,
            platform=get_platform(),
            session_id=session_id,
            local_time=now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
            utc_time=now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            env=_safe_env_snapshot(),
            working_dirs=working_dirs,
            workspace_name=control_chars_as_spaces(workspace.name) if workspace is not None else "",
            workspace_folders=workspace_folders,
            workspace_index=workspace_index,
            missing_working_dirs=missing_working_dirs,
        )


def _safe_env_snapshot() -> dict[str, str]:
    """Capture a filtered subset of environment variables (no secrets)."""
    safe_keys = {"HOME", "USER", "SHELL", "LANG", "PATH", "TERM", "EDITOR"}
    return {k: v for k, v in os.environ.items() if k in safe_keys}
