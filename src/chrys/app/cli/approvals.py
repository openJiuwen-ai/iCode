# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-owned remembered approval management."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from chrys.app.cli.display import one_line
from chrys.app.cli.headless import configure_logging, write_error, write_warning
from chrys.app.cli.launch_cwd import resolve_workspace_selector
from chrys.foundation.branding import APP_COMMAND
from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.config.warnings import trust_conflict_warning
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.platform import get_platform
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.approval.grant_store import (
    GRANTS_FILE,
    ApprovalGrantStore,
    grant_matches_scope,
    session_grants_path,
)
from chrys.service.approval.reuse import ApprovalGrant, CommandKey, workdir_path
from chrys.service.workspaces.registry import WorkspaceRegistryError, is_workspace_id


def _stores(config_dir: Path, session: str | None, *, shared_only: bool) -> list[ApprovalGrantStore]:
    if session is not None:
        path = session_grants_path(config_dir, session)
        if not path.parent.is_dir():
            raise ValueError("Session directory does not exist")
        # A directory that exists under its own name but maps to a different short
        # ID would let `clear --session <dir>` wipe an unrelated session.
        sessions_dir = resolve_sessions_dir(config_dir, create=False)
        if (sessions_dir / session).is_dir() and path.parent != sessions_dir / session:
            raise ValueError("Session ID is ambiguous; pass the full session ID")
        return [ApprovalGrantStore(path)]
    if shared_only:
        return [ApprovalGrantStore(config_dir / GRANTS_FILE)]
    sessions = resolve_sessions_dir(config_dir, create=False)
    paths = [config_dir / GRANTS_FILE, *sorted(sessions.glob(f"*/{GRANTS_FILE}"))]
    return [ApprovalGrantStore(path) for path in paths]


def _session_hint_round_trips(config_dir: Path, name: str, path: Path) -> bool:
    """A ``--session`` hint is only safe when the CLI maps this name back to *path*."""
    if session_short_id(name) != name:
        return False
    try:
        return session_grants_path(config_dir, name) == path
    except ValueError:
        return False


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
    parser.add_argument("--all", action="store_true", help="Explicitly clear all shared, workdir and session grants")
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
    if args.json:
        # Structured output must stay one JSON value per line: library logs would
        # otherwise reach stderr through the last-resort handler and break parsing.
        configure_logging()
    bootstrap = bootstrap_runtime(configure_stdio=True, setup_telemetry=False)
    for warning in bootstrap.warnings:
        write_warning(warning.message, code=warning.code, as_json=args.json)
    if bootstrap.loaded.trust_conflicts:
        warning = trust_conflict_warning(bootstrap.loaded.trust_conflicts)
        write_warning(warning.message, code=warning.code, as_json=args.json)
    config_dir = get_platform().config_dir
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
        stores = _stores(config_dir, args.session, shared_only=workspace is not None)
    except ValueError as error:
        parser.error(str(error))
    if args.action == "clear":
        failed: list[tuple[ApprovalGrantStore, str]] = []
        if args.all:
            # The shared store goes first and alone: if it cannot be repaired there is
            # no point destroying the session grants that still work.
            reason = stores[0].clear_or_reason()
            if reason is not None:
                failed.append((stores[0], reason))
            else:
                failed.extend(
                    (store, reason) for store in stores[1:] if (reason := store.clear_or_reason()) is not None
                )
        else:
            failed.extend(
                (store, reason)
                for store in stores
                if (reason := store.clear_or_reason(scope_key=scope_key)) is not None
            )
        if failed:
            for store, reason in failed:
                write_error(
                    f"Approval store could not be cleared: {store.path} ({reason})",
                    as_json=args.json,
                    code="approval_store_not_cleared",
                )
            return 1
        sys.stdout.write("Cleared selected grants.\n")
        return 0
    rows: list[tuple[ApprovalGrantStore, ApprovalGrant]] = []
    unreadable_sessions: list[ApprovalGrantStore] = []
    shared_store: ApprovalGrantStore | None = None
    for store in stores:
        for record in store.load():
            try:
                grant = ApprovalGrant.model_validate(record)
            except ValueError, TypeError, RecursionError:
                continue
            if scope_key is None or grant_matches_scope(grant.scope, grant.scope_id, grant.workdir, scope_key):
                rows.append((store, grant))
        if store.unreadable:
            if store.path.parent == config_dir:
                shared_store = store
            else:
                unreadable_sessions.append(store)
    unreadable = shared_store is not None or bool(unreadable_sessions)
    if unreadable and args.json:
        # One structured record per store, so a batch runner never has to parse prose.
        for store in ([shared_store] if shared_store is not None else []) + unreadable_sessions:
            write_error(
                f"Approval store could not be read: {store.path}", as_json=True, code="approval_store_unreadable"
            )
    elif unreadable:
        # A store that cannot be read must not look like a store that holds nothing,
        # and the suggested repair must match the store that actually failed. Paths and
        # reasons come from the filesystem, so each line is flattened before it is
        # written: a control character in a name must not split one diagnostic into two.
        sys.stderr.write(one_line("Some approval stores could not be read:") + "\n")
        if shared_store is not None:
            sys.stderr.write(one_line(f"  shared store: {shared_store.path}") + "\n")
        for store in unreadable_sessions:
            sys.stderr.write(one_line(f"  session store: {store.path.parent.name}") + "\n")
        for store in unreadable_sessions:
            name = store.path.parent.name
            problem = store.target_problem()
            if problem is not None:
                # Naming the command here would suggest a repair that cannot work.
                sys.stderr.write(one_line(f"  reason: {problem}") + "\n")
                sys.stderr.write(
                    one_line(
                        f"Fix or remove {shlex.quote(str(store.path))} by hand: it cannot be replaced automatically."
                    )
                    + "\n"
                )
            elif _session_hint_round_trips(config_dir, name, store.path):
                sys.stderr.write(
                    one_line(
                        f"Run `{APP_COMMAND} approval clear --session {shlex.quote(name)}` "
                        "to reset this session's approvals."
                    )
                    + "\n"
                )
            else:
                # The directory name is not a session ID: `--session` would project it
                # to some other store, so naming the path is the only safe advice.
                sys.stderr.write(
                    one_line(
                        f"Repair {shlex.quote(str(store.path))} by hand: the directory name is not a "
                        "session ID, so `clear --session` would pick a different store."
                    )
                    + "\n"
                )
        if shared_store is not None:
            problem = shared_store.target_problem()
            if problem is None:
                sys.stderr.write(
                    one_line(
                        f"Run `{APP_COMMAND} approval clear --all` to reset all workspace, workdir "
                        "and session approvals."
                    )
                    + "\n"
                )
            else:
                # Naming `clear --all` here would suggest a repair that cannot work.
                sys.stderr.write(one_line(f"  reason: {problem}") + "\n")
                sys.stderr.write(
                    one_line(
                        f"Fix or remove {shlex.quote(str(shared_store.path))} by hand: "
                        "it cannot be replaced automatically."
                    )
                    + "\n"
                )
    if args.action == "revoke":
        for store, grant in rows:
            if grant.id == args.id:
                if store.revoke(grant.id):
                    sys.stdout.write(f"Revoked {grant.id}.\n")
                    return 0
                write_error("Grant could not be revoked.", as_json=args.json, code="approval_grant_not_revoked")
                return 1
        if unreadable:
            # A store that could not be read may hold the id, so "not found" would be a guess.
            write_error(
                "No matching grant in the readable stores; some stores could not be read.",
                as_json=args.json,
                code="approval_grant_lookup_incomplete",
            )
        else:
            write_error("No matching grant.", as_json=args.json, code="approval_grant_not_found")
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
        if not rows and not unreadable:
            sys.stdout.write("No remembered approvals.\n")
    return 1 if unreadable else 0
