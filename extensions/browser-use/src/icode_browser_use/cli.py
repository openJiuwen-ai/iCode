# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``icode-browser-use``: diagnostics plus folder-package install/uninstall.

``doctor`` is the read-only overall report (see :mod:`icode_browser_use.diagnose`).
``install`` copies an exported folder package into
``<config_dir>/extensions/browser`` for iCode's own extension loader, and
refuses when the wheel (as visible to this interpreter) or the single-file
layout already provides the same id. ``uninstall`` removes only the extension's
own folder or file: browser profiles, logins and action journals live in the
state directory and always stay. When the wheel is also installed, uninstall
still removes the folder package and says how to remove the distribution.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__, diagnose, distribution

HELP = f"""icode-browser-use {__version__}

Usage:
  icode-browser-use doctor [--json] [--connect] [--bridge] [--config-dir PATH]
  icode-browser-use install [--source PATH] [--config-dir PATH] [--replace]
  icode-browser-use uninstall [--config-dir PATH]

doctor     report extension discovery, host API, configuration, browser,
           state folder and Bridge/Broker status; read-only unless --connect
           (TCP probe) or --bridge (actively wait for the extension)
install    copy an exported folder package into <config_dir>/extensions/browser
uninstall  remove that folder package only (profiles and logins are kept)

Exit status: doctor and install return 0 on success; uninstall returns 1 when
the browser id is still installed afterwards (by a wheel this command cannot
remove) or when removing failed.
"""


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    command, rest = args[0], args[1:]
    if command == "doctor":
        return diagnose.main(rest)
    if command in ("install", "uninstall"):
        return _pack(command, rest)
    print(f"icode-browser-use: unknown command {command!r}\n{HELP}", file=sys.stderr)
    return 2


def _pack(command: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"icode-browser-use {command}",
        description="Copy a folder package into the iCode extensions folder"
        if command == "install"
        else "Remove the installed folder package; profiles and logins are kept",
    )
    if command == "install":
        parser.add_argument("--source", type=Path, default=None, help="exported extensions/browser folder")
        parser.add_argument("--replace", action="store_true", help="replace an already installed copy")
    parser.add_argument("--config-dir", type=Path, default=None, help="iCode config dir (default: ~/.chrys)")
    args = parser.parse_args(argv)
    if command == "install":
        source = args.source or _own_pack()
        if source is None:
            print(
                "icode-browser-use install: this runs from an installed distribution, which iCode already loads; "
                "pass --source <exported extensions/browser folder> to install a folder package instead",
                file=sys.stderr,
            )
            return 2
        try:
            target = distribution.install_pack(source, config_dir=args.config_dir, replace=args.replace)
        except (ValueError, OSError) as exc:
            print(f"icode-browser-use install: {exc}", file=sys.stderr)
            return 1
        print(f"installed: {target}")
        print(f"enable it for an agent in iCode, then run: {_doctor_hint()}")
        return 0
    try:
        removed = distribution.uninstall_pack(config_dir=args.config_dir)
    except OSError as exc:
        print(f"icode-browser-use uninstall: {exc}", file=sys.stderr)
        return 1
    if removed:
        print("removed: " + ", ".join(str(path) for path in removed))
        print("kept: browser profiles, logins and action journals in the state folder")
    else:
        print("icode-browser-use uninstall: no folder package was installed")
    if distribution.wheel_entry_point() is not None:
        print(
            "icode-browser-use uninstall: the icode-browser-use distribution is still installed in this Python "
            "(uninstall it with your Python installer, for example: uv pip uninstall icode-browser-use)",
            file=sys.stderr,
        )
        return 1
    return 0


def _doctor_hint() -> str:
    """The diagnostics command that exists in this install: the pack ships ``doctor.py``, the wheel a script."""
    if distribution.loaded_from(__name__) == "folder":
        return "python doctor.py (from the extension folder)"
    return "icode-browser-use doctor"


def _own_pack() -> Path | None:
    """The folder package this module runs from, or ``None`` inside a distribution."""
    if distribution.loaded_from(__name__) != "folder":
        return None
    return Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    raise SystemExit(main())
