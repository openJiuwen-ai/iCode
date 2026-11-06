# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP adapter error types."""

from __future__ import annotations

from collections.abc import Collection, Mapping

from chrys.foundation.errors import ErrorKind, classify_error, clean_error_message, iter_explicit_graph
from chrys.service.mcp._stdio_transport import (
    MAX_BANNER_LINE_CHARS,
    MAX_STDIO_STDERR_BYTES_CAPTURED,
    _clean_stdio_stderr,
    _display_path,
    _stderr_preview,
)

_DIAGNOSTIC_MAX_CHARS = 300
_FAILED_TO_CONNECT = "failed to connect"
_DIAGNOSTICS = {
    ErrorKind.NO_ROUTE: "no route to the server",
    ErrorKind.HOST_UNREACHABLE: "the server's host is unreachable",
    ErrorKind.DNS_FAILED: "the server's host name could not be resolved",
    ErrorKind.CONNECTION_REFUSED: "the connection was refused",
    ErrorKind.CONNECT_TIMEOUT: "connecting timed out",
    ErrorKind.READ_TIMEOUT: "the server did not answer in time",
    ErrorKind.WRITE_TIMEOUT: "sending to the server timed out",
    ErrorKind.CONNECTION_LOST: "the connection was lost",
    ErrorKind.PROXY_REJECTED: "the proxy could not reach the server",
    ErrorKind.PROXY_AUTH_FAILED: "the proxy rejected the credentials",
    ErrorKind.TLS_FAILED: "the secure connection failed",
    ErrorKind.INVALID_ENDPOINT: "the server address is invalid",
    ErrorKind.AUTH_FAILED: "the server rejected the credentials",
    ErrorKind.RATE_LIMITED: "the server is limiting requests",
    ErrorKind.SERVER_ERROR: "the server reported an error",
}


def mcp_failure_diagnostic(error: BaseException) -> str:
    """One line saying why a connection failed, fit for a user-facing report.

    A fixed phrase for the kind of failure, never the failure's own text: what a
    server, a library or a URL says can repeat a credential, and no list of known
    values covers one a command supplied. The log keeps the full error.
    """
    if isinstance(error, MCPToolConfigurationError) and error.cause is not None:
        # Chrys's own words, naming the tools to fix.
        return clean_error_message(error.cause).partition("\n")[0][:_DIAGNOSTIC_MAX_CHARS]
    cause = error.cause if isinstance(error, MCPConnectionError) else error
    if cause is None:
        return _FAILED_TO_CONNECT
    if (
        isinstance(error, MCPConnectionError)
        and error.transport == "stdio"
        and any(isinstance(node, FileNotFoundError | PermissionError) for node in iter_explicit_graph(cause))
    ):
        text = "the server command could not be started"
    else:
        text = _DIAGNOSTICS.get(classify_error(cause).kind, _FAILED_TO_CONNECT)
    if isinstance(error, MCPConnectionError) and error.process_exit_code is not None:
        text = f"{text} (exit code {error.process_exit_code})"
    return text


class MCPConnectionError(RuntimeError):
    """Raised when an MCP server fails to connect, initialize, or list tools.

    ``banner_lines`` carries any non-JSON output the stdio child wrote to
    its stdout before initialization (captured by ``tolerant_stdio_client``).
    Surfaced in the error string so the user can see at a glance that a
    server is emitting a banner instead of speaking JSON-RPC.

    Stdio process failures may also carry a bounded ``stderr_tail``,
    ``process_exit_code``, ``resolved_executable`` and ``effective_cwd``.
    Stderr is captured separately from stdout so server diagnostics can never
    corrupt the JSON-RPC protocol stream; the message previews only the last
    :data:`MAX_STDIO_STDERR_PREVIEW_LINES` lines of it.
    """

    def __init__(
        self,
        server_name: str,
        transport: str,
        cause: BaseException | None = None,
        banner_lines: list[str] | None = None,
        *,
        failure_summary: str = "failed to connect",
        stderr_tail: str = "",
        stderr_dropped_bytes: int = 0,
        process_exit_code: int | None = None,
        resolved_executable: str = "",
        effective_cwd: str = "",
    ) -> None:
        self.server_name = server_name
        self.transport = transport
        self.cause = cause
        # Truncate each line so a single very long banner row (e.g. a
        # serialized stack trace) can't blow up the error message.
        self.banner_lines: list[str] = (
            [
                (line[:MAX_BANNER_LINE_CHARS] + "…") if len(line) > MAX_BANNER_LINE_CHARS else line
                for line in banner_lines
            ]
            if banner_lines
            else []
        )
        self.stderr_tail = _clean_stdio_stderr(stderr_tail)[-MAX_STDIO_STDERR_BYTES_CAPTURED:]
        self.stderr_dropped_bytes = max(0, stderr_dropped_bytes)
        self.process_exit_code = process_exit_code
        self.resolved_executable = resolved_executable
        self.effective_cwd = effective_cwd
        detail = f": {clean_error_message(cause)}" if cause is not None else ""
        message = f"MCP server {server_name!r} ({transport}) {failure_summary}{detail}"
        context_lines = []
        if self.resolved_executable:
            context_lines.append(f"Executable: {_display_path(self.resolved_executable)}")
        if self.effective_cwd:
            context_lines.append(f"Working directory: {_display_path(self.effective_cwd)}")
        if context_lines:
            message = f"{message}\n\n" + "\n".join(context_lines)
        if self.process_exit_code == 0:
            message = f"{message}\n\nServer process exited normally (code 0) before completing the MCP handshake."
        elif self.process_exit_code is not None:
            message = f"{message}\n\nServer process exit code: {self.process_exit_code}."
        if self.stderr_tail:
            preview = _stderr_preview(self.stderr_tail, dropped_bytes=self.stderr_dropped_bytes)
            message = f"{message}\n\n{preview}"
        if self.banner_lines:
            preview = "\n  ".join(self.banner_lines)
            message = f"{message}\n\nServer emitted non-JSON output before initialization:\n  {preview}"
        super().__init__(message)


class MCPToolConfigurationError(MCPConnectionError):
    """Base class for deterministic MCP tool-namespace configuration errors."""


class MCPToolNameCollisionError(MCPToolConfigurationError):
    """Raised when a permitted MCP tool namespace is not globally unique."""

    def __init__(
        self,
        server_name: str,
        transport: str,
        *,
        conflicting_names: Collection[str],
        conflict_with: str,
        guidance: str,
    ) -> None:
        self.conflicting_names = tuple(sorted(set(conflicting_names)))
        self.conflict_with = conflict_with
        names = ", ".join(self.conflicting_names)
        cause = ValueError(f"permitted tool name collision with {conflict_with}: {names}. {guidance}")
        super().__init__(
            server_name,
            transport,
            cause,
            failure_summary="has invalid tool configuration",
        )


class MCPToolNameAmbiguityError(MCPToolConfigurationError):
    """Raised when an ``allowed_tools``/``always_load`` name selects more than one tool."""

    def __init__(
        self,
        server_name: str,
        transport: str,
        *,
        matches_by_name: Mapping[str, Collection[tuple[str, str | None]]],
    ) -> None:
        """*matches_by_name* maps each ambiguous entry to ``(original name, name selecting only that tool)``
        pairs, one per tool it matches; the second item is None when no name selects that tool alone.
        """
        self.matches_by_name = {name: tuple(sorted(matches)) for name, matches in sorted(matches_by_name.items())}
        details = "; ".join(
            f"'{name}' matches " + ", ".join(_describe_ambiguous_match(original, sole) for original, sole in matches)
            for name, matches in self.matches_by_name.items()
        )
        message = f"a configured tool name selects more than one tool: {details}."
        if any(sole is None for matches in self.matches_by_name.values() for _original, sole in matches):
            message += " Configure a different Tool Name Prefix to tell apart a tool that no name selects alone."
        cause = ValueError(message)
        super().__init__(
            server_name,
            transport,
            cause,
            failure_summary="has invalid tool configuration",
        )


def _describe_ambiguous_match(original: str, sole: str | None) -> str:
    if sole is None:
        return f"the server's '{original}' (no name selects only it)"
    return f"the server's '{original}' (write '{sole}' to select only it)"


class MCPToolNameValidationError(MCPToolConfigurationError):
    """Raised when an exposed MCP tool name is not accepted by model providers."""

    def __init__(
        self,
        server_name: str,
        transport: str,
        *,
        violations: Collection[str],
    ) -> None:
        self.violations = tuple(sorted(set(violations)))
        cause = ValueError("provider-incompatible MCP tool name: " + "; ".join(self.violations))
        super().__init__(
            server_name,
            transport,
            cause,
            failure_summary="has invalid tool configuration",
        )
