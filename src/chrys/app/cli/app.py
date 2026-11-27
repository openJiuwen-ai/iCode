# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Top-level ``chrys`` command dispatcher."""

from __future__ import annotations

import argparse
import sys
from typing import Final

from chrys import __version__
from chrys.app.parsing import (
    WORKDIR_OPTIONS,
    SanitizingArgumentParser,
    WorkspaceHelpFormatter,
    add_startup_session_option,
)
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME

_TUI_SUBPROCESS_COMMAND = "__tui_subprocess__"

_PLURAL_ALIASES: Final = {
    "agents": "agent",
    "models": "model",
    "approvals": "approval",
    "hooks": "hook",
    "workflows": "workflow",
    "trajectories": "trajectory",
}
"""Noun commands are shown in the singular and also accepted in the plural; neither form is deprecated."""


def _show_startup_notice(argv: list[str]) -> None:
    """Flush one complete line without taking ownership of terminal modes."""
    import contextlib
    import os

    if any(arg in {"--help", "-h", "--version", "-v"} for arg in argv) or os.environ.get("TEXTUAL_DRIVER"):
        return
    # Complete the line before imports or argument parsing can print diagnostics.
    # No later cleanup may erase a parser error, log line, or traceback.
    with contextlib.suppress(OSError, ValueError):
        if sys.stderr.isatty():
            sys.stderr.write(f"Starting {APP_DISPLAY_NAME}...\n")
            sys.stderr.flush()


def _run_tui(argv: list[str]) -> int:
    """Run the TUI with *argv* as its argument list."""
    _show_startup_notice(argv)
    from chrys.app.tui.app import main as tui_main

    old_argv = sys.argv
    sys.argv = [old_argv[0], *argv]
    try:
        tui_main()
    finally:
        sys.argv = old_argv
    return 0


def _run_install(argv: list[str]) -> int:
    """Run the installer command."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} install",
        description=f"Install {APP_DISPLAY_NAME} and set it to PATH so it can be launched anywhere.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    _, extra = parser.parse_known_args(argv)
    if extra:
        parser.error("install does not accept arguments")

    from chrys.app.installer import install_to_path

    install_to_path()
    return 0


def _run_uninstall(argv: list[str]) -> int:
    """Run the uninstall command."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} uninstall",
        description=(
            f"Remove {APP_DISPLAY_NAME} installed from an offline package. For an install made with uv, "
            f"pipx or pip, print the command that removes it. {APP_DISPLAY_NAME}'s data folder, with your "
            "settings, API keys, model profiles, agents, skills and sessions, is kept unless --purge is given."
        ),
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help=f"Also delete {APP_DISPLAY_NAME}'s data folder and everything in it, such as your settings and API keys",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Remove without asking first")
    args = parser.parse_args(argv)

    from chrys.app.uninstaller import uninstall

    return uninstall(purge=args.purge, assume_yes=args.yes)


def _run_serve(argv: list[str]) -> int:
    """Run the browser-hosted Chrys TUI command."""
    from chrys.app.cli.serve import main as serve_main

    return serve_main(argv)


def _run_acp(argv: list[str]) -> int:
    """Run the ACP stdio server command."""
    from chrys.app.cli.acp import main as acp_main

    return acp_main(argv)


def build_parser() -> argparse.ArgumentParser:
    """Help-only parser; real dispatch lives in ``main()``."""
    parser = SanitizingArgumentParser(
        prog=APP_COMMAND,
        description=f"{APP_DISPLAY_NAME} is a general-purpose extensible agent platform.",
        epilog=(
            "Commands:\n"
            "  run         Run an agent headlessly until the final response\n"
            "  agent       List available agent profiles\n"
            "  model       List available model profiles\n"
            "  approval    List, revoke or clear remembered approvals\n"
            "  hook        List hooks and check hook configuration files\n"
            "  workflow    List, validate and run workflows headlessly\n"
            "  trajectory  Export recorded trajectory analytics (perfetto/json/csv)\n"
            "  rsi         Experimental RSI eval and profile optimize\n"
            "  acp         Run an Agent Client Protocol stdio server\n"
            f"  serve       Host the {APP_DISPLAY_NAME} TUI in a browser\n"
            f"  install     Install {APP_DISPLAY_NAME} to PATH\n"
            f"  uninstall   Remove {APP_DISPLAY_NAME} (--purge also deletes your data)\n\n"
            "agent, model, approval, hook, workflow and trajectory also work in the plural;\n"
            "agent, model, approval, hook and workflow list when given no subcommand.\n\n"
            f"Default: '{APP_COMMAND}' launches the TUI. Run '{APP_COMMAND} <command> --help' for command options."
        ),
        formatter_class=WorkspaceHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument("-v", "--version", action="store_true", help=f"Print the {APP_DISPLAY_NAME} version and exit")

    tui_options = parser.add_argument_group("TUI options")
    add_startup_session_option(tui_options)
    tui_options.add_argument(
        "-a",
        "--agent",
        metavar="AGENT",
        help="Start the TUI with the given agent profile id, name, or display name",
    )
    tui_options.add_argument(
        "-m",
        "--model",
        metavar="MODEL",
        help="Start the TUI with the given active model profile id or name",
    )
    workspace_flags = tui_options.add_mutually_exclusive_group()
    workspace_flags.add_argument("--workspace", metavar="WORKSPACE", help="Open a workspace by name or ID prefix")
    workspace_flags.add_argument(*WORKDIR_OPTIONS, metavar="DIR", help="Start the TUI in the given working directory")
    from chrys.foundation import guard

    if guard.installed():  # offered only when it can work: without the package, --sandbox refuses to start
        tui_options.add_argument(
            "--sandbox",
            action="store_true",
            help="Confine file reads to the workspace and granted folders for this run (also: CHRYS_FILE_SANDBOX=1)",
        )
        tui_options.add_argument(
            "--no-sandbox", action="store_true", help="Run without the file-access sandbox this time"
        )

    tui_options.add_argument(
        "--sandbox-on-unavailable",
        choices=("refuse", "warn"),
        help="When a requested sandbox is unavailable: refuse startup (default), or warn and continue",
    )

    commands = parser.add_argument_group("commands")
    commands.add_argument(
        "command",
        metavar="command",
        nargs="?",
        help="Command to run; anything else is passed to the TUI",
    )
    return parser


_SANDBOXED_COMMANDS: Final = frozenset({"run", "acp", "workflow", "workflows", _TUI_SUBPROCESS_COMMAND})
_UNSANDBOXED_COMMANDS: Final = frozenset(
    {"agent", "agents", "model", "models", "approval", "approvals", "hook", "hooks", "install", "uninstall"}
    | {"serve", "trajectory", "trajectories", "sandbox"}
)


def _sandbox_launch_roots(argv: list[str]) -> list[str]:
    """Resolve launch roots without entering a folder or starting the runtime.

    Invalid selectors remain the command parser's to report. An empty result
    gives the guard no launch-folder access while that command reports them.
    """
    import os
    from pathlib import Path

    from chrys.foundation.guard import launch_dirs

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
    selectors = parser.add_mutually_exclusive_group()
    selectors.add_argument(*WORKDIR_OPTIONS, dest="workdir")
    selectors.add_argument("--workspace")
    try:
        selected, _unknown = parser.parse_known_args(argv)
    except argparse.ArgumentError:
        return []
    if selected.workspace is not None:
        from chrys.app.cli.launch_cwd import resolve_workspace_selector
        from chrys.service.workspaces.registry import WorkspaceRegistryError

        try:
            workspace, _snapshot = resolve_workspace_selector(selected.workspace)
        except WorkspaceRegistryError:
            return []
        return [workspace.primary_cwd, *(directory.path for directory in workspace.working_dirs)]
    if selected.workdir is not None:
        try:
            return [str(Path(os.path.expanduser(selected.workdir)).absolute())]
        except OSError:
            # A relative selector cannot resolve against a deleted cwd. Let the
            # command report its usual launch-directory error after parsing.
            return []
    return launch_dirs([])


def _enable_sandbox(argv: list[str]) -> list[str]:
    """Enable the file-access sandbox for commands that run an agent; strip its flags.

    Runs before anything else in the process — the sandbox confines only
    threads started after it — and decides once: see ``foundation/guard.py``.
    """
    from chrys.foundation.guard import parse_flags, startup_flags

    rest, wanted, policy = parse_flags(argv)
    command = rest[0] if rest else ""
    if command == "serve":
        # The HTTP server stays unconfined; every browser session decides at startup.
        return ["serve", *startup_flags(wanted, policy), *rest[1:]]
    if command in _UNSANDBOXED_COMMANDS or argv in (["--version"], ["-v"], ["--help"], ["-h"]):
        from chrys.foundation.guard import strip_flags

        return strip_flags(argv)[0]
    # _SANDBOXED_COMMANDS and anything else (passed to the TUI) are confined.
    from chrys.foundation.guard import install_at_startup

    rest, _status = install_at_startup(argv, resolve_launch_roots=_sandbox_launch_roots)
    return rest


def main() -> int:
    """Dispatch ``chrys`` entrypoint modes."""
    argv = _enable_sandbox(sys.argv[1:])
    if argv and argv[0] == "sandbox":
        from chrys.foundation import guard

        return guard.cli(argv[1:])
    if argv and argv[0] == _TUI_SUBPROCESS_COMMAND:
        return _run_tui(argv[1:])
    if argv in (["--version"], ["-v"]):
        sys.stdout.write(f"{__version__}\n")
        return 0
    if argv in (["--help"], ["-h"]):
        build_parser().print_help()
        return 0
    if argv and argv[0] in _PLURAL_ALIASES:
        argv = [_PLURAL_ALIASES[argv[0]], *argv[1:]]
    if argv and argv[0] == "run":
        from chrys.app.cli.run import main as run_main

        return run_main(argv[1:])
    if argv and argv[0] == "agent":
        from chrys.app.cli.profiles import agents_main

        return agents_main(argv[1:])
    if argv and argv[0] == "model":
        from chrys.app.cli.profiles import models_main

        return models_main(argv[1:])
    if argv and argv[0] == "approval":
        from chrys.app.cli.approvals import main as approvals_main

        return approvals_main(argv[1:])
    if argv and argv[0] == "hook":
        from chrys.app.cli.hooks import main as hooks_main

        return hooks_main(argv[1:])
    if argv and argv[0] == "install":
        return _run_install(argv[1:])
    if argv and argv[0] == "uninstall":
        return _run_uninstall(argv[1:])
    if argv and argv[0] == "serve":
        return _run_serve(argv[1:])
    if argv and argv[0] == "acp":
        return _run_acp(argv[1:])
    if argv and argv[0] == "trajectory":
        from chrys.app.cli.trajectory import main as trajectory_main

        return trajectory_main(argv[1:])
    if argv and argv[0] == "workflow":
        from chrys.app.cli.workflow import main as workflow_main

        return workflow_main(argv[1:])
    if argv and argv[0] == "rsi":
        from chrys.app.cli.rsi import main as rsi_main

        return rsi_main(argv[1:])
    return _run_tui(argv)


def pyapp_main() -> None:
    """PyApp entrypoint that preserves integer return codes."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
