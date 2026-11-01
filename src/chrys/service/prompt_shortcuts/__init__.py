# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt shortcuts: reusable prompts stored as Markdown files.

A prompt shortcut is a ``shortcuts/**/*.md`` file under the user's config dir
or a ``.chrys`` dir from the workspace up to its Git repository's top, in the
format opencode and Claude Code use for custom commands. Frontends list them
after ``/`` and expand the chosen one into the draft for the user to edit
before sending.
"""

from __future__ import annotations

from chrys.service.prompt_shortcuts.loader import (
    SHORTCUTS_DIR_NAME,
    PromptShortcut,
    global_shortcuts_dir,
    load_prompt_shortcuts,
    project_roots,
    project_shortcuts_dir,
    prompt_shortcut_folders,
    prompt_shortcut_sources,
    working_folder_dirs,
)
from chrys.service.prompt_shortcuts.template import PromptExpansion, expand_template

__all__ = [
    "SHORTCUTS_DIR_NAME",
    "PromptExpansion",
    "PromptShortcut",
    "expand_template",
    "global_shortcuts_dir",
    "load_prompt_shortcuts",
    "project_roots",
    "project_shortcuts_dir",
    "prompt_shortcut_folders",
    "prompt_shortcut_sources",
    "working_folder_dirs",
]
