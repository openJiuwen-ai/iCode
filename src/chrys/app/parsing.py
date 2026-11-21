# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared command-line parsing helpers."""

from __future__ import annotations

import argparse
from copy import copy
from typing import Never

from chrys.foundation.i18n.formatting import sanitize_legacy_scalar

WORKDIR_OPTIONS = ("-C", "--workdir", "--w", "--wo", "--wor", "--work")


class WorkspaceHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep the workdir compatibility aliases out of help."""

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if tuple(action.option_strings) == WORKDIR_OPTIONS:
            action = copy(action)
            action.option_strings = list(WORKDIR_OPTIONS[:2])
        return super()._format_action_invocation(action)


class SanitizingArgumentParser(argparse.ArgumentParser):
    """Argument parser that sanitizes attacker-controlled error details."""

    def error(self, message: str) -> Never:
        message = message.replace("/".join(WORKDIR_OPTIONS), "-C")
        super().error(sanitize_legacy_scalar(message))


def add_help_option(parser: argparse.ArgumentParser) -> None:
    """Give *parser*, built with ``add_help=False``, the usual ``-h``/``--help`` with a capitalized description."""
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )


def default_to_list(argv: list[str]) -> list[str]:
    """*argv* for a command group with ``list`` filled in when no subcommand was named.

    A group's arguments name no subcommand when they are empty or start with an
    option other than help, so ``workflow --json`` means ``workflow list --json``
    while ``workflow --help`` still shows the group's own help; after ``--`` the
    subcommand is named next, as in ``workflow -- run demo``.
    """
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--")):
        return ["list", *argv]
    return argv
