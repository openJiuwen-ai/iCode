# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace command validation and messages shared by frontends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.events.types import WorkspaceChange
from chrys.foundation.i18n import DisplayBlock, DisplayPath, MessageRef, msg
from chrys.foundation.models.workspace import WorkspaceConfirmation, path_identity, same_directory
from chrys.foundation.models.workspace_operations import WorkspaceDefinitionEdit, WorkspaceOperation
from chrys.service.workspaces.paths import folders_overlap, resolve_workspace_directory
from chrys.service.workspaces.registry import (
    PRIMARY_IMMUTABLE,
    WorkspaceRegistryError,
    is_unspecified_workspace_name,
    workspace_folder_path,
    workspace_folder_spelling,
    workspace_not_found,
    workspace_selector_not_found,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.foundation.i18n import Localizer
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.workspaces.registry import RegistrySnapshot, WorkspaceRegistry

WORKSPACE = "workspace"
WORKSPACE_DESCRIPTION = msg("tui.commands.description.workspace", fallback="Switch, create, edit or delete workspaces")
COMMAND_UNAVAILABLE_RUNNING = msg(
    "tui.commands.unavailable_while_running",
    fallback="/{name} is not available while agent is running",
)
WORKSPACE_UPDATE_FAILED = msg("workspaces.commands.update_failed", fallback="Workspace update failed.")

_USAGE = msg(
    "workspaces.commands.usage",
    fallback="Usage\n{commands}",
    multiline=True,
)
_ACTION_USAGE = msg("workspaces.commands.action_usage", fallback="Usage: /workspace {syntax}")
_UNKNOWN_ACTION = msg(
    "workspaces.commands.unknown_action",
    fallback="Unknown workspace action “{action}”. Run /workspace for usage.",
)
_UNSPECIFIED_DELETE = msg(
    "workspaces.commands.unspecified_delete", fallback="Unspecified is a reserved item and cannot be deleted."
)
_NOT_IN_WORKSPACE = msg(
    "workspaces.commands.not_in_workspace",
    fallback="This session is not in a workspace. Use /workspace switch <name> to enter one.",
)
_FOLDER_MISSING = msg("workspaces.commands.folder_missing", fallback="Folder does not exist: {path}.")
_FOLDER_ALREADY_REGISTERED = msg(
    "workspaces.commands.folder_already_registered", fallback="{path} is already a reference folder."
)
_FOLDER_IS_PRIMARY = msg("workspaces.commands.folder_is_primary", fallback="{path} is already the primary folder.")
_FOLDER_OVERLAPS_PRIMARY = msg(
    "workspaces.commands.folder_overlaps_primary", fallback="{path} overlaps with the primary folder {primary}."
)
_FOLDER_OVERLAPS_REFERENCE = msg(
    "workspaces.commands.folder_overlaps_reference", fallback="{path} overlaps with the reference folder {reference}."
)
_FOLDER_NOT_REGISTERED = msg(
    "workspaces.commands.folder_not_registered", fallback="No reference folder matches {path}."
)
WORKSPACE_CREATED = msg("workspaces.commands.created_only", fallback="Created workspace “{name}”.")
ADD_REFERENCE_FOLDERS_HINT = msg(
    "workspaces.commands.add_reference_folders_hint",
    fallback="To add reference folders, use /workspace add-folder <path>.",
)
_RENAMED = msg("workspaces.commands.renamed", fallback="Renamed the workspace to “{name}”.")
_FOLDER_ADDED = msg("workspaces.commands.folder_added", fallback="Added reference folder {path}.")
_FOLDER_AMBIGUOUS = msg(
    "workspaces.commands.folder_ambiguous",
    fallback="Several reference folders point to {path}. Enter the folder as the workspace lists it.",
)
_FOLDER_REMOVED = msg("workspaces.commands.folder_removed", fallback="Removed reference folder {path}.")
WORKSPACE_DELETED = msg("workspaces.commands.deleted", fallback="Deleted workspace “{name}”.")
_WORKSPACE_ALREADY_ABSENT = msg("workspaces.commands.already_absent", fallback="Workspace “{name}” is already absent.")
_WORKSPACE_REPLACED_ELSEWHERE = msg(
    "workspaces.commands.replaced_elsewhere",
    fallback="Workspace “{name}” was replaced elsewhere, so nothing was deleted. Check the workspace list and try again.",
)
_DELETED_CURRENT = msg(
    "workspaces.commands.deleted_current", fallback="Deleted workspace “{name}”. This session is now Unspecified."
)
WORKSPACE_EDIT_APPLIES_ON_REOPEN = msg(
    "workspaces.commands.edit_applies_on_reopen",
    fallback="Workspace changes will take effect the next time you open this session.",
)

ACP_LIST_ENTRY = msg("workspaces.commands.list_entry", fallback="{name} ({path})")
ACP_CURRENT_LIST_ENTRY = msg("workspaces.commands.current_list_entry", fallback="{name} ({path}) — current workspace")
ACP_CURRENT_UNSPECIFIED = msg("workspaces.commands.current_unspecified", fallback="Unspecified — current")
ACP_LIST_FOOTER = msg("workspaces.commands.list_footer", fallback="Run /workspace for usage.")
ACP_OPEN_PRIMARY_IN_EDITOR = msg(
    "workspaces.commands.open_primary_in_editor",
    fallback=(
        "The primary folder of workspace “{name}” is {path}. Open this folder in your editor, "
        "start a new conversation, then enter /workspace switch {name}."
    ),
)
ACP_MOVED = msg("workspaces.commands.moved", fallback="Moved to workspace “{name}”.")
ACP_ALREADY_IN_WORKSPACE = msg(
    "workspaces.commands.already_in_workspace", fallback="This session is already in workspace “{name}”."
)
ACP_MOVED_TO_UNSPECIFIED = msg("workspaces.commands.moved_to_unspecified", fallback="This session is Unspecified.")
ACP_MOVED_WITH_HISTORY = msg(
    "workspaces.commands.moved_with_history",
    fallback=(
        "Moved this session to workspace “{name}”. To start a new session in this workspace, "
        "start a new conversation in your editor, then enter /workspace switch {name}."
    ),
)
ACP_CREATED_AND_MOVED = msg(
    "workspaces.commands.created", fallback="Created workspace “{name}” and moved this session into it."
)

WORKSPACE_HINT = msg(
    "workspaces.commands.workspace_hint",
    fallback="{actions} …",
)

WORKSPACE_ACTIONS = {
    "switch": "[<name>]",
    "new": "<name>",
    "rename": "<new name>",
    "add-folder": "<path>",
    "remove-folder": "<path>",
    "delete": "<name>",
}


@dataclass(frozen=True, slots=True)
class WorkspaceCommand:
    action: str
    argument: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceCommandReply:
    messages: tuple[MessageRef, ...]
    change: WorkspaceChange | None = None
    error: bool = False
    created: Workspace | None = None
    list_count: int = 0


def parse_workspace_command(text: str) -> WorkspaceCommand | WorkspaceCommandReply | None:
    """Recognize /workspace, keeping folder paths literal and trimming names."""
    text = text.lstrip()
    trimmed = text.rstrip()
    if "\n" in trimmed or "\r" in trimmed:
        return None
    text = text.partition("\n")[0].partition("\r")[0]
    words = text.split(None, 1)
    # The fullwidth slash an IME types counts as "/", as in the TUI.
    if not words or words[0] not in {f"/{WORKSPACE}", f"\uff0f{WORKSPACE}"}:
        return None
    argument = words[1] if len(words) > 1 else ""
    if not argument:
        commands = "\n".join(f"/{WORKSPACE} {action} {syntax}".rstrip() for action, syntax in WORKSPACE_ACTIONS.items())
        return WorkspaceCommandReply((_USAGE.bind(commands=DisplayBlock(commands)), PRIMARY_IMMUTABLE.bind()))
    parts = argument.split(None, 1)
    action = parts[0]
    syntax = WORKSPACE_ACTIONS.get(action)
    if syntax is None:
        return WorkspaceCommandReply((_UNKNOWN_ACTION.bind(action=action),), error=True)
    if action in {"add-folder", "remove-folder"}:
        # The first whitespace after the action separates it from the literal path.
        value = argument[len(action) + 1 :]
    else:
        value = parts[1].strip() if len(parts) > 1 else ""
    if action != "switch" and bool(syntax) != bool(value):
        return WorkspaceCommandReply((_ACTION_USAGE.bind(syntax=f"{action} {syntax}".rstrip()),), error=True)
    return WorkspaceCommand(action, value)


def find_workspace_by_name(snapshot: RegistrySnapshot, name: str) -> Workspace:
    """Find an exact, case-insensitive name in a readable registry."""
    if snapshot.error is not None:
        raise snapshot.error
    for workspace in snapshot.workspaces:
        if workspace.name.casefold() == name.casefold():
            return workspace
    raise workspace_selector_not_found(name)


def workspace_change(
    workspace_id: str | None, session_id: str | None, *, confirmation: WorkspaceConfirmation | None = None
) -> WorkspaceChange:
    return WorkspaceChange(
        session_id=session_id,
        workspace_operation=WorkspaceOperation.SELECT if workspace_id is not None else WorkspaceOperation.UNSPECIFY,
        workspace_id=workspace_id,
        confirmation=confirmation,
    )


def delete_workspace_request(
    workspace: Workspace, *, current_id: str | None, session_id: str | None
) -> WorkspaceCommandReply:
    """Request one registry deletion and any live membership change it implies."""
    message = _DELETED_CURRENT if workspace.id == current_id else WORKSPACE_DELETED
    return WorkspaceCommandReply(
        (message.bind(name=workspace.name),),
        WorkspaceChange(
            session_id=session_id,
            workspace_operation=WorkspaceOperation.UNSPECIFY,
            delete_workspace=workspace,
        ),
    )


def edit_workspace_request(edit: WorkspaceDefinitionEdit, session_id: str | None) -> WorkspaceChange:
    return WorkspaceChange(
        session_id=session_id,
        workspace_operation=WorkspaceOperation.SELECT,
        workspace_id=edit.expected.id,
        edit_workspace=edit,
    )


def manage_workspace(
    registry: WorkspaceRegistry,
    command: WorkspaceCommand,
    *,
    current: Workspace | None,
    cwd: str,
    session_id: str | None,
    localizer: Localizer | None = None,
    shown: Sequence[Workspace] | None = None,
) -> WorkspaceCommandReply:
    """Prepare the workspace command and its session change.

    *shown* is the list the user chose from. ``delete`` refuses a name that now
    belongs to another workspace than the one listed under it.
    """
    action, argument = command.action, command.argument
    current_id = current.id if current is not None else None
    try:
        if action == "new":
            created = registry.create(argument, cwd)
            confirmation = WorkspaceConfirmation.capture(current_id, created.primary_cwd)
            return WorkspaceCommandReply(
                (),
                workspace_change(created.id, session_id, confirmation=confirmation),
                created=created,
            )
        snapshot = registry.snapshot()
        if action == "delete":
            if snapshot.error is not None:
                raise snapshot.error
            target = next((item for item in snapshot.workspaces if item.name.casefold() == argument.casefold()), None)
            if target is None:
                if is_unspecified_workspace_name(argument, localizer):
                    return WorkspaceCommandReply((_UNSPECIFIED_DELETE.bind(),), error=True)
                return WorkspaceCommandReply((_WORKSPACE_ALREADY_ABSENT.bind(name=argument),))
            listed = next((item for item in shown or () if item.name.casefold() == argument.casefold()), None)
            if listed is not None and listed.id != target.id:
                return WorkspaceCommandReply((_WORKSPACE_REPLACED_ELSEWHERE.bind(name=argument),), error=True)
            return delete_workspace_request(target, current_id=current_id, session_id=session_id)
        if current is None:
            return WorkspaceCommandReply((_NOT_IN_WORKSPACE.bind(),), error=True)
        if snapshot.error is not None:
            raise snapshot.error
        target = snapshot.workspace(current.id)
        if target is None:
            raise workspace_not_found(current.id)
        prepared = prepare_definition_edit(target, action, argument, cwd)
        if isinstance(prepared, WorkspaceCommandReply):
            return prepared
        edit, message = prepared
    except WorkspaceRegistryError as error:
        return WorkspaceCommandReply((error.display_message,), error=True)
    messages = (message,)
    if not same_directory(cwd, target.primary_cwd, realpath_fallback=True):
        messages = (*messages, WORKSPACE_EDIT_APPLIES_ON_REOPEN.bind())
    return WorkspaceCommandReply(messages, edit_workspace_request(edit, session_id))


def prepare_definition_edit(
    target: Workspace, action: str, argument: str, cwd: str
) -> tuple[WorkspaceDefinitionEdit, MessageRef] | WorkspaceCommandReply:
    """Prepare a rename or reference-folder edit and its success message."""
    folders = [folder.path for folder in target.working_dirs]
    if action == "rename":
        name = argument
        message = _RENAMED.bind(name=name)
    else:
        name = target.name
        result = _edit_reference_folder(folders, cwd, argument, primary=target.primary_cwd, add=action == "add-folder")
        if isinstance(result, WorkspaceCommandReply):
            return result
        message = result
    return WorkspaceDefinitionEdit(target, name, tuple(folders)), message


def _edit_reference_folder(
    folders: list[str], cwd: str, argument: str, *, primary: str, add: bool
) -> MessageRef | WorkspaceCommandReply:
    if not add:
        # A listed spelling names its own entry, so the folder itself is not read.
        typed = workspace_folder_spelling(cwd, argument)
        if typed in folders:
            folders.remove(typed)
            return _FOLDER_REMOVED.bind(path=DisplayPath(typed))
    path = workspace_folder_path(cwd, argument)
    display = DisplayPath(path)
    if add and resolve_workspace_directory(path) is None:
        return WorkspaceCommandReply((_FOLDER_MISSING.bind(path=display),), error=True)
    identity = path_identity(path)
    same = [folder for folder in folders if path_identity(folder) == identity]
    if add:
        if same:
            return WorkspaceCommandReply((_FOLDER_ALREADY_REGISTERED.bind(path=display),), error=True)
        if identity == path_identity(primary):
            return WorkspaceCommandReply((_FOLDER_IS_PRIMARY.bind(path=display),), error=True)
        if folders_overlap(path, primary):
            error = _FOLDER_OVERLAPS_PRIMARY.bind(path=display, primary=DisplayPath(primary))
            return WorkspaceCommandReply((error,), error=True)
        for folder in folders:
            if folders_overlap(path, folder):
                error = _FOLDER_OVERLAPS_REFERENCE.bind(path=display, reference=DisplayPath(folder))
                return WorkspaceCommandReply((error,), error=True)
        folders.append(path)
        message = _FOLDER_ADDED
    else:
        # Not a listed spelling: a symlink may now point where several entries do.
        if len(same) > 1:
            return WorkspaceCommandReply((_FOLDER_AMBIGUOUS.bind(path=display),), error=True)
        if not same:
            return WorkspaceCommandReply((_FOLDER_NOT_REGISTERED.bind(path=display),), error=True)
        folders.remove(same[0])
        display = DisplayPath(same[0])
        message = _FOLDER_REMOVED
    return message.bind(path=display)
