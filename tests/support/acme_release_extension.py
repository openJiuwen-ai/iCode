# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Release metadata for Acme projects.

An example Python extension, written as an extension author would: it imports
nothing from iCode but ``chrys.extensions``. Tests install it both as a file
(copied to ``<config_dir>/extensions/acme_release.py``) and through a
distribution's entry point, and its authors' own tests call its tools directly.
"""

from __future__ import annotations

import json
import tomllib
from typing import Annotated, TypedDict

from chrys.extensions import Extension, ToolContext, ToolError

extension = Extension()


class ReleaseNote(TypedDict):
    """One line of release notes."""

    title: str
    tags: list[str]


@extension.tool(requires_approval=False)
async def acme_project_name(
    ctx: ToolContext,
    path: Annotated[str, "Path to pyproject.toml, relative to the working directory"] = "pyproject.toml",
) -> str:
    """Read the project's distribution name."""
    try:
        data = tomllib.loads((ctx.cwd / path).read_text(encoding="utf-8"))
        return data["project"]["name"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise ToolError("Missing or invalid project name.", code="invalid_project") from exc


@extension.tool
async def acme_add_release_note(
    ctx: ToolContext,
    note: Annotated[ReleaseNote, "The note to add to this session's release notes"],
) -> dict[str, object]:
    """Add a note to this session's release notes and say how many there are."""
    folder = ctx.session_data_dir or ctx.data_dir
    notes_file = folder / "notes.json"
    notes = json.loads(notes_file.read_text(encoding="utf-8")) if notes_file.exists() else []
    notes.append(note)
    notes_file.write_text(json.dumps(notes), encoding="utf-8")
    return {"notes": len(notes), "last": note, "agent": ctx.agent_name}
