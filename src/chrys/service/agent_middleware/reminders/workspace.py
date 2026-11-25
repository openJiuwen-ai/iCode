# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The workspace section of the runtime reminder: folder index, instruction paths, departure line.

It is computed once per build from the memory files this build loaded, so the
reminder carries the same text for the same workspace and files — a workspace's
folders are runtime state, never part of the system prompt.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.models.workspace import path_identity
from chrys.foundation.platform.paths import is_absolute_path
from chrys.service.context.memory_loader import memory_file_path

if TYPE_CHECKING:
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.context.memory_loader import MemoryContent
    from chrys.service.profiles.agents.schema import MemoryConfig


def workspace_index_lines(
    workspace: Workspace,
    memory: MemoryConfig,
    loaded: MemoryContent,
) -> tuple[str, ...]:
    """Index the workspace's folders and their not-yet-loaded instruction files.

    A relative memory file name is looked up in each available workspace
    folder; a file identity the build already loaded whole (aliases included)
    is not listed again. Absolute names and memory folders load as before and
    are not indexed. With no reference files configured only the folders list.
    """
    cwd = workspace.primary_cwd
    folders = [cwd] if os.path.isdir(cwd) else []
    folders.extend(directory.path for directory in workspace.working_dirs if directory.exists)
    if not folders:
        return ()
    seen = {path_identity(os.path.join(cwd, path)) for path in loaded.loaded_files}
    relative_names = [name for name in memory.files if not is_absolute_path(os.path.expanduser(name.strip()))]
    lines = ["Workspace folders:"]
    for folder in folders:
        lines.append(f"- {folder}" + (" (primary)" if folder == cwd else ""))
        try:
            base = Path(folder).resolve()
        except OSError:
            continue
        references: list[str] = []
        for name in relative_names:
            path = memory_file_path(name, base)
            if path is None:
                continue
            identity = path_identity(str(path))
            if identity not in seen:
                seen.add(identity)
                # The physical path: tools may fold `..` in the folder's spelling before symlinks.
                references.append(str(path))
        if references:
            lines.append("  Instructions for this folder: " + ", ".join(references))
            lines.append("  Read them before working in this folder; they apply only inside it.")
    return tuple(lines)
