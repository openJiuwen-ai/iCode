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
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.service.mcp.importers import (
    FieldUse,
    ImportWarning,
    McpImportCandidate,
    McpSourceError,
    Problem,
    SourceErrorKind,
    ValueReason,
)
from chrys.service.mcp.importers import WarningKind as Kind

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.foundation.i18n import MessageRef
    from chrys.service.profiles.agents.schema import MCPServerConfig

_NOT_IMPORTED = msg(
    "tui.mcp.import.warning.not_imported",
    fallback="Some settings from the original application were not copied. After import, test the connection and check that the features you need are available.",
)
_STARTUP_WAIT = msg(
    "tui.mcp.import.warning.startup_wait",
    fallback="After import, the connection timeout may differ from the one in ChatGPT. Click “Test”; if the connection times out, increase “MCP Request Timeout (seconds)” and retry. Increasing this value also increases the tool call timeout.",
)
_APPROVAL = msg(
    "tui.mcp.import.warning.approval",
    fallback="The original application's tool approval settings were not copied. These tools will follow {app}'s current approval mode and this agent's approval rules. See “Configure approval modes” in the User Guide (F8).",
)
_DEFAULT_TIMEOUT = msg(
    "tui.mcp.import.warning.default_timeout",
    fallback="ChatGPT's tool timeout is set to 0, but {app} requires a timeout greater than 0, so the imported server will use the default MCP request timeout. To change it, enter a number of seconds greater than 0 in “MCP Request Timeout (seconds)”.",
)
_AUTH_NOT_IMPORTED = msg(
    "tui.mcp.import.warning.auth_not_imported",
    fallback="Sign-in from {agent} cannot be carried over. {app} does not yet support MCP OAuth sign-in. If the server supports token authentication, configure “Headers” as its documentation describes. If it only supports OAuth sign-in, it cannot currently be used in {app}.",
)
_AUTH_HEADERS = msg(
    "tui.mcp.import.warning.auth_headers",
    fallback="Some headers reference the original application's authentication variables, so they were not copied. If the MCP server requires authentication, configure the required credentials in “Headers” after import as its documentation describes.",
)
_TOOLS_RESTRICTED = msg(
    "tui.mcp.import.warning.tools_restricted",
    fallback="The original application's tool restrictions cannot be carried over directly, so this server's tools are initially turned off after import. Choose the tools you need in “Available Tool Scope”.",
)
_TOOL_LOADING = msg(
    "tui.mcp.import.warning.tool_loading",
    fallback="Some tool loading rules were not copied. After import, check “Loading Strategy” and “Initially Visible Tools”.",
)
_CODEX_RELATIVE_CWD = msg(
    "tui.mcp.import.warning.codex_relative_cwd",
    fallback="After import, the server's working directory may differ from the original. To keep using the original folder, enter its full path in “Working Directory”.",
)
_OPENCODE_VARIABLE_CWD = msg(
    "tui.mcp.import.warning.opencode_variable_cwd",
    fallback="This Working Directory uses an environment variable. After import, the server's working directory may differ from the folder it used in OpenCode. To keep using that folder, enter its full path in “Working Directory”. Click “Test” to check the connection; a successful test does not confirm that the folder is the one you intended.",
)
_UNSUPPORTED_CONNECTION = msg(
    "tui.mcp.import.problem.unsupported_connection",
    fallback="This server connects over {transport}; {app} supports only STDIO and Streamable HTTP. If the server also offers one of these, add it manually.",
)
_REMOTE_ENVIRONMENT = msg(
    "tui.mcp.import.problem.remote_environment",
    fallback="This configuration accesses the MCP server through a remote environment, which {app} cannot use. If you have a Streamable HTTP URL accessible from this computer or a STDIO command that runs here, add the server manually.",
)
_VARIABLE_NOT_SUPPORTED = msg(
    "tui.mcp.import.problem.variable_not_supported",
    fallback="{field} contains an environment variable reference, which {app} does not expand here. {action}",
)
_FROM_FILE = msg(
    "tui.mcp.import.problem.value_from_file",
    fallback="{field} is stored in another file and cannot be imported automatically. {action}",
)
_FROM_COMMAND = msg(
    "tui.mcp.import.problem.value_from_command",
    fallback="{field} is supplied by a command and cannot be imported automatically. {action}",
)
_VALUE_NOT_REPRESENTABLE = msg(
    "tui.mcp.import.problem.value_not_representable",
    fallback="{field} cannot be imported as written. {action}",
)
_EMPTY = msg(
    "tui.mcp.import.problem.empty",
    fallback="{field} is missing. Fill it in in the original configuration file, then reopen import, or add the server manually.",
)
_WHITESPACE = msg(
    "tui.mcp.import.problem.whitespace",
    fallback="{field} starts or ends with whitespace. The configuration editor would remove it and change the content, so this server cannot be imported as written.",
)
_ENV_NAME_NOT_REPRESENTABLE = msg(
    "tui.mcp.import.problem.env_name_not_representable",
    fallback="The environment variable name is empty or starts or ends with whitespace. The configuration editor cannot preserve it as written, so this server cannot be imported.",
)
_CHARACTERS = msg(
    "tui.mcp.import.problem.characters",
    fallback="{field} contains characters that cannot be imported. Check it in the original configuration file against the server instructions, then reopen import.",
)
_HEADER_CONFLICT = msg(
    "tui.mcp.import.problem.header_conflict",
    fallback="Two header names differ only in case and have different values. Keep one value for that header as the server requires before importing again.",
)
_VARIABLE_SYNTAX = msg(
    "tui.mcp.import.problem.variable_syntax",
    fallback="The variable reference in {field} cannot be carried over directly. {action}",
)
_WINDOWS_PATH = msg(
    "tui.mcp.import.problem.windows_path",
    fallback="Working Directory needs a full path such as C:\\tools, rather than C:tools. Add the server manually and enter a full path.",
)
_VARIABLE_PARENT = msg(
    "tui.mcp.import.problem.variable_parent",
    fallback="This Working Directory combines an environment variable with “..”, so import could change the folder used. Add the server manually and enter the intended full path.",
)
_ENTER_LITERAL = msg(
    "tui.mcp.import.action.literal",
    fallback="Add the server manually and fill in this setting using the server instructions.",
)
_ENTER_URL = msg(
    "tui.mcp.import.action.url",
    fallback="Add the server manually and enter its full URL, replacing any environment variable references with their values.",
)
_ENTER_VALUE = msg(
    "tui.mcp.import.action.value",
    fallback="Add the server manually and fill in the value using the server instructions. You can also reference an environment variable or get the value from a command.",
)
_ENTER_CWD = msg(
    "tui.mcp.import.action.cwd",
    fallback="Add the server manually and enter the full path of the folder to use. You can also reference an environment variable.",
)
_ORIGINAL_SETTING = msg("tui.mcp.import.original_setting", fallback="Original configuration field: {field}")
_ORIGINAL_SETTINGS = msg("tui.mcp.import.original_settings", fallback="Original configuration fields:")
_UNREADABLE = msg(
    "tui.mcp.import.error.unreadable", fallback="Couldn't read {path}. Check access permissions, then reopen import."
)
_UNPARSABLE = msg(
    "tui.mcp.import.error.unparsable", fallback="Couldn't parse {path}. Correct the syntax, then reopen import."
)
_UNPARSABLE_AT = msg(
    "tui.mcp.import.error.unparsable_at",
    fallback="Couldn't parse {path}: syntax error at line {line}, column {column}. Correct it, then reopen import.",
)
_FIELD_VALUE = msg("tui.mcp.import.field.value", fallback="The setting's content")
_FIELD_NAME = msg("tui.mcp.import.field.name", fallback="The server name")
_FIELD_URL = msg("tui.mcp.import.field.url", fallback="The server URL")
_FIELD_COMMAND = msg("tui.mcp.import.field.command", fallback="The startup command")
_FIELD_ARGUMENT = msg("tui.mcp.import.field.argument", fallback="The startup argument")
_FIELD_ENV_NAME = msg("tui.mcp.import.field.env_name", fallback="The environment variable name")
_FIELD_ENV_VALUE = msg("tui.mcp.import.field.env_value", fallback="The environment variable value")
_FIELD_HEADER_NAME = msg("tui.mcp.import.field.header_name", fallback="The header name")
_FIELD_HEADER_VALUE = msg("tui.mcp.import.field.header_value", fallback="The header value")
_FIELD_CWD = msg("tui.mcp.import.field.cwd", fallback="The Working Directory")

_HEADER_NAME_CHARACTERS = msg(
    "tui.mcp.import.problem.header_name_characters",
    fallback="The header name contains unsupported characters, such as spaces or line breaks. Check the name in the original configuration file against the server instructions, then reopen import.",
)
_HEADER_VALUE_CHARACTERS = msg(
    "tui.mcp.import.problem.header_value_characters",
    fallback="Header values cannot contain characters such as Chinese text or line breaks in {app}. Check this header in the original configuration file against the server instructions, then reopen import.",
)
_CWD_CHARACTERS = msg(
    "tui.mcp.import.problem.cwd_characters",
    fallback="The Working Directory contains line breaks or invisible special characters. Retype the folder path in the original configuration file, then reopen import.",
)

_FIELDS = {
    FieldUse.VALUE: _FIELD_VALUE,
    FieldUse.NAME: _FIELD_NAME,
    FieldUse.URL: _FIELD_URL,
    FieldUse.COMMAND: _FIELD_COMMAND,
    FieldUse.ARGUMENT: _FIELD_ARGUMENT,
    FieldUse.ENV_NAME: _FIELD_ENV_NAME,
    FieldUse.ENV_VALUE: _FIELD_ENV_VALUE,
    FieldUse.HEADER_NAME: _FIELD_HEADER_NAME,
    FieldUse.HEADER_VALUE: _FIELD_HEADER_VALUE,
    FieldUse.CWD: _FIELD_CWD,
}
_INVALID = msg("tui.mcp.import.error.invalid", fallback="{path} isn't a valid {agent} configuration.")
_INVALID_FIELD = msg("tui.mcp.import.error.invalid_field", fallback="{path} has an invalid setting: {field}.")
_ORIGIN = msg("tui.mcp.import.origin", fallback="Imported from {agent} ({path}).")
# How the import names a connection type it can't use; any other is shown in capitals.
_TRANSPORT_NAMES = {"sse": "SSE", "ws": "WebSocket"}


def warning_text(warning: ImportWarning, agent: str) -> MessageRef:
    """The warning's sentence; its source settings are presented separately."""
    message = {
        Kind.NOT_IMPORTED: _NOT_IMPORTED,
        Kind.STARTUP_WAIT: _STARTUP_WAIT,
        Kind.APPROVAL: _APPROVAL,
        Kind.DEFAULT_TIMEOUT: _DEFAULT_TIMEOUT,
        Kind.AUTH_NOT_IMPORTED: _AUTH_NOT_IMPORTED,
        Kind.AUTH_HEADERS: _AUTH_HEADERS,
        Kind.TOOLS_RESTRICTED: _TOOLS_RESTRICTED,
        Kind.TOOL_LOADING: _TOOL_LOADING,
        Kind.CODEX_RELATIVE_CWD: _CODEX_RELATIVE_CWD,
        Kind.OPENCODE_VARIABLE_CWD: _OPENCODE_VARIABLE_CWD,
    }[warning.kind]
    if warning.kind is Kind.AUTH_NOT_IMPORTED:
        return message.bind(agent=agent, app=APP_DISPLAY_NAME)
    if warning.kind in (Kind.APPROVAL, Kind.DEFAULT_TIMEOUT):
        return message.bind(app=APP_DISPLAY_NAME)
    return message.bind()


def problem_text(
    candidate: McpImportCandidate, agent: str, render: Callable[[MessageRef], str] = format_message
) -> MessageRef:
    """Explain the recorded cause, with advice limited to what the target field supports."""
    if candidate.problem is Problem.UNSUPPORTED_CONNECTION:
        transport = candidate.transport
        transport = sanitize_legacy_scalar(_TRANSPORT_NAMES.get(transport.lower(), transport.upper()))
        return _UNSUPPORTED_CONNECTION.bind(transport=transport, app=APP_DISPLAY_NAME)
    if candidate.problem is Problem.REMOTE_ENVIRONMENT:
        return _REMOTE_ENVIRONMENT.bind(app=APP_DISPLAY_NAME)
    use = candidate.field_use
    if (
        candidate.problem is Problem.VALUE_NOT_REPRESENTABLE
        and use is FieldUse.ENV_NAME
        and candidate.reason in (ValueReason.EMPTY, ValueReason.WHITESPACE)
    ):
        return _ENV_NAME_NOT_REPRESENTABLE.bind()
    action = _ENTER_LITERAL
    if use is FieldUse.URL:
        action = _ENTER_URL
    elif use in (FieldUse.ENV_VALUE, FieldUse.HEADER_VALUE):
        action = _ENTER_VALUE
    elif use is FieldUse.CWD:
        action = _ENTER_CWD
    message = {
        ValueReason.EMPTY: _EMPTY,
        ValueReason.WHITESPACE: _WHITESPACE,
        ValueReason.CHARACTERS: _CHARACTERS,
        ValueReason.HEADER_CONFLICT: _HEADER_CONFLICT,
        ValueReason.VARIABLE: _VARIABLE_SYNTAX,
        ValueReason.FILE: _FROM_FILE,
        ValueReason.COMMAND: _FROM_COMMAND,
        ValueReason.WINDOWS_PATH: _WINDOWS_PATH,
        ValueReason.VARIABLE_PARENT: _VARIABLE_PARENT,
    }.get(candidate.reason, _VALUE_NOT_REPRESENTABLE)
    if candidate.reason is ValueReason.CHARACTERS:
        specific = {
            FieldUse.HEADER_NAME: _HEADER_NAME_CHARACTERS,
            FieldUse.HEADER_VALUE: _HEADER_VALUE_CHARACTERS,
            FieldUse.CWD: _CWD_CHARACTERS,
        }.get(use)
        if specific is not None:
            if specific is _HEADER_VALUE_CHARACTERS:
                return specific.bind(app=APP_DISPLAY_NAME)
            return specific.bind()
    if candidate.problem is Problem.VARIABLE_NOT_SUPPORTED:
        message = _VARIABLE_NOT_SUPPORTED
    if message in (_HEADER_CONFLICT, _WINDOWS_PATH, _VARIABLE_PARENT):
        return message.bind()
    field = render(_FIELDS[use].bind())
    if message in (_EMPTY, _WHITESPACE, _CHARACTERS):
        return message.bind(field=field)
    if message is _VARIABLE_NOT_SUPPORTED:
        return message.bind(field=field, action=render(action.bind()), app=APP_DISPLAY_NAME)
    return message.bind(field=field, action=render(action.bind()))


def original_setting(field: str) -> MessageRef:
    return _ORIGINAL_SETTING.bind(field=sanitize_legacy_scalar(field))


def names_field(candidate: McpImportCandidate) -> bool:
    """Whether the refusal is introduced by its source setting.

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
        items = [sanitize_legacy_scalar(item) for item in warning.items]
        text = render(warning_text(warning, agent))
        lines.append("")
        if len(items) == 1:
            lines.append(f"⚠ {render(original_setting(items[0]))}")
        elif items:
            lines.append(f"⚠ {render(_ORIGINAL_SETTINGS.bind())}")
            lines.extend(f"  {item}" for item in items)
        else:
            text = f"⚠ {text}"
        lines.append(text)
    return replace(config, description="\n".join(lines))
