# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manage the opt-in to reusing the user's Chrome login."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .bridge.protocol import PROTOCOL_VERSION
from .config import CHROME_DEVTOOLS_PREFERENCE, CHROME_LOGIN_PREFERENCE, BrowserConfig, BrowserEnv, ConfigError

_REMOTE_DEBUGGING = "chrome://inspect/#remote-debugging"


async def _prepare_devtools(on_wait: Callable[[], None] | None = None) -> None:
    """Open Chrome's own opt-in screen and wait for its local endpoint to appear."""
    from .bridge.install import _launch, chrome_devtools_ws, default_chrome_executable

    try:
        chrome_devtools_ws()
        return
    except RuntimeError:
        pass
    chrome = default_chrome_executable()
    if chrome is None:
        raise RuntimeError("Install Chrome 144 or newer to use Chrome login reuse without an extension.")
    _launch([str(chrome), _REMOTE_DEBUGGING])
    if on_wait is not None:
        on_wait()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        try:
            chrome_devtools_ws()
            return
        except RuntimeError:
            await asyncio.sleep(0.4)
    raise RuntimeError(f"Chrome did not enable remote debugging. Check {_REMOTE_DEBUGGING} and try again.")


def _save_preference(selected: Path, previous: Path) -> None:
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.touch()
    previous.unlink(missing_ok=True)


async def remember_native_login(on_wait: Callable[[], None] | None = None, *, keep_bridge: bool = False) -> None:
    source = BrowserEnv()
    cfg = BrowserConfig.from_env()
    if keep_bridge and cfg.bridge and not cfg.broker:
        return
    if cfg.broker or cfg.cdp_url or source.get("BRIDGE") is not None:
        raise RuntimeError("Clear the Browser Use backend environment override before connecting Chrome.")
    await _prepare_devtools(on_wait)
    state_dir = source.state_dir()
    _save_preference(state_dir / CHROME_DEVTOOLS_PREFERENCE, state_dir / CHROME_LOGIN_PREFERENCE)


def main(argv: list[str] | None = None, *, prog: str = "icode-browser-use") -> int:
    parser = argparse.ArgumentParser(prog=prog, description="Manage Chrome login reuse for Browser Use.")
    commands = parser.add_subparsers(dest="action")
    connect = commands.add_parser("connect", help="Set up Chrome login reuse for new browsers")
    choices = connect.add_mutually_exclusive_group()
    choices.add_argument("--chrome", action="store_true", help="Use Chrome's own approval flow without an extension")
    choices.add_argument("--bridge", action="store_true", help="Use an already installed Chrome Bridge extension")
    commands.add_parser("disconnect", help="Clear the saved choice to reuse Chrome")
    commands.add_parser("status", help="Show the backend new browsers will use")
    commands.add_parser("update-bridge", help="Update a previously installed Chrome Bridge")
    args = parser.parse_args(argv)
    action = args.action
    source = BrowserEnv()
    preference = source.state_dir() / CHROME_LOGIN_PREFERENCE
    devtools = source.state_dir() / CHROME_DEVTOOLS_PREFERENCE
    try:
        if action == "update-bridge":
            from .bridge.install import update_extension

            target = update_extension()
            print(f"Chrome Bridge {__version__} copied to {target}.")
            print(f"Reload iCode Browser Bridge at chrome://extensions, then run {prog} connect --bridge.")
            return 0
        if action == "disconnect":
            preference.unlink(missing_ok=True)
            devtools.unlink(missing_ok=True)
            print("Saved Chrome login reuse disabled. Existing browser handles keep their backend.")
            if source.get("BRIDGE") is not None:
                print("The ICODE_BROWSER_BRIDGE environment setting still applies.")
            return 0
        cfg = BrowserConfig.from_env()
        if action != "connect":
            backend = "Chrome (native connection)" if cfg.chrome_devtools else cfg.backend
            print(f"Browser Use backend for new browsers: {backend}")
            saved = preference.is_file() or devtools.is_file()
            print(f"Saved Chrome login reuse: {'enabled' if saved else 'disabled'}")
            return 0
        if cfg.broker or cfg.cdp_url or (source.get("BRIDGE") is not None and not cfg.bridge):
            print("Clear the Browser Use backend environment override before connecting Chrome.", file=sys.stderr)
            return 1

        from .bridge.install import registered_manifest

        has_registered_bridge = any(
            (reg := registered_manifest(name)) is not None and reg.is_file() for name in ("chrome", "chromium", "edge")
        )
        use_native = args.chrome or (
            not (args.bridge or cfg.bridge) and (cfg.chrome_devtools or not has_registered_bridge)
        )
        if use_native:
            asyncio.run(
                remember_native_login(
                    lambda: print("Turn on Allow remote debugging in Chrome. Waiting up to 60 seconds…", flush=True)
                )
            )
            print("Chrome login reuse saved. Approve Chrome's prompt when Browser Use first connects.")
            return 0

        from .diagnose import probe_bridge

        result, problems = probe_bridge()
        if not result["connected"]:
            for problem in problems:
                print(problem["message"], file=sys.stderr)
                print(problem["advice"], file=sys.stderr)
            return 1
        if result["protocol"] != PROTOCOL_VERSION:
            print("Chrome Bridge uses a different protocol. Update and reload the extension.", file=sys.stderr)
            return 1
        version = result.get("extension_version") or "unknown"
        print(f"Running Chrome Bridge extension version: {version}")
        if "page_log_gated" not in result.get("capabilities", ()):
            print(f"Browser logs require an updated Bridge. Run {prog} update-bridge and reload the extension.")
        _save_preference(preference, devtools)
    except (ConfigError, OSError, RuntimeError) as error:
        print(f"Could not configure Browser Use: {error}", file=sys.stderr)
        return 1
    print("Chrome Bridge connected. New browser handles will reuse that Chrome profile.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(prog="icode-browser-use"))
