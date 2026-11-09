# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the MCP import dialog says about each server, apart from its widgets.

Turns importer results (codes, setting paths) into localized messages, and a
ticked candidate into the server the MCP tab adds.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.service.mcp.importers import ImportWarning, McpImportCandidate, McpSourceError, Problem, SourceErrorKind
from chrys.service.mcp.importers import WarningKind as Kind

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.foundation.i18n import MessageRef
    from chrys.service.profiles.agents.schema import MCPServerConfig

_NOT_IMPORTED = msg(
    "tui.mcp.import.warning.not_imported",
    fallback="These {agent} settings are not imported. Set up any the server needs in {app} by hand:",
)
_AUTH_NOT_IMPORTED = msg(
    "tui.mcp.import.warning.auth_not_imported",
    fallback="Sign-in from {agent} is not imported. If the server requires authentication, add the headers it expects after import.",
)
_VARIABLE_REQUIRED = msg(
    "tui.mcp.import.warning.variable_required",
    fallback="Set these environment variables to nonempty values before starting {app}:",
)
_TOOLS_RESTRICTED = msg(
    "tui.mcp.import.warning.tools_restricted",
    fallback="{agent} limits this server's tools in a way {app} can't express, so it is imported with no tools. Choose its tools in “Available Tool Scope” after import.",
)
_UNSUPPORTED_CONNECTION = msg(
    "tui.mcp.import.problem.unsupported_connection",
    fallback="This server connects over {transport}; {app} supports only STDIO and Streamable HTTP. If the server also offers one of these, add it manually.",
)
_REMOTE_ENVIRONMENT = msg(
    "tui.mcp.import.problem.remote_environment",
    fallback="This server is set to run in a remote {agent} environment; {app} starts MCP servers only on this computer. Add a configuration that runs on this computer manually.",
)
_VARIABLE_NOT_SUPPORTED = msg(
    "tui.mcp.import.problem.variable_not_supported",
    fallback="{app} doesn't support environment variables in URLs, commands or arguments. Add the server manually and enter the actual value:",
)
_VALUE_FROM_FILE_OR_COMMAND = msg(
    "tui.mcp.import.problem.value_from_file_or_command",
    fallback="{agent} reads this value from a file or a command, which the import doesn't do. Add the server manually and enter the value, reference an environment variable, or get the value from a command:",
)
_VALUE_NOT_REPRESENTABLE = msg(
    "tui.mcp.import.problem.value_not_representable",
    fallback="The value of this {agent} setting can't be used in {app} as written. Add the server manually; the User Guide (F8) lists common causes. The setting:",
)
_UNREADABLE = msg("tui.mcp.import.error.unreadable", fallback="Couldn't read {path}.")
_UNPARSABLE = msg(
    "tui.mcp.import.error.unparsable", fallback="Couldn't parse {path}. Correct the file, then open import again."
)
_UNPARSABLE_AT = msg(
    "tui.mcp.import.error.unparsable_at",
    fallback="Couldn't parse {path}: syntax error at line {line}, column {column}. Correct it, then open import again.",
)
_INVALID = msg("tui.mcp.import.error.invalid", fallback="{path} isn't a valid {agent} configuration.")
_INVALID_FIELD = msg("tui.mcp.import.error.invalid_field", fallback="{path} has an invalid setting: {field}.")
_ORIGIN = msg("tui.mcp.import.origin", fallback="Imported from {agent} ({path}).")
# How the import names a connection type it can't use; any other is shown in capitals.
_TRANSPORT_NAMES = {"sse": "SSE", "ws": "WebSocket"}


def warning_text(warning: ImportWarning, agent: str) -> MessageRef:
    """The warning's sentence; the settings or variables it names (``warning.items``) follow it."""
    match warning.kind:
        case Kind.NOT_IMPORTED:
            return _NOT_IMPORTED.bind(agent=agent, app=APP_DISPLAY_NAME)
        case Kind.AUTH_NOT_IMPORTED:
            return _AUTH_NOT_IMPORTED.bind(agent=agent)
        case Kind.VARIABLE_REQUIRED:
            return _VARIABLE_REQUIRED.bind(app=APP_DISPLAY_NAME)
        case Kind.TOOLS_RESTRICTED:
            return _TOOLS_RESTRICTED.bind(agent=agent, app=APP_DISPLAY_NAME)


def problem_text(candidate: McpImportCandidate, agent: str) -> MessageRef:
    """Why the server can't be imported; :func:`names_field` says whether the setting follows."""
    match candidate.problem:
        case Problem.UNSUPPORTED_CONNECTION:
            transport = candidate.transport
            transport = sanitize_legacy_scalar(_TRANSPORT_NAMES.get(transport.lower(), transport.upper()))
            return _UNSUPPORTED_CONNECTION.bind(transport=transport, app=APP_DISPLAY_NAME)
        case Problem.REMOTE_ENVIRONMENT:
            return _REMOTE_ENVIRONMENT.bind(agent=agent, app=APP_DISPLAY_NAME)
        case Problem.VARIABLE_NOT_SUPPORTED:
            return _VARIABLE_NOT_SUPPORTED.bind(app=APP_DISPLAY_NAME)
        case Problem.VALUE_FROM_FILE_OR_COMMAND:
            return _VALUE_FROM_FILE_OR_COMMAND.bind(agent=agent)
        case _:
            return _VALUE_NOT_REPRESENTABLE.bind(agent=agent, app=APP_DISPLAY_NAME)


def names_field(candidate: McpImportCandidate) -> bool:
    """Whether the refusal's sentence leads to the source setting, listed after it.

    A connection refusal says everything in its sentence: the setting adds nothing.
    """
    connection = candidate.problem in (Problem.UNSUPPORTED_CONNECTION, Problem.REMOTE_ENVIRONMENT)
    return bool(candidate.problem_field) and not connection


def source_error_text(error: McpSourceError, agent: str) -> MessageRef:
    path = sanitize_legacy_scalar(error.path)
    match error.kind:
        case SourceErrorKind.UNREADABLE:
            return _UNREADABLE.bind(path=path)
        case SourceErrorKind.UNPARSABLE if error.line is not None:
            return _UNPARSABLE_AT.bind(path=path, line=error.line, column=error.column or 1)
        case SourceErrorKind.UNPARSABLE:
            return _UNPARSABLE.bind(path=path)
        case SourceErrorKind.INVALID if error.field:
            return _INVALID_FIELD.bind(path=path, field=sanitize_legacy_scalar(error.field))
        case SourceErrorKind.INVALID:
            return _INVALID.bind(path=path, agent=agent)


def imported_config(
    candidate: McpImportCandidate, agent: str, render: Callable[[MessageRef], str]
) -> MCPServerConfig | None:
    """The server to add: the source's description, where it came from and the warnings to act on.

    The warnings stay with the server because they matter after import, when
    the user fills in what is missing and tests the server.
    """
    config = candidate.config
    if config is None:
        return None
    lines = [config.description] if config.description else []
    lines.append(render(_ORIGIN.bind(agent=agent, path=sanitize_legacy_scalar(candidate.source_path))))
    for warning in candidate.warnings:
        items = ", ".join(sanitize_legacy_scalar(item) for item in warning.items)
        text = render(warning_text(warning, agent))
        # A full-width colon carries its own spacing.
        separator = "" if text.endswith("\uff1a") else " "
        lines.append(f"⚠ {text}{separator}{items}".rstrip())
    return replace(config, description="\n".join(lines))
