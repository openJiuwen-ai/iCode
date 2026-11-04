# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a chat draft belongs to when a prompt shortcut expanded into it.

The input bar keeps it with the draft's text and fields (``ChatDraft``), and
the prompt-shortcut card reads it from there: one draft, one owner.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ShortcutIntent:
    """What an expanded prompt shortcut declares for its draft, kept whatever the session uses now.

    Keeping the declaration, showing it and switching for it are separate: the
    card shows only what differs from the session now, and a send compares
    again when it switches. A shortcut naming the agent already in use keeps
    that name, so a send that changes the agent meanwhile does not lose it.
    How many ``!`command``` placeholders the draft holds is read from its text.
    """

    name: str
    agent: str
    """The frontmatter ``agent``, as written; empty when it names none."""
    model: str
    """The frontmatter ``model``, as written; empty when it names none."""
    switch_dropped: bool = False
    """✕, or the user's own agent or model pick: the session keeps its agent and model."""
    path: str = ""
    """The shortcut file the draft was expanded from (``PromptShortcut.path``): the engine checks it still loads it."""


__all__ = ["ShortcutIntent"]
