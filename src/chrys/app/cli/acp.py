# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP stdio server command."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import cast

import acp as acp_sdk
from acp.agent.connection import AgentSideConnection
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.stdio import stdio_streams

from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionManager
from chrys.app.acp.transport import AbortableMessageSender
from chrys.app.cli.launch_cwd import launch_cwd_missing_message
from chrys.app.features.buddy.lifecycle import on_successful_turn as on_buddy_successful_turn
from chrys.foundation.branding import APP_COMMAND, APP_DISPLAY_NAME
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.config.spec import Source
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.util.once_close import finish_close
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import resolve_for_agent


def build_parser() -> argparse.ArgumentParser:
    """Build the ``chrys acp`` argument parser."""
    parser = argparse.ArgumentParser(
        prog=f"{APP_COMMAND} acp",
        description=f"Run {APP_DISPLAY_NAME} as an Agent Client Protocol stdio server.",
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help", action="help", default=argparse.SUPPRESS, help="Show this help message and exit"
    )
    parser.add_argument("-a", "--agent", default="Code", help="Agent profile id, name, or display name to expose")
    parser.add_argument(
        "--approval",
        choices=["manual", "auto", "bypass"],
        default="manual",
        help="Initial approval mode for ACP sessions",
    )
    parser.add_argument(
        "-C",
        "--workdir",
        metavar="DIR",
        dest="cwd",
        default=None,
        help="Default working directory for ACP requests that omit cwd",
    )
    parser.add_argument(
        "--ask-user-timeout",
        metavar="SECONDS",
        type=int,
        default=None,
        help="Seconds to wait for ask_user replies; omit or pass <=0 to wait indefinitely (client owns timing)",
    )
    return parser


def _configure_logging() -> None:
    """Route logs to stderr so stdout stays valid ACP JSON-RPC."""
    handler = logging.StreamHandler(sys.stderr)
    # The stderr handler must filter at WARNING itself: the root *logger*
    # level only gates records emitted at the root — it does not filter
    # records propagated from child loggers that already accepted them.
    # ``setup_otel()`` raises the ``chrys`` logger to INFO, which would
    # otherwise spill the per-tool INFO lines (chrys.kernel.loop)
    # onto ACP stderr.  The OTel root ``LoggingHandler`` (level NOTSET)
    # still collects those INFO records for the logs sink.
    handler.setLevel(logging.WARNING)
    logging.basicConfig(handlers=[handler], level=logging.WARNING)


def _prepare_runtime(process_cwd: str | None = None) -> LoadedSettings:
    """Load environment, apply runtime patches, and return settings."""
    bootstrap = bootstrap_runtime(dotenv_override=True, dotenv_cwd=process_cwd, configure_stdio=True)
    for warning in (*bootstrap.warnings, *settings_warning_events(bootstrap.loaded)):
        sys.stderr.write(f"Warning: {warning.message}\n")
    return bootstrap.loaded


def _resolve_cwd(cwd: str) -> str:
    path = Path(cwd).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        msg = f"Working directory does not exist: {cwd}"
        raise FileNotFoundError(msg) from exc
    if not resolved.is_dir():
        msg = f"Working directory is not a directory: {resolved}"
        raise NotADirectoryError(msg)
    return str(resolved)


def _initial_vision(
    *,
    settings: Settings,
    profile_name: str,
    agent_registry: AgentProfileRegistry,
    model_registry: ModelProfileRegistry,
) -> bool:
    profile = agent_registry.resolve_selector(profile_name)
    if profile is None:
        return False
    return resolve_for_agent(model_registry, settings, profile).vision


async def run_command(args: argparse.Namespace) -> int:
    """Execute parsed ``chrys acp`` args."""
    process_cwd = _resolve_cwd(args.cwd) if args.cwd else None
    loaded = _prepare_runtime(process_cwd)
    # ACP clients own the ask_user interaction lifetime, so default to no
    # backend timeout; a positive --ask-user-timeout opts back into a bound.
    ask_user_timeout = args.ask_user_timeout if args.ask_user_timeout and args.ask_user_timeout > 0 else None
    loaded = loaded.overlay(Source.CLI, ask_user_timeout_seconds=ask_user_timeout)
    settings = loaded.settings
    agent_registry = AgentProfileRegistry()
    model_registry = ModelProfileRegistry()
    agent_registry.load_all()
    model_registry.load_all()
    manager = AcpSessionManager(
        loaded_settings=loaded,
        profile_name=args.agent,
        approval_mode=ApprovalMode.from_string(args.approval),
        process_cwd=process_cwd,
        agent_registry=agent_registry,
        model_registry=model_registry,
        on_successful_turn=on_buddy_successful_turn,
    )
    initial_vision = _initial_vision(
        settings=settings,
        profile_name=args.agent,
        agent_registry=agent_registry,
        model_registry=model_registry,
    )
    server = ChrysAcpServer(
        manager,
        initial_vision=initial_vision,
        permission_timeout_seconds=settings.approval_timeout_seconds,
    )
    try:
        await _serve_agent(server)
    finally:
        await manager.shutdown()
    return 0


async def _serve_agent(server: ChrysAcpServer) -> None:
    """Own the transport until its in-flight requests have drained."""
    reader, writer = await stdio_streams(limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES)
    # The SDK dispatches structurally; unadvertised optional handlers are absent.
    connection = AgentSideConnection(
        cast(acp_sdk.Agent, server),
        writer,
        reader,
        listening=False,
        use_unstable_protocol=True,
        sender_factory=AbortableMessageSender,
    )
    try:
        await connection.listen()
    finally:
        # Closing cancels SDK handlers and releases their prompt locks before
        # manager.shutdown can wait on them, including after Ctrl-C or EOF.
        await finish_close(asyncio.create_task(connection.close()))


def _exception_message(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc) or type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``chrys acp``."""
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if (missing_cwd := launch_cwd_missing_message(workdir_flag=True, workdir=args.cwd)) is not None:
        sys.stderr.write(f"Error: {missing_cwd}\n")
        return 1
    try:
        return asyncio.run(run_command(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        sys.stderr.write(f"Error: {_exception_message(exc)}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
