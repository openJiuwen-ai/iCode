# ruff: noqa: T201
# (CLI launcher — print is the intended user-facing output channel)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One-command launcher for the local mock backends (dev tool, never shipped).

``python -m aixcoding.mock`` starts the base combo for manual TUI runs:

* ``model_catalog`` on :7777 with the device-code auth mock mounted on the
  same port — login + catalog + LLM chat from one origin, zero extra config
  under ``AIXCODING_ENVIRONMENT=local``;
* ``update`` on :7778.

Each mock keeps its own server instance and port; this module only
orchestrates (DIRECTORY.md §6 "package-root orchestration layer"). Ports are
fixed on purpose — the product's LOCAL profile points at them — so a taken
port fails fast naming the service, instead of silently moving and leaving
the TUI talking to nothing. That contract is the CLI's dev-facing default:
every starter also takes ``port`` (the round-trip test passes ``port=0``,
the OS-assigned ephemeral port the server factories natively support), so
tests never depend on :7777/:7778 being free. Ctrl+C stops everything that
was started.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from collections.abc import Callable

from aixcoding.mock.auth.server import DEFAULT_PORT as AUTH_PORT
from aixcoding.mock.auth.server import MockAuthConfig
from aixcoding.mock.auth.server import create_server as create_auth_server
from aixcoding.mock.model_catalog.server import DEFAULT_PORT as CATALOG_PORT
from aixcoding.mock.model_catalog.server import start as start_catalog
from aixcoding.mock.update.server import DEFAULT_PORT as UPDATE_PORT
from aixcoding.mock.update.server import create_server as create_update_server

_LOGGER = logging.getLogger("chrys.mock_launcher")

# (name, port, one-line description, starter) — started in table order.
# "auth" is the standalone form; the launcher's default covers login via the
# auth routes model_catalog mounts on :7777 (--only auth is for pure-login
# work, hence the mutual exclusion with model_catalog below).
# The starter type is ellipsis-typed: every starter takes ``port`` with a
# default (the CLI calls it bare, the round-trip test passes ``port=0``).
_Services = dict[str, tuple[int, str, Callable[..., tuple[str, Callable[[], None]]]]]


def _start_model_catalog(port: int = CATALOG_PORT) -> tuple[str, Callable[[], None]]:
    running = start_catalog(port=port, auth=MockAuthConfig(mode="auto"))

    def stop() -> None:
        running.close()

    return running.origin, stop


def _start_update(port: int = UPDATE_PORT) -> tuple[str, Callable[[], None]]:
    server = create_update_server(port=port)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="chrys-mock-update", daemon=True
    ).start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return str(server.mock_config.base_url), stop


def _start_auth(port: int = AUTH_PORT) -> tuple[str, Callable[[], None]]:
    server = create_auth_server(MockAuthConfig(mode="manual"), port=port)
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.2}, name="chrys-mock-auth", daemon=True
    ).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return origin, stop


_SERVICES: _Services = {
    "model_catalog": (CATALOG_PORT, "catalog dispatch + embedded auth + LLM chat /v1", _start_model_catalog),
    "update": (UPDATE_PORT, "release-check /csas/api/v2/releases/check", _start_update),
    "auth": (AUTH_PORT, "standalone device-code login mock", _start_auth),
}

_DEFAULT_COMBO = ("model_catalog", "update")


def _resolve_selection(only: str, all_: bool) -> list[str] | None:
    if all_ or not only:
        # --all currently equals the default combo; telemetry (:4321) joins
        # here when its mock migrates.
        return list(_DEFAULT_COMBO)
    names = [name.strip() for name in only.split(",") if name.strip()]
    unknown = [name for name in names if name not in _SERVICES]
    if unknown:
        _LOGGER.error("unknown mock(s): %s (known: %s)", ", ".join(unknown), ", ".join(_SERVICES))
        return None
    if "auth" in names and "model_catalog" in names:
        _LOGGER.error(
            "auth and model_catalog both bind :%s — pick one (model_catalog embeds the auth routes)", AUTH_PORT
        )
        return None
    return names


def _print_registry() -> None:
    print("available mocks:")
    for name, (port, description, _starter) in _SERVICES.items():
        print(f"  {name:<14} :{port}  {description}")
    print("default combo: model_catalog(:7777 incl. auth) + update(:7778); --only takes a comma-separated subset")


def main(argv: list[str] | None = None) -> int:
    """Start the selected mocks and block until interrupted. Returns an exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m aixcoding.mock",
        description="Start the local AIxCoding mock backends (base combo by default).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="start every migrated mock (currently the default combo; telemetry joins later)",
    )
    parser.add_argument("--only", metavar="NAMES", help=f"comma-separated subset of: {', '.join(_SERVICES)}")
    parser.add_argument("--list", action="store_true", help="list available mocks and exit")
    args = parser.parse_args(argv)

    if args.list:
        _print_registry()
        return 0

    names = _resolve_selection(args.only or "", args.all)
    if names is None:
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    stopped: list[tuple[str, Callable[[], None]]] = []
    for name in names:
        port, _description, starter = _SERVICES[name]
        try:
            origin, stop = starter()
        except OSError as exc:
            _LOGGER.error("cannot start %s on 127.0.0.1:%s: %s", name, port, exc)
            _LOGGER.error(
                'port taken? find the holder: netstat -ano | findstr :%s  (then tasklist /fi "PID eq <pid>")', port
            )
            for entry, entry_stop in stopped:
                entry_stop()
                _LOGGER.info("stopped %s (rolled back)", entry)
            return 1
        stopped.append((name, stop))
        _LOGGER.info("%s on %s", name, origin)

    print()
    print("Point the TUI at the mocks, e.g.:")
    print("  AIXCODING_ENVIRONMENT=local uv run aixcoding-cli")
    if "update" in names:
        print("  update: contract mock only; product wiring lands with the update batch")
    print("Ctrl+C stops everything.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        _LOGGER.info("shutting down")
    finally:
        for _name, stop in stopped:
            stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
