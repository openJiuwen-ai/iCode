# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless ``chrys hook`` commands: list the hooks a session would load, and check hooks files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from chrys.app.cli import headless
from chrys.app.cli.display import counted, one_line, shown_path
from chrys.app.cli.headless import EXIT_INTERRUPTED
from chrys.app.cli.launch_cwd import LAUNCH_CWD_MISSING_CODE, launch_cwd_missing_message
from chrys.app.parsing import SanitizingArgumentParser, add_help_option, default_to_list
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.platform import get_platform
from chrys.service.hooks.validation import check_default_sources, check_path

if TYPE_CHECKING:
    from chrys.service.hooks.schema import HookConfig, HookMatch, HookRun
    from chrys.service.hooks.validation import CheckedFile, HookDiagnostic, HooksReport


_NOT_LOADED: Final = "not loaded: turn on Load project hooks under Settings → Safety → Project trust"
# The listing says this itself, on the file's own line.
_DORMANT_WARNING_CODE: Final = "project_hooks_dormant"


def build_parser() -> argparse.ArgumentParser:
    """Build the ``chrys hook`` parser: ``list`` and ``validate`` sub-commands."""
    parser = SanitizingArgumentParser(
        prog=f"{APP_COMMAND} hook",
        description=f"List the hooks an {APP_DISPLAY_NAME} session started in the current directory would load, "
        "and check hooks configuration files. With no command, list them.",
        add_help=False,
    )
    add_help_option(parser)
    commands = parser.add_subparsers(
        dest="command", metavar="command", required=True, parser_class=SanitizingArgumentParser
    )
    list_parser = commands.add_parser(
        "list",
        help="List the hooks found for the current directory",
        description="List the project hooks file (.chrys/hooks in the current directory), then the global one, "
        "with the hooks each defines in configuration order, and say whether a session started here loads it. "
        "Nothing is run.",
        add_help=False,
    )
    add_help_option(list_parser)
    list_parser.add_argument("--json", action="store_true", help="Emit JSON output")
    validate_parser = commands.add_parser(
        "validate",
        help="Check hooks configuration files and report each problem",
        description="Check hooks configuration files as a session would load them, and report every problem with "
        "its file and line. Nothing is run. With no PATH, check the project file for the current directory (also "
        "when project hooks are off) and the global file. Exit code 0 means PASS (warnings allowed), 1 means FAIL.",
        add_help=False,
    )
    add_help_option(validate_parser)
    validate_parser.add_argument(
        "path", nargs="?", help="A hooks file, or a folder holding hooks.yaml, hooks.yml or hooks.json"
    )
    validate_parser.add_argument("--json", action="store_true", help="Emit one JSON report on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys hook``."""
    headless.configure_logging()
    args = build_parser().parse_args(default_to_list(sys.argv[1:] if argv is None else argv))
    if (missing_cwd := launch_cwd_missing_message(workdir_flag=False)) is not None:
        headless.write_error(missing_cwd, as_json=args.json, code=LAUNCH_CWD_MISSING_CODE)
        return 1
    try:
        runtime = headless.prepare_runtime()
        workspace = Path.cwd()
        config_dir = get_platform().config_dir
        if args.command == "list":
            headless.write_warning_events(
                (warning for warning in runtime.pending_warnings if warning.code != _DORMANT_WARNING_CODE),
                runtime.localizer,
                as_json=args.json,
            )
            report = check_default_sources(
                config_dir, workspace, project_hooks_enabled=runtime.settings.project_hooks_enabled
            )
            _write_list(report, workspace, as_json=args.json)
            return 0
        headless.write_pending_warnings(runtime, as_json=args.json)
        if args.path is None:
            report = check_default_sources(
                config_dir, workspace, project_hooks_enabled=runtime.settings.project_hooks_enabled
            )
        else:
            report = check_path(Path(args.path), config_dir=config_dir)
    except KeyboardInterrupt:
        headless.write_error("Interrupted by user.", as_json=args.json, code="interrupted")
        return EXIT_INTERRUPTED
    except Exception as exc:
        headless.write_error(headless.exception_message(exc), as_json=args.json)
        return 1
    if args.json:
        _write_json(_validate_json(report))
    else:
        sys.stdout.write("".join(f"{line}\n" for line in _report_lines(report, workspace)))
    return 0 if report.passed else 1


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _write_list(report: HooksReport, workspace: Path, *, as_json: bool) -> None:
    if as_json:
        _write_json(
            {
                "version": 1,
                "files": [_file_json(checked) for checked in report.files],
                "searched": list(report.searched),
            }
        )
        return
    sys.stdout.write("".join(f"{line}\n" for line in _list_lines(report, workspace)))


def _list_lines(report: HooksReport, workspace: Path) -> list[str]:
    """The human listing, one line per fact and unwrapped, whole for a pipe or a grep; a terminal wraps a long one."""
    if not report.files:
        return ["No hooks configuration found. Searched:", *(f"  {shown_path(s, workspace)}" for s in report.searched)]
    lines: list[str] = []
    for index, checked in enumerate(report.files):
        if index:
            lines.append("")
        lines.extend(_file_lines(checked, workspace))
    lines.extend(
        f"note: {one_line(_hook_prefix(diagnostic) + diagnostic.message)}"
        for diagnostic in report.diagnostics
        if diagnostic.code == "duplicate_id_across_files"
    )
    return lines


def _file_lines(checked: CheckedFile, workspace: Path) -> list[str]:
    source = checked.source
    scope = "Project" if source.scope == "project" else "Global"
    path = shown_path(str(source.path), workspace)
    invalid = None
    if checked.rejection is not None:
        invalid = f"{_diagnostic_text(checked.rejection)}; run '{APP_COMMAND} hook validate' for details"
    if not source.loaded:
        # Fixing the file alone would not load it: say why it is off first.
        state = _NOT_LOADED if invalid is None else f"{_NOT_LOADED}; the file is also invalid: {invalid}"
    elif invalid is not None:
        state = f"invalid, not loaded: {invalid}"
    else:
        state = "loaded"
    lines = [f"{scope} hooks: {path} ({one_line(state)})"]
    hooks_file = checked.hooks_file
    if hooks_file is None:
        return lines
    if not hooks_file.hooks and not hooks_file.skipped_hooks:
        return [*lines, "  No hooks defined."]
    # One short block per hook: a table this wide folds its paths apart in an 80-column terminal.
    for hook in hooks_file.hooks:
        when = [hook.event.value, hook.execution.mode] + ([] if hook.enabled else ["turned off"])
        lines.append(f"  {one_line(hook.id)} ({', '.join(when)})")
        if (match := _match_summary(hook.match)) is not None:
            lines.append(f"    match: {one_line(match)}")
        lines.append(f"    run: {_run_summary(hook, source.path.parent, workspace)}")
    lines.extend(
        f"  skipped {one_line(skipped.id)}: {one_line(skipped.reason)}" for skipped in hooks_file.skipped_hooks
    )
    return lines


def _match_summary(match: HookMatch) -> str | None:
    parts: list[str] = []
    if match.profile is not None:
        parts.append(f"profile={match.profile}")
    if match.profiles:
        parts.append(f"profiles={','.join(match.profiles)}")
    if match.tool_kind is not None:
        parts.append(f"tool_kind={match.tool_kind}")
    if match.tool_name is not None:
        parts.append(f"tool_name={match.tool_name}")
    for name, arg in match.args.items():
        # Quoted as written, not repr()'s escaping, which would double every backslash in a pattern.
        conditions = [
            f"{verb} '{value}'"
            for verb, value in (("equals", arg.equals), ("contains", arg.contains), ("matches", arg.regex))
            if value is not None
        ]
        parts.append(f"args.{name} {' and '.join(conditions)}" if conditions else f"args.{name} present")
    return ", ".join(parts) or None


def _existing_target(run: HookRun) -> str | None:
    """The script path or the program a hook runs, if it names a file that exists or a program found on PATH.

    Anything else is never shown, whatever its shape: what was written there can hold more than a path, such as
    ``notify --token abc``. The lookup is made from here, not from the hook's own run.cwd or PATH, which can hold
    variables filled in only when it runs.
    """
    if run.type == "script":
        return run.path if os.path.isfile(run.path) else None
    if run.type == "command":
        program = run.argv[0]
        return program if os.path.isfile(program) or shutil.which(program) is not None else None
    return None


def _run_summary(hook: HookConfig, hooks_dir: Path, workspace: Path) -> str:
    """What the hook runs, without arguments, environment values or shell text, which can hold secrets.

    A script in the hooks file's folder is shown as written there, relative to that folder; ``…`` stands in for a
    script or program that names nothing that exists.
    """
    run = hook.run
    if run.type == "shell":
        return "shell"
    target = _existing_target(run)
    if target is None:
        return f"{run.type} …"
    if run.type == "command":
        return f"command {one_line(target)}"
    # Script paths are resolved when the file loads; the folder may be reached through a link.
    hooks_dir = hooks_dir.resolve()
    try:
        return f"script {one_line(str(Path(target).relative_to(hooks_dir)))}"
    except ValueError:
        return f"script {shown_path(target, workspace)}"


def _file_json(checked: CheckedFile) -> dict[str, Any]:
    source = checked.source
    hooks_file = checked.hooks_file
    if not source.loaded:
        state = "not_loaded"
    elif checked.rejection is not None:
        state = "invalid"
    else:
        state = "loaded"
    return {
        "scope": source.scope,
        "path": str(source.path),
        "state": state,
        "error": None if checked.rejection is None else _diagnostic_json(checked.rejection),
        "hooks": [] if hooks_file is None else [_hook_json(hook) for hook in hooks_file.hooks],
        "skipped": []
        if hooks_file is None
        else [{"id": skipped.id, "reason": skipped.reason} for skipped in hooks_file.skipped_hooks],
    }


def _hook_json(hook: HookConfig) -> dict[str, Any]:
    run = hook.run
    target = _existing_target(run)
    execution = hook.execution
    match = hook.match
    return {
        "id": hook.id,
        "event": hook.event.value,
        "enabled": hook.enabled,
        "description": hook.description,
        "execution": {
            "mode": execution.mode,
            "detach": execution.detach,
            "delivery": execution.delivery,
            "timeout_seconds": execution.timeout_seconds,
            "on_error": execution.on_error,
        },
        "match": {
            "profile": match.profile,
            "profiles": list(match.profiles),
            "tool_kind": match.tool_kind,
            "tool_name": match.tool_name,
            "args": {
                name: {"equals": arg.equals, "contains": arg.contains, "regex": arg.regex}
                for name, arg in match.args.items()
            },
        },
        "run": {
            "type": run.type,
            "path": target if run.type == "script" else None,
            "program": target if run.type == "command" else None,
        },
    }


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _report_lines(report: HooksReport, workspace: Path) -> list[str]:
    """The human report: compiler-style diagnostics, then one ``PASS``/``FAIL`` line."""
    lines: list[str] = []
    for diagnostic in report.diagnostics:
        place = ""
        if diagnostic.file is not None:
            place = shown_path(diagnostic.file, workspace)
            if diagnostic.line is not None:
                place += f":{diagnostic.line}"
                if diagnostic.column is not None:
                    place += f":{diagnostic.column}"
            place += ": "
        lines.append(
            f"{place}{diagnostic.severity}: {one_line(_hook_prefix(diagnostic) + diagnostic.message)} "
            f"[{diagnostic.code}]"
        )
    errors = sum(diagnostic.severity == "error" for diagnostic in report.diagnostics)
    warnings = len(report.diagnostics) - errors
    summary = f"{counted(len(report.files), 'file')} · {counted(report.hook_count, 'hook')}"
    if report.passed:
        lines.append(f"PASS {summary}" + (f" · {counted(warnings, 'warning')}" if warnings else ""))
    else:
        counts = counted(errors, "error") + (f", {counted(warnings, 'warning')}" if warnings else "")
        lines.append(f"FAIL {summary}: {counts}")
    return lines


def _validate_json(report: HooksReport) -> dict[str, Any]:
    """Version 1 of the ``--json`` report; every key is always present, unknown values are null."""
    return {
        "version": 1,
        "status": "pass" if report.passed else "fail",
        "files": [
            {
                "scope": checked.source.scope,
                "path": str(checked.source.path),
                # A session loads a rejected file's hooks no more than a switched-off one's.
                "loaded": None
                if checked.source.loaded is None
                else checked.source.loaded and checked.rejection is None,
                "hook_count": checked.entry_count,
            }
            for checked in report.files
        ],
        "searched": list(report.searched),
        "diagnostics": [_diagnostic_json(diagnostic) for diagnostic in report.diagnostics],
    }


def _diagnostic_json(diagnostic: HookDiagnostic) -> dict[str, Any]:
    return {
        "severity": diagnostic.severity,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "file": diagnostic.file,
        "line": diagnostic.line,
        "column": diagnostic.column,
        "hook_id": diagnostic.hook_id,
        "entry": diagnostic.entry,
    }


def _diagnostic_text(diagnostic: HookDiagnostic) -> str:
    """*diagnostic* without its file, for a line that already names the file."""
    place = f"line {diagnostic.line}: " if diagnostic.line is not None else ""
    return place + _hook_prefix(diagnostic) + diagnostic.message


def _hook_prefix(diagnostic: HookDiagnostic) -> str:
    if diagnostic.hook_id is not None:
        return f"hook {diagnostic.hook_id!r}: "
    if diagnostic.entry is not None:
        return f"hooks[{diagnostic.entry}]: "
    return ""


def _write_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
