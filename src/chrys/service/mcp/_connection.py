# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP tool validation and connection factory."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import TYPE_CHECKING, Any

from chrys.foundation.util.env_templates import resolve_env_template_mapping
from chrys.service.mcp._http_transport import _HTTP_HEADERS_OVERRIDE, _HTTPMCPTool
from chrys.service.mcp._stdio_transport import (
    _STDIO_CWD_OVERRIDE,
    _STDIO_ENV_IS_COMPLETE,
    _STDIO_ENV_OVERRIDE,
    _SafeStdioTool,
    _StdioFailureDiagnostics,
)
from chrys.service.mcp.errors import MCPToolNameValidationError
from chrys.service.mcp.validation import (
    MCP_PROGRESSIVE_CONTROL_TOOL_NAMES,
    validate_mcp_tool_loading_policy,
    validate_mcp_tool_name_prefix,
)

if TYPE_CHECKING:
    from chrys.service.mcp.owned import MCPTool
    from chrys.service.profiles.agents.schema import MCPServerConfig


# Hard timeouts that bound how long a misbehaving MCP server can hold up the
# UI / agent build.  The MCP SDK's per-request timeout
# (``read_timeout_seconds``) defaults to ``None`` (wait forever), and a
# server that emits welcome banner text instead of a valid ``initialize``
# response will leave the client blocked indefinitely on
# ``response_stream_reader.receive()``.  These floors are injected into
# the MCP tool's ``request_timeout`` when the user didn't set one, so the
# SDK's own timeout fires from *inside* MCPTool's lifecycle-owner
# task.  We intentionally do **not** wrap ``__aenter__`` in
# ``asyncio.wait_for`` from the outside: the lifecycle owner is a
# separate ``asyncio.Task`` (see ``MCPTool._run_lifecycle_owner``) which
# ``wait_for`` cannot cancel safely (anyio cancel scopes are scoped per
# task and cross-task cancellation raises "Attempted to exit cancel scope
# in a different task").  ``MCPServerConfig.request_timeout`` (when set)
# always takes precedence — these are only the defaults applied when no
# per-server timeout is configured.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30


def _validate_config(config: MCPServerConfig) -> None:
    """Reject malformed configs before constructing an owned MCPTool."""
    if not config.name:
        raise ValueError("MCP server config requires a non-empty 'name'")
    if not isinstance(config.use_progressive_disclosure, bool):
        raise ValueError(f"MCP server {config.name!r}: 'use_progressive_disclosure' must be a boolean")
    if not isinstance(config.expose_instructions, bool):
        raise ValueError(f"MCP server {config.name!r}: 'expose_instructions' must be a boolean")
    cap = config.max_tool_result_tokens
    if not (cap is None or (type(cap) is int and (cap == 0 or cap >= 100))):
        raise ValueError(
            f"MCP server {config.name!r}: 'max_tool_result_tokens' must be null, 0, or an integer of at least 100"
        )
    if config.allowed_tools is not None and (
        not isinstance(config.allowed_tools, list) or not all(isinstance(name, str) for name in config.allowed_tools)
    ):
        raise ValueError(f"MCP server {config.name!r}: 'allowed_tools' must be a list of strings or null")
    if not isinstance(config.always_load, list) or not all(isinstance(name, str) for name in config.always_load):
        raise ValueError(f"MCP server {config.name!r}: 'always_load' must be a list of strings")
    if policy_errors := validate_mcp_tool_loading_policy(
        allowed_tools=config.allowed_tools,
        use_progressive_disclosure=config.use_progressive_disclosure,
        always_load=config.always_load,
    ):
        raise ValueError(f"MCP server {config.name!r}: {' '.join(policy_errors)}")
    prefix_error = validate_mcp_tool_name_prefix(
        config.tool_name_prefix,
        generated_suffixes=MCP_PROGRESSIVE_CONTROL_TOOL_NAMES if config.use_progressive_disclosure else (),
    )
    if prefix_error is not None:
        raise MCPToolNameValidationError(
            config.name,
            config.transport,
            violations={prefix_error},
        )
    if config.transport == "stdio":
        if not config.command:
            raise ValueError(f"MCP server {config.name!r} (stdio) requires 'command'")
    elif config.transport == "http":
        if not config.url:
            raise ValueError(f"MCP server {config.name!r} (http) requires 'url'")
    else:
        raise ValueError(f"Unknown MCP transport: {config.transport!r}")


def _build_common_kwargs(config: MCPServerConfig) -> dict[str, Any]:
    """Kwargs shared by every MCPTool subclass."""
    kwargs: dict[str, Any] = {
        "name": config.name,
        "load_prompts": config.load_prompts,
    }
    if config.description:
        kwargs["description"] = config.description
    if config.tool_name_prefix:
        kwargs["tool_name_prefix"] = config.tool_name_prefix
    if config.allowed_tools is not None:
        kwargs["allowed_tools"] = config.allowed_tools
    if config.request_timeout is not None:
        kwargs["request_timeout"] = config.request_timeout
    return kwargs


def _create_mcp_tool(config: MCPServerConfig) -> MCPTool:
    """Create the appropriate MCPTool subclass from a profile config."""
    _validate_config(config)
    common = _build_common_kwargs(config)

    if config.transport == "stdio":
        env = _STDIO_ENV_OVERRIDE.get()
        env_is_complete = _STDIO_ENV_IS_COMPLETE.get()
        if env is None:
            env = resolve_env_template_mapping(config.env, location=f"MCP server {config.name!r} env")
            env_is_complete = False
        kwargs: dict[str, Any] = {
            **common,
            "command": config.command,
            "args": config.args or None,
            "env": env if env_is_complete else env or None,
            "encoding": config.encoding,
            "env_is_complete": env_is_complete,
        }
        cwd = _STDIO_CWD_OVERRIDE.get()
        if cwd is not None:
            kwargs["cwd"] = cwd
        return _SafeStdioTool(**kwargs)

    # transport == "http" (validated above).  Static headers are attached by
    # ``_HTTPMCPTool``'s same-origin request hook, so they reach every request,
    # ``initialize`` and ``list_tools`` included.
    headers = _HTTP_HEADERS_OVERRIDE.get()
    if headers is None:
        headers = (
            resolve_env_template_mapping(config.headers, location=f"MCP server {config.name!r} headers")
            if config.resolve_header_templates
            else dict(config.headers)
        )
    return _HTTPMCPTool(
        **common,
        url=config.url,
        terminate_on_close=config.terminate_on_close,
        verify_ssl=config.verify_ssl,
        bypass_proxy=config.bypass_proxy,
        headers=headers or None,
        resolve_commands=config.resolve_header_templates,
    )


def _create_tool_with_timeout_floor(
    config: MCPServerConfig,
    default_timeout: int,
    *,
    resolved_stdio_env: dict[str, str] | None = None,
    stdio_cwd: str | None = None,
    resolved_http_headers: dict[str, str] | None = None,
) -> Any:
    """Build the MCP tool, injecting the timeout floor when none is configured.

    We bound a hung connect/initialize by setting the MCP tool's
    ``request_timeout`` (used as ``ClientSession.read_timeout_seconds``)
    — the SDK's own ``anyio.fail_after`` then fires from inside the
    owned MCPTool's lifecycle-owner task, where ``_safe_close_exit_stack``
    runs cleanup safely.  A user-configured ``request_timeout`` always
    wins; we only inject when ``config.request_timeout is None``.

    Why not ``asyncio.wait_for(__aenter__, ...)`` from outside?  The
    lifecycle owner runs in a separate task started by
    ``MCPTool._run_lifecycle_owner`` and uses anyio cancel scopes
    which are pinned to the owning task.  Cross-task cancellation
    raises ``RuntimeError("Attempted to exit cancel scope in a
    different task than it was entered in")`` and leaves the inner
    coroutine still running — defeating both the timeout and cleanup.
    """
    effective_config = (
        dataclasses.replace(config, request_timeout=default_timeout) if config.request_timeout is None else config
    )
    env_token = _STDIO_ENV_OVERRIDE.set(resolved_stdio_env)
    cwd_token = _STDIO_CWD_OVERRIDE.set(stdio_cwd)
    headers_token = _HTTP_HEADERS_OVERRIDE.set(resolved_http_headers)
    complete_token = _STDIO_ENV_IS_COMPLETE.set(resolved_stdio_env is not None)
    try:
        return _create_mcp_tool(effective_config)
    finally:
        _STDIO_ENV_IS_COMPLETE.reset(complete_token)
        _HTTP_HEADERS_OVERRIDE.reset(headers_token)
        _STDIO_CWD_OVERRIDE.reset(cwd_token)
        _STDIO_ENV_OVERRIDE.reset(env_token)


def _banner_lines_from(tool: Any) -> list[str]:
    """Extract banner diagnostics from a stdio tool, empty list otherwise."""
    return list(getattr(tool, "dropped_banner_lines", []) or [])


def _stdio_failure_diagnostics_from(tool: Any) -> _StdioFailureDiagnostics:
    """Snapshot bounded process diagnostics from Chrys's stdio tool."""
    if not isinstance(tool, _SafeStdioTool):
        return _StdioFailureDiagnostics()
    diagnostics = tool._process_diagnostics
    return _StdioFailureDiagnostics(
        stderr_tail=diagnostics.stderr_tail,
        stderr_dropped_bytes=diagnostics.stderr_dropped_bytes,
        process_exit_code=diagnostics.exit_code,
        resolved_executable=diagnostics.resolved_executable,
        effective_cwd=diagnostics.effective_cwd,
    )


def _is_timeout_cause(exc: BaseException) -> bool:
    """True if the exception chain contains an MCP/anyio read-timeout signal.

    The MCP SDK's ``ClientSession.send_request`` translates an
    ``anyio.fail_after`` timeout into ``McpError(ErrorData(code=408, ...))``.
    For other paths (e.g. cancelled transport) we also accept plain
    ``TimeoutError`` / ``asyncio.TimeoutError``.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, (TimeoutError, asyncio.TimeoutError)):
            return True
        data = getattr(cur, "error", None)
        if getattr(data, "code", None) == 408:
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False
