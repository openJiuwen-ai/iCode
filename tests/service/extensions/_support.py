# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One agent build to bind extensions into, for the extension tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.foundation.config.settings import Settings
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.service.profiles.agents.schema import ToolsConfig
from chrys.service.tools.packs import ToolBuild

if TYPE_CHECKING:
    from pathlib import Path


def extension_build(
    workspace: Path,
    *,
    builtins: list[str] | None = None,
    extra_dirs: tuple[Path, ...] = (),
    session_dir: Path | None = None,
    session_id: str | None = "s-1",
) -> ToolBuild:
    """A build for agent ``Coder`` in *workspace* (with only sleep of iCode's tools unless *builtins* lists more)."""
    folders = Workspace(
        primary_cwd=str(workspace),
        working_dirs=[WorkingDir(path=str(workspace), is_primary=True), *(WorkingDir(path=str(d)) for d in extra_dirs)],
    )
    return ToolBuild(
        tools=ToolsConfig(builtins=builtins or []),
        agent_name="Coder",
        runtime=SessionEnvironment.capture(session_id=session_id or "", workspace=folders),
        settings=Settings(),
        session_id=session_id,
        session_dir=session_dir,
    )
