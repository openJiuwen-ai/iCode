# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Top-level ``chrys`` command dispatcher."""

from __future__ import annotations

import argparse
import sys
from typing import Final

from chrys import __version__
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


def _run_tui(argv: list[str]) -> int:
    """Run the TUI with *argv* as its argument list."""
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
    parser = argparse.ArgumentParser(
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
            "  acp         Run an Agent Client Protocol stdio server\n"
            f"  serve       Host the {APP_DISPLAY_NAME} TUI in a browser\n"
            f"  install     Install {APP_DISPLAY_NAME} to PATH\n"
            f"  uninstall   Remove {APP_DISPLAY_NAME} (--purge also deletes your data)\n\n"
            "agent, model, approval, hook, workflow and trajectory also work in the plural;\n"
            "agent, model, approval, hook and workflow list when given no subcommand.\n\n"
            f"Default: '{APP_COMMAND}' launches the TUI. Run '{APP_COMMAND} <command> --help' for command options."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument("-v", "--version", action="store_true", help=f"Print the {APP_DISPLAY_NAME} version and exit")

    tui_options = parser.add_argument_group("TUI options")
    tui_options.add_argument(
        "--launch-manifest", metavar="FILE", help="Receive a request-correlated interactive launch manifest"
    )
    tui_options.add_argument("-s", "--session", metavar="SESSION", help="Restore the given session id after startup")
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
    tui_options.add_argument("-C", "--workdir", metavar="DIR", help="Start the TUI in the given working directory")

    commands = parser.add_argument_group("commands")
    commands.add_argument(
        "command",
        metavar="command",
        nargs="?",
        help="Command to run; anything else is passed to the TUI",
    )
    return parser


def main() -> int:
    """Dispatch ``chrys`` entrypoint modes."""
    argv = sys.argv[1:]
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
    if argv and argv[0] == "launch":
        from chrys.app.cli.interactive_launch import main as launch_main

        return launch_main(argv[1:])
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
    return _run_tui(argv)


def pyapp_main() -> None:
    """PyApp entrypoint that preserves integer return codes."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
