# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-owned remembered approval management."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from chrys.foundation.branding import APP_COMMAND
from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform import get_platform
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.approval.grant_store import GRANTS_FILE, ApprovalGrantStore, session_grants_path
from chrys.service.approval.reuse import ApprovalGrant, CommandKey, project_path


def _stores(config_dir: Path, session: str | None) -> list[ApprovalGrantStore]:
    if session is not None:
        path = session_grants_path(config_dir, session)
        if not path.parent.is_dir():
            raise ValueError("Session directory does not exist")
        return [ApprovalGrantStore(path)]
    sessions = resolve_sessions_dir(config_dir, create=False)
    paths = [config_dir / GRANTS_FILE, *sorted(sessions.glob(f"*/{GRANTS_FILE}"))]
    return [ApprovalGrantStore(path) for path in paths]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} approval", description="Manage explicitly remembered approvals."
    )
    parser.add_argument(
        "action", nargs="?", choices=["list", "revoke", "clear"], default="list", help="What to do (default: list)"
    )
    parser.add_argument("id", nargs="?", help="Full grant ID for revoke")
    parser.add_argument("--session", help="Select one saved session (full or short ID)")
    parser.add_argument("--project", help="Filter by project directory")
    parser.add_argument("--all", action="store_true", help="Explicitly clear all project and session grants")
    parser.add_argument("--json", action="store_true", help="List structured records")
    args = parser.parse_args(argv)
    if (args.action == "revoke") != bool(args.id):
        parser.error("revoke requires a grant ID; other commands do not accept one")
    if args.action == "clear" and not (args.all or args.session or args.project):
        parser.error("clear requires --all, --session or --project")
    if args.all and (args.action != "clear" or args.session or args.project):
        parser.error("--all is only valid with an unfiltered clear")
    bootstrap_runtime(configure_stdio=True, setup_telemetry=False)
    try:
        stores = _stores(get_platform().config_dir, args.session)
    except ValueError as error:
        parser.error(str(error))
    project = project_path(args.project) if args.project else None
    if args.action == "clear":
        results = [store.clear(project=project) for store in stores]
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
            if project is None or grant.project == project:
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
            json.dumps([grant.model_dump(mode="json") for _, grant in rows], ensure_ascii=True, indent=2) + "\n"
        )
    else:
        for _, grant in rows:
            if isinstance(grant.key, CommandKey):
                command = grant.key.command
                target = shlex.join(command) if isinstance(command, tuple) else command
            else:
                target = grant.key.path
            sys.stdout.write(
                sanitize_legacy_scalar(
                    f"{grant.id}  {grant.scope} {grant.scope_id}  project={grant.project}  {'prefix' if grant.prefix else 'exact'} {target}"
                )
                + "\n"
            )
        if not rows:
            sys.stdout.write("No remembered approvals.\n")
    return 0
