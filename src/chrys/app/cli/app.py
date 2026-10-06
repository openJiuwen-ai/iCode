# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Top-level ``chrys`` command dispatcher."""

from __future__ import annotations

import argparse
import sys

from chrys import __version__
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME

_TUI_SUBPROCESS_COMMAND = "__tui_subprocess__"


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
            "  agents      List available agent profiles\n"
            "  models      List available model profiles\n"
            "  acp         Run an Agent Client Protocol stdio server\n"
            "  buddy       Local Buddy JSON bridge\n"
            f"  serve       Host the {APP_DISPLAY_NAME} TUI in a browser\n"
            "  trajectory  Export recorded trajectory analytics (perfetto/json/csv)\n"
            "  workflow    List, validate and run workflows headlessly\n"
            f"  install     Install {APP_DISPLAY_NAME} to PATH\n\n"
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
    if argv and argv[0] == "run":
        from chrys.app.cli.run import main as run_main

        return run_main(argv[1:])
    if argv and argv[0] == "agents":
        from chrys.app.cli.profiles import agents_main

        return agents_main(argv[1:])
    if argv and argv[0] == "models":
        from chrys.app.cli.profiles import models_main

        return models_main(argv[1:])
    if argv and argv[0] == "install":
        return _run_install(argv[1:])
    if argv and argv[0] == "serve":
        return _run_serve(argv[1:])
    if argv and argv[0] == "acp":
        return _run_acp(argv[1:])
    if argv and argv[0] == "buddy":
        from chrys.app.cli.buddy import main as buddy_main

        return buddy_main(argv[1:])
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
