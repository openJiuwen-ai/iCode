# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned skill data model.

Plain dataclasses populated by chrys's own loader
(:mod:`chrys.service.skills.loader`). Owning the model keeps skill internals
(scripts / resources / content) on stable public fields instead of fragile
private-attribute reads.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from chrys.foundation.text.encoding import decode_bytes


def _read_resource_text(path: Path) -> str:
    """Keep disk IO and encoding detection off the event loop."""
    return decode_bytes(path.read_bytes(), errors="strict").replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class SkillResource:
    """Supplementary content attached to a skill.

    Exactly one of ``full_path`` (file-backed, read from disk on demand) or
    ``content`` (inline, served directly) is expected to be set.
    """

    name: str
    description: str | None = None
    full_path: str | None = None
    content: str | None = None

    async def read(self) -> str:
        """Return the resource content, reading file-backed resources from disk."""
        if self.content is not None:
            return self.content
        if not self.full_path:
            raise ValueError(f"Resource '{self.name}' has neither inline content nor a file path.")
        path = Path(self.full_path)
        if not await asyncio.to_thread(path.is_file):
            raise ValueError(f"Resource file '{self.name}' not found at '{self.full_path}'.")
        return await asyncio.to_thread(_read_resource_text, path)


@dataclass
class SkillScript:
    """An executable script file attached to a file-based skill."""

    name: str
    """Relative forward-slash path within the skill directory (e.g. ``scripts/convert.py``)."""

    full_path: str
    """Absolute path to the script file."""

    description: str | None = None


@dataclass
class Skill:
    """A loaded skill — file-based (``path`` set) or inline from profile YAML.

    ``content`` holds the full ``load_skill`` body: the raw SKILL.md text
    (including frontmatter) for file-based skills, or the synthesized XML
    document for inline skills.
    """

    name: str
    description: str
    content: str
    path: str | None = None
    """Absolute skill directory for file-based skills; ``None`` for inline skills."""

    resources: list[SkillResource] = field(default_factory=list)
    scripts: list[SkillScript] = field(default_factory=list)

    revision: str = ""
    """Cached catalog revision (12-hex content hash) for this staged Skill.

    Computed and stamped by the skills provider at catalog staging; empty
    until then. One stable value per staged Skill: the catalog block, the
    ``load_skill`` metadata suffix, and persisted call provenance all read
    this cache so a ``run_skill_script`` that mutates its own skill files
    cannot shift the model-visible or persisted revision mid-staging.
    """

    # Optional Agent Skills specification frontmatter fields, parsed and kept
    # for forward compatibility (not currently consumed beyond validation).
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: str | None = None
    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class SkillLoadFailure:
    """A directory containing a ``SKILL.md`` that failed to produce a skill."""

    skill_dir: str
    reason: str


@dataclass(frozen=True)
class SkillProviderWarning:
    """Non-fatal skills-stack warning ready to publish as a TUI warning event."""

    code: str
    message: str


@runtime_checkable
class SkillScriptRunner(Protocol):
    """Protocol for chrys skill script runners.

    Any callable (sync or async) matching this signature satisfies the
    protocol; :class:`chrys.service.skills.runner.SubprocessScriptRunner` is the
    default implementation.
    """

    def __call__(
        self,
        skill: Skill,
        script: SkillScript,
        args: dict[str, Any] | list[str] | None = None,
        arguments: list[str] | None = None,
        cwd: str | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Run *script* belonging to *skill* and return its output."""
        ...
