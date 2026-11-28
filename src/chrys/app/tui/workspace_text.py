# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared workspace labels; user-owned names and paths are always rendered as text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual.content import Content

from chrys.app.tui.i18n import render_str
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.service.workspaces.paths import missing_folders
from chrys.service.workspaces.registry import UNSPECIFIED as UNSPECIFIED

if TYPE_CHECKING:
    from chrys.foundation.i18n import Localizer, MessageRef

WORKSPACE = msg("tui.workspaces.workspace", fallback="Workspace")
WORKSPACES = msg("tui.workspaces.title", fallback="Workspaces")
NO_WORKSPACE = msg("tui.workspaces.none_description", fallback="No workspace")
NEW = msg("tui.workspaces.new", fallback="+ New workspace…")
NEW_TITLE = msg("tui.workspaces.new_title", fallback="New Workspace")
EDIT_TITLE = msg("tui.workspaces.edit_title", fallback="Edit Workspace")
WORKSPACE_SAVED = msg("workspaces.commands.saved", fallback="Saved workspace “{name}”.")
NAME = msg("tui.workspaces.name", fallback="Name")
NAME_PLACEHOLDER = msg("tui.workspaces.name_placeholder", fallback="Workspace name")
PRIMARY = msg("tui.workspaces.primary", fallback="Primary folder")
PRIMARY_HINT = msg(
    "tui.workspaces.primary_hint",
    fallback="Sessions in this workspace use the primary folder as their working directory.",
)
SELECT_PRIMARY = msg("tui.workspaces.select_primary", fallback="Select primary folder")
ADD_REFERENCE = msg("tui.workspaces.add_reference", fallback="Add reference folder")
REFERENCE_FOLDERS = msg("tui.workspaces.reference_folders", fallback="Reference folders")
ADD_FOLDER = msg("tui.workspaces.add_folder", fallback="+ Add folder…")
REFERENCE_FOLDER_COUNT = msg(
    "tui.workspaces.folders", fallback="{count} reference folder", plural_fallback="{count} reference folders"
)
FOLDER_MISSING = msg("tui.workspaces.folder_missing", fallback="Folder was deleted or moved")
PRIMARY_MISSING = msg("tui.workspaces.primary_missing", fallback="Primary folder missing")
MISSING = msg("tui.workspaces.missing", fallback="missing")
PRIMARY_MARK = msg("tui.workspaces.primary_mark", fallback="primary")
MORE_REFERENCE_FOLDERS = msg(
    "tui.workspaces.more",
    fallback="+{count} more reference folder",
    plural_fallback="+{count} more reference folders",
)
CREATE = msg("tui.workspaces.create", fallback="Create")
CREATED_PICKER = msg(
    "tui.workspaces.created_picker",
    fallback="Created workspace “{name}”. You can enter it from the workspace picker.",
)
SAVE = msg("tui.workspaces.save", fallback="Save")
CANCEL = msg("tui.workspaces.cancel", fallback="Cancel")
DELETE = msg("tui.workspaces.delete", fallback="Delete")
DELETE_TITLE = msg("tui.workspaces.delete_title", fallback="Delete Workspace")
DELETE_MESSAGE = msg(
    "tui.workspaces.delete_message",
    fallback='Delete workspace\n"{name}"?\n\nIts sessions and folders are kept;\nthe sessions become Unspecified.',
    multiline=True,
)
BUSY = msg("tui.workspaces.busy", fallback="Cannot change workspace while agent is busy")
BUSY_TITLE = msg("tui.workspaces.busy_title", fallback="Busy")
EDIT = msg("tui.workspaces.edit", fallback="Edit")


@dataclass(frozen=True, slots=True)
class WorkspaceIndicatorState:
    """Display text for the workspace selector."""

    label: str
    tooltip: Content
    selector_label: str


_TOOLTIP_FOLDER_LIMIT = 4
"""Folders the status-bar tooltip lists; a longer list shows one fewer and a count of the rest."""


def _tooltip_folders(folders: tuple[str, ...]) -> tuple[str, ...]:
    return folders[: _TOOLTIP_FOLDER_LIMIT - 1 if len(folders) > _TOOLTIP_FOLDER_LIMIT else _TOOLTIP_FOLDER_LIMIT]


def find_missing_tooltip_folders(folders: tuple[str, ...]) -> frozenset[str]:
    """Check installed and configured folders from a background thread."""
    return frozenset(missing_folders(folders))


def compute_workspace_indicator_state(
    localizer: Localizer,
    name: str | None,
    folders: tuple[str, ...] = (),
    missing: frozenset[str] = frozenset(),
    error: MessageRef | None = None,
) -> WorkspaceIndicatorState:
    """Render the workspace name, configured paths, and diagnostics as literal text."""
    label = (
        sanitize_legacy_scalar(surrogate_safe_text(name))
        if name is not None
        else render_str(localizer, UNSPECIFIED.bind())
    )
    rows: list[Content | str] = [label]
    for index, folder in enumerate(_tooltip_folders(folders)):
        marks = []
        if index == 0:
            marks.append(Content(render_str(localizer, PRIMARY_MARK.bind())))
        if folder in missing:
            marks.append(Content.styled(render_str(localizer, MISSING.bind()), "$warning"))
        path = sanitize_legacy_scalar(surrogate_safe_text(folder))
        rows.append(Content.assemble(path, " (", Content(", ").join(marks), ")") if marks else path)
    if len(folders) > _TOOLTIP_FOLDER_LIMIT:
        rows.append(
            render_str(localizer, MORE_REFERENCE_FOLDERS.bind(count=len(folders) - (_TOOLTIP_FOLDER_LIMIT - 1)))
        )
    if error is not None:
        rows.append(render_str(localizer, error))
    return WorkspaceIndicatorState(label, Content("\n").join(rows), render_str(localizer, WORKSPACE.bind()))
