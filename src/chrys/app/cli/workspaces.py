# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manage saved workspace definitions without opening a session."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import TYPE_CHECKING

from rich.table import Table
from rich.text import Text

from chrys.app.cli import display, headless
from chrys.app.cli.launch_cwd import (
    LAUNCH_CWD_MISSING_CODE,
    launch_cwd_missing_message,
    resolve_workspace_selector,
)
from chrys.app.parsing import SanitizingArgumentParser, default_to_list
from chrys.app.workspace_commands import (
    WORKSPACE_ACTIONS,
    WORKSPACE_CREATED,
    WORKSPACE_DELETED,
    WorkspaceCommandReply,
    prepare_definition_edit,
)
from chrys.foundation.branding import APP_COMMAND
from chrys.foundation.config.warnings import trust_conflict_warning
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.workspaces.paths import resolve_workspace_directory
from chrys.service.workspaces.registry import WorkspaceRegistry, WorkspaceRegistryError

if TYPE_CHECKING:
    from chrys.foundation.models.workspace import Workspace

_ADD_FOLDERS_HINT = msg("workspaces.cli.add_folders_hint", fallback="To add reference folders, run: {command}")


def build_parser() -> argparse.ArgumentParser:
    parser = SanitizingArgumentParser(
        prog=f"{APP_COMMAND} workspace",
        description="List, create, edit or delete workspaces. With no command, list them.",
    )
    commands = parser.add_subparsers(dest="action", required=True, parser_class=SanitizingArgumentParser)
    listing = commands.add_parser("list", help="List saved workspaces")
    listing.add_argument("--json", action="store_true", help="Emit JSON output")
    for action in WORKSPACE_ACTIONS:
        if action == "switch":
            continue
        command = commands.add_parser(action)
        if action == "new":
            command.add_argument("name")
        else:
            command.add_argument("workspace", help="Workspace name, full ID or unique ID prefix")
            if action == "rename":
                command.add_argument("argument", metavar="new_name")
            elif action in {"add-folder", "remove-folder"}:
                command.add_argument("argument", metavar="path", type=_folder_path)
    parser.set_defaults(json=False)
    return parser


def _folder_path(value: str) -> str:
    # An empty path would resolve to the current directory.
    if not value:
        raise argparse.ArgumentTypeError("path must not be empty")
    return value


def _clean(value: str) -> str:
    return sanitize_legacy_scalar(surrogate_safe_text(value))


def _print_workspaces(workspaces: tuple[Workspace, ...]) -> None:
    console = display.stdout_console()
    if not workspaces:
        console.print(Text("No workspaces."))
        return
    table = Table(box=None, show_edge=False, pad_edge=False)
    for title in ("Name", "Primary folder", "Reference folders"):
        table.add_column(title, overflow="fold")

    def folder_cell(path: str) -> str:
        return _clean(path) + (" (missing)" if resolve_workspace_directory(path) is None else "")

    for workspace in workspaces:
        table.add_row(
            Text(_clean(workspace.name)),
            Text(folder_cell(workspace.primary_cwd)),
            Text("\n".join(folder_cell(folder.path) for folder in workspace.working_dirs)),
        )
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(default_to_list(list(sys.argv[1:] if argv is None else argv)))
    bootstrap = bootstrap_runtime(configure_stdio=True, setup_telemetry=False)
    for warning in bootstrap.warnings:
        headless.write_warning(warning.message, code=warning.code, as_json=args.json)
    if bootstrap.loaded.trust_conflicts:
        warning = trust_conflict_warning(bootstrap.loaded.trust_conflicts)
        headless.write_warning(warning.message, code=warning.code, as_json=args.json)
    cwd = ""
    if args.action in {"new", "add-folder", "remove-folder"}:
        missing = launch_cwd_missing_message(workdir_flag=False)
        if missing:
            headless.write_error(missing, as_json=False, code=LAUNCH_CWD_MISSING_CODE)
            return 1
        cwd = os.getcwd()
    registry = WorkspaceRegistry()
    try:
        if args.action == "list":
            snapshot = registry.snapshot()
            if snapshot.error is not None:
                raise snapshot.error
            if args.json:
                # ASCII escapes keep undecodable path bytes intact through stdout's replace error handler.
                sys.stdout.write(
                    json.dumps(
                        {
                            "workspaces": [
                                {
                                    "id": workspace.id,
                                    "name": workspace.name,
                                    "primary_folder": workspace.primary_cwd,
                                    "reference_folders": [folder.path for folder in workspace.working_dirs],
                                    "missing_folders": [
                                        path
                                        for path in (
                                            workspace.primary_cwd,
                                            *(folder.path for folder in workspace.working_dirs),
                                        )
                                        if resolve_workspace_directory(path) is None
                                    ],
                                }
                                for workspace in snapshot.workspaces
                            ]
                        },
                        ensure_ascii=True,
                    )
                    + "\n"
                )
            else:
                _print_workspaces(snapshot.workspaces)
            return 0
        if args.action == "new":
            created = registry.create(args.name, cwd)
            message = format_message(WORKSPACE_CREATED.bind(name=created.name))
            # The full ID needs no shell quoting and cannot be ambiguous as a selector.
            command = f"{APP_COMMAND} workspace add-folder {created.id} <path>"
            message += " " + format_message(_ADD_FOLDERS_HINT.bind(command=command))
        else:
            target, _ = resolve_workspace_selector(args.workspace)
            if args.action == "delete":
                registry.delete(target.id, expected=target)
                message = format_message(WORKSPACE_DELETED.bind(name=target.name))
            else:
                # Trim a new name as the slash command parser does; paths are kept as typed.
                argument = args.argument.strip() if args.action == "rename" else args.argument
                prepared = prepare_definition_edit(target, args.action, argument, cwd)
                if isinstance(prepared, WorkspaceCommandReply):
                    headless.write_error(" ".join(format_message(item) for item in prepared.messages), as_json=False)
                    return 1
                edit, success = prepared
                registry.update(
                    target.id, name=edit.name, primary=target.primary_cwd, folders=edit.folders, expected=target
                )
                message = format_message(success)
    except WorkspaceRegistryError as error:
        headless.write_error(format_message(error.display_message), as_json=args.json, code=error.code)
        return 1
    sys.stdout.write(_clean(message) + "\n")
    return 0
