# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-owned remembered approval management."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from chrys.app.cli.headless import write_error, write_warning
from chrys.app.cli.launch_cwd import resolve_workspace_selector
from chrys.foundation.branding import APP_COMMAND
from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.config.warnings import trust_conflict_warning
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.platform import get_platform
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.approval.grant_store import GRANTS_FILE, ApprovalGrantStore, grant_matches_scope, session_grants_path
from chrys.service.approval.reuse import ApprovalGrant, CommandKey, workdir_path
from chrys.service.workspaces.registry import WorkspaceRegistryError, is_workspace_id


def _stores(config_dir: Path, session: str | None, *, shared_only: bool) -> list[ApprovalGrantStore]:
    if session is not None:
        path = session_grants_path(config_dir, session)
        if not path.parent.is_dir():
            raise ValueError("Session directory does not exist")
        return [ApprovalGrantStore(path)]
    if shared_only:
        return [ApprovalGrantStore(config_dir / GRANTS_FILE)]
    sessions = resolve_sessions_dir(config_dir, create=False)
    paths = [config_dir / GRANTS_FILE, *sorted(sessions.glob(f"*/{GRANTS_FILE}"))]
    return [ApprovalGrantStore(path) for path in paths]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} approval", description="Manage explicitly remembered approvals."
    )
    parser.add_argument(
        "action", nargs="?", choices=["list", "revoke", "clear"], default="list", help="What to do (default: list)"
    )
    parser.add_argument("id", nargs="?", help="Full grant ID for revoke")
    parser.add_argument("--session", help="Select one saved session (full or short ID)")
    parser.add_argument("--workdir", metavar="DIR", help="Filter grants by working directory")
    parser.add_argument("--project", dest="workdir", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", metavar="WORKSPACE", help="Filter shared grants by workspace name or ID")
    parser.add_argument("--all", action="store_true", help="Explicitly clear all shared and session grants")
    parser.add_argument("--json", action="store_true", help="List structured records")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.action == "revoke") != bool(args.id):
        parser.error("revoke requires a grant ID; other commands do not accept one")
    if args.workdir == "":
        parser.error("--workdir requires a non-empty directory")
    if args.session == "":
        parser.error("--session requires a non-empty session ID")
    workspace = args.workspace
    if workspace is not None:
        if not workspace.strip():
            parser.error("--workspace requires a non-empty workspace name or ID")
        if args.workdir is not None or args.session is not None:
            parser.error("--workspace cannot be combined with --workdir or --session")
    filtered = args.session is not None or args.workdir is not None or workspace is not None
    if args.action == "clear" and not (args.all or filtered):
        parser.error("clear requires --all, --session, --workdir or --workspace")
    if args.all and (args.action != "clear" or filtered):
        parser.error("--all is only valid with an unfiltered clear")
    bootstrap = bootstrap_runtime(configure_stdio=True, setup_telemetry=False)
    for warning in bootstrap.warnings:
        write_warning(warning.message, code=warning.code, as_json=args.json)
    if bootstrap.loaded.trust_conflicts:
        warning = trust_conflict_warning(bootstrap.loaded.trust_conflicts)
        write_warning(warning.message, code=warning.code, as_json=args.json)
    scope_key = None
    if workspace is not None:
        # A full ID always means that workspace, including a deleted one whose grants remain.
        if not is_workspace_id(workspace):
            try:
                selected, _ = resolve_workspace_selector(workspace)
            except WorkspaceRegistryError as error:
                # CLI entry-point output stays English (AGENTS.md).
                write_error(format_message(error.display_message), as_json=args.json, code=error.code)
                return 1
            workspace = selected.id
        scope_key = ("WORKSPACE", workspace)
    elif args.workdir is not None:
        scope_key = ("WORKDIR", workdir_path(args.workdir))
    try:
        stores = _stores(get_platform().config_dir, args.session, shared_only=workspace is not None)
    except ValueError as error:
        parser.error(str(error))
    if args.action == "clear":
        results = [store.clear(scope_key=scope_key) for store in stores]
        if not all(results):
            sys.stderr.write("Some approval stores could not be cleared.\n")
            return 1
        sys.stdout.write("Cleared selected grants.\n")
        return 0
    rows: list[tuple[ApprovalGrantStore, ApprovalGrant]] = []
    for store in stores:
        for record in store.load():
            try:
                grant = ApprovalGrant.model_validate(record)
            except ValueError, TypeError, RecursionError:
                continue
            if scope_key is None or grant_matches_scope(grant.scope, grant.scope_id, grant.workdir, scope_key):
                rows.append((store, grant))
    if args.action == "revoke":
        for store, grant in rows:
            if grant.id == args.id:
                if store.revoke(grant.id):
                    sys.stdout.write(f"Revoked {grant.id}.\n")
                    return 0
                sys.stderr.write("Grant could not be revoked.\n")
                return 1
        sys.stderr.write("No matching grant.\n")
        return 1
    if args.json:
        sys.stdout.write(
            json.dumps(
                [grant.model_dump(mode="json", exclude_none=True) for _, grant in rows], ensure_ascii=True, indent=2
            )
            + "\n"
        )
    else:
        for _, grant in rows:
            if isinstance(grant.key, CommandKey):
                command = grant.key.command
                target = shlex.join(command) if isinstance(command, tuple) else command
            else:
                target = grant.key.path
            scope_label = "workspace" if grant.scope == "WORKSPACE" else "workdir"
            scope_value = grant.workdir if grant.scope == "SESSION" else grant.scope_id
            sys.stdout.write(
                sanitize_legacy_scalar(
                    f"{grant.id}  {grant.scope} {grant.scope_id}  {scope_label}={scope_value}  {'prefix' if grant.prefix else 'exact'} {target}"
                )
                + "\n"
            )
        if not rows:
            sys.stdout.write("No remembered approvals.\n")
    return 0
