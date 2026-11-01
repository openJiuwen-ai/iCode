# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP configuration panel — composable widget for the MCP tab."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import socket
from collections.abc import Callable, Collection, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal, TypeGuard

from rich.markup import escape
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Label

from chrys.app.tui.i18n import render_str, widget_locale_controller, widget_localizer
from chrys.app.tui.screens.agents.mcp_choices import MCPChoices, mcp_choice_conflict, mcp_name_key
from chrys.app.tui.screens.agents.mcp_picker import MCPPickerAction, MCPPickerDialog, MCPPickerResult, conflict_message
from chrys.app.tui.screens.agents.panels.markdown_report import (
    NOT_ADVERTISED,
    SUPPORTED_MARK,
    UNSUPPORTED_MARK,
    code_span,
    definition_bullets,
    inline_text,
    md_table,
)
from chrys.app.tui.screens.agents.validation_messages import (
    CONTEXT_ERROR,
    DUPLICATE_KEY_ROW,
    FIELD_REQUIRED,
    MCP_COMMAND_REQUIRED,
    MCP_DUPLICATE_SERVER,
    MCP_INITIAL_TOOLS_PERMITTED,
    MCP_INITIALLY_VISIBLE_TOOLS_VALIDATION,
    MCP_INVALID_COMMAND,
    MCP_ROW_KEY_REQUIRED,
    MCP_ROW_VALUE_REQUIRED,
    MCP_SELECT_PERMITTED,
    MCP_SERVER_CONTEXT,
    MCP_TIMEOUT_POSITIVE,
    MCP_TIMEOUT_VALID,
    MCP_TOOL_LIST_DUPLICATE,
    MCP_TOOL_LIST_EMPTY,
    MCP_URL_REQUIRED,
    MCP_URL_SCHEME,
    NAME_FIELD,
    SERVER_ERROR,
    TOOL_NAME_ITEM,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MCP_ENVIRONMENT_VARIABLES as _ENVIRONMENT_VARIABLES,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MCP_HEADERS as _HEADERS,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MCP_INITIALLY_VISIBLE_TOOLS as _INITIALLY_VISIBLE_TOOLS,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MCP_SELECTED_TOOL_NAMES as _SELECTED_TOOL_NAMES,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MCP_TOOL_ACCESS_NONE as _TOOL_ACCESS_NONE_LABEL,
)
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog
from chrys.app.tui.util.liveness import composed_live
from chrys.app.tui.widgets import Checkbox, ConfigAddButton, EnhancedTextArea, HatchedEmptyState, Select
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.app.tui.widgets.config_card import CardListChanged, CollapsibleConfigCard
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import DisplayBlock, MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.command_line import join_windows_command_line, split_windows_command_line
from chrys.service.mcp.adapter import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    STANDARD_CAPABILITY_GROUPS,
    MCPAdapter,
    MCPToolConfigurationError,
)
from chrys.service.mcp.validation import MCP_PROGRESSIVE_CONTROL_TOOL_NAMES, validate_mcp_tool_name_prefix
from chrys.service.tools.names import chrys_reserved_tool_names

logger = logging.getLogger(__name__)

_MCP_TITLE = msg("tui.mcp.title", fallback="MCP")
_MCP_SERVER_SUBJECT = msg("tui.mcp.connection_test.subject", fallback="MCP Server")
_WAIT_BEFORE_ADDING = msg(
    "tui.mcp.wait_before_adding",
    fallback="Wait for MCP connection tests to finish before adding servers.",
)
_WAIT_BEFORE_REMOVING = msg(
    "tui.mcp.wait_before_removing",
    fallback="Wait for MCP connection tests to finish before removing servers.",
)
_DELETE_SERVER_TITLE = msg("tui.mcp.confirm_delete.title", fallback="Delete MCP Server")
_DELETE_SERVER_MESSAGE = msg(
    "tui.mcp.confirm_delete.message",
    fallback='Delete MCP server\n"{name}"?',
    multiline=True,
)
_DELETE_SERVER_CONFIRM = msg("tui.mcp.confirm_delete.confirm", fallback="Delete")
_TOOL_ACCESS_ALL_LABEL = msg("tui.mcp.tool_access.all", fallback="All server tools")
_TOOL_ACCESS_SELECTED_LABEL = msg("tui.mcp.tool_access.selected", fallback="Only selected tools")
_TOOL_LOADING_FULL_LABEL = msg(
    "tui.mcp.tool_loading.full",
    fallback="Full — load all available tools",
)
_TOOL_LOADING_PROGRESSIVE_LABEL = msg(
    "tui.mcp.tool_loading.progressive",
    fallback="On demand — load tools as needed",
)
_LOAD_PROMPTS_TOOLTIP = msg(
    "tui.mcp.load_prompts_tooltip",
    fallback=(
        "When enabled, {app} loads prompt templates advertised by this MCP server as callable tools. If the server "
        "does not advertise prompt support, prompt loading is skipped automatically. Disable this to keep server "
        "prompt templates out of the model's tool list."
    ),
)
_LOAD_PROMPTS_HINT = msg(
    "tui.mcp.load_prompts_hint",
    fallback="Prompt templates advertised by the server are loaded as callable tools in the model's tool list.",
)
_EXPOSE_INSTRUCTIONS_TOOLTIP = msg(
    "tui.mcp.expose_instructions_tooltip",
    fallback=(
        "When enabled, {app} includes the server's usage instructions from the MCP handshake in the model's context "
        "as a per-turn system reminder. Servers that provide no instructions are unaffected. Disable this to keep "
        "the server's guidance out of the model's context."
    ),
)
_EXPOSE_INSTRUCTIONS_HINT = msg(
    "tui.mcp.expose_instructions_hint",
    fallback="The server's usage guidance from the MCP handshake is injected as a system reminder.",
)
_TOOL_ACCESS_TOOLTIP = msg(
    "tui.mcp.tool_access_tooltip",
    fallback=(
        "Defines the MCP catalog boundary. Choose all tools advertised by the server, an explicit allowlist, or no "
        "callable tools."
    ),
)
_PROGRESSIVE_DISCLOSURE_TOOLTIP = msg(
    "tui.mcp.progressive_disclosure_tooltip",
    fallback=(
        "When enabled, only loader controls and the optional initially visible tools are sent first. The model can "
        "load other tools from the available-tool scope during the current run."
    ),
)
_ALWAYS_LOAD_TOOLTIP = msg(
    "tui.mcp.always_load_tooltip",
    fallback="Optional comma-separated names from the available-tool scope to send to the model from the first request.",
)
_HEADER_NAME_PLACEHOLDER = msg(
    "tui.mcp.placeholder.header_name",
    fallback="e.g. X-Auth-Token",
)
_HEADER_VALUE_PLACEHOLDER = msg(
    "tui.mcp.placeholder.header_value",
    fallback="e.g. {{{{AUTH_TOKEN}}}}",
)
_VARIABLE_NAME_PLACEHOLDER = msg("tui.mcp.placeholder.variable_name", fallback="Variable name")
_VALUE_PLACEHOLDER = msg("tui.mcp.placeholder.value", fallback="Value")
_TRANSPORT = msg("tui.mcp.transport", fallback="Transport")
_SERVER_NAME = msg("tui.mcp.server_name", fallback="Server Name")
_SERVER_NAME_PLACEHOLDER = msg("tui.mcp.placeholder.server_name", fallback="Server name")
_DESCRIPTION = msg("tui.mcp.description", fallback="Description")
_PROMPTS_AND_INSTRUCTIONS = msg(
    "tui.mcp.prompts_and_instructions",
    fallback="Server Instructions & Prompt Templates",
)
_EXPOSE_SERVER_INSTRUCTIONS = msg(
    "tui.mcp.expose_server_instructions",
    fallback="Include server instructions in model context (if available)",
)
_EXPOSE_SERVER_PROMPTS = msg(
    "tui.mcp.expose_server_prompts",
    fallback="Load server prompt templates as tools (if available)",
)
_TOOL_ACCESS = msg("tui.mcp.tool_access", fallback="Tool Access")
_PERMITTED_TOOL_SET = msg("tui.mcp.permitted_tool_set", fallback="Available Tool Scope")
_SELECTED_TOOLS_PLACEHOLDER = msg("tui.mcp.placeholder.selected_tools", fallback="tool_a, tool_b")
_TOOL_LOADING = msg("tui.mcp.tool_loading", fallback="Tool Loading")
_LOADING_STRATEGY = msg("tui.mcp.loading_strategy", fallback="Loading Strategy")
_INITIAL_TOOLS_PLACEHOLDER = msg("tui.mcp.placeholder.initial_tools", fallback="search, read_file")
_NAMING_AND_LIMITS = msg("tui.mcp.naming_and_limits", fallback="Naming & Limits")
_TOOL_NAME_PREFIX = msg("tui.mcp.tool_name_prefix", fallback="Tool Name Prefix")
_TOOL_NAME_PREFIX_PLACEHOLDER = msg(
    "tui.mcp.placeholder.tool_name_prefix",
    fallback="e.g. github → tools named github_<tool>",
)
_REQUEST_TIMEOUT = msg("tui.mcp.request_timeout", fallback="MCP Request Timeout (seconds)")
_REQUEST_TIMEOUT_PLACEHOLDER = msg(
    "tui.mcp.placeholder.request_timeout",
    fallback="Leave blank for default ({default})",
)
_ENABLED = msg("tui.mcp.enabled", fallback="Enabled")
_TEST = msg("tui.mcp.test", fallback="Test")
_TESTING = msg("tui.mcp.testing", fallback="Testing...")
_URL = msg("tui.mcp.url", fallback="URL")
_URL_PLACEHOLDER = msg("tui.mcp.placeholder.url", fallback="https://api.example.com/mcp")
_SKIP_TLS_VERIFICATION = msg("tui.mcp.skip_tls_verification", fallback="Skip TLS verification")
_INSECURE_TLS = msg(
    "tui.mcp.insecure_tls",
    fallback="Insecure: disables HTTPS certificate validation. Use only for trusted self-signed servers.",
)
_BYPASS_PROXY = msg("tui.mcp.bypass_proxy", fallback="Bypass proxy")
_COMMAND = msg("tui.mcp.command", fallback="Command")
_COMMAND_PLACEHOLDER = msg(
    "tui.mcp.placeholder.command",
    fallback="e.g. npx -y @modelcontextprotocol/server-filesystem /path",
)
_HTTP_OPTIONS = msg("tui.mcp.http_options", fallback="HTTP Options")
_STDIO_OPTIONS = msg("tui.mcp.stdio_options", fallback="STDIO Options")
_ADD = msg("tui.mcp.add", fallback="+ Add")
_SERVERS = msg("tui.mcp.servers", fallback="MCP Servers")
_SERVERS_DESCRIPTION = msg(
    "tui.mcp.servers_description",
    fallback="Configure Model Context Protocol servers",
)
_EMPTY = msg("tui.mcp.empty", fallback="No MCP servers configured")
_CONNECTION_SUCCESSFUL = msg("tui.mcp.connection_test.successful", fallback="Connection successful.")
_CONNECTION_CONFIGURATION_CONFLICT = msg(
    "tui.mcp.connection_test.error.configuration_conflict",
    fallback="MCP tool configuration conflict.",
)
_CONNECTION_CANCELLED = msg(
    "tui.mcp.connection_test.error.cancelled",
    fallback="Connection test was cancelled. Please try again.",
)
_CONNECTION_TIMED_OUT = msg(
    "tui.mcp.connection_test.error.timed_out",
    fallback="Connection test timed out. Please check server and timeout settings.",
)
_CONNECTION_UNREACHABLE = msg(
    "tui.mcp.connection_test.error.unreachable",
    fallback="Unable to reach MCP server. Please check URL, network, and server status.",
)
_CONNECTION_REQUEST_FAILED = msg(
    "tui.mcp.connection_test.error.request_failed",
    fallback="MCP HTTP request failed. Please verify endpoint and authentication.",
)
_CONNECTION_FAILED = msg(
    "tui.mcp.connection_test.error.failed",
    fallback="MCP server connection failed.",
)
_CAPABILITIES_HEADING = msg("tui.mcp.connection_report.heading.capabilities", fallback="Capabilities")
_INSTRUCTIONS_HEADING = msg("tui.mcp.connection_report.heading.instructions", fallback="Instructions")
_TOOLS_HEADING = msg("tui.mcp.connection_report.heading.tools", fallback="Tools")
_PROMPTS_HEADING = msg("tui.mcp.connection_report.heading.prompts", fallback="Prompts")
_PROGRESSIVE_DISCLOSURE_HEADING = msg(
    "tui.mcp.connection_report.heading.progressive_disclosure",
    fallback="On-demand Loading",
)
_IDENTITY_NOT_REPORTED = msg(
    "tui.mcp.connection_report.identity_not_reported",
    fallback="*The server did not report its identity.*",
)
_NO_COMMAND_CONFIGURED = msg(
    "tui.mcp.connection_report.no_command_configured",
    fallback="*no command configured*",
)
_NO_URL_CONFIGURED = msg(
    "tui.mcp.connection_report.no_url_configured",
    fallback="*no URL configured*",
)
_CONNECTED_VIA = msg(
    "tui.mcp.connection_report.connected_via",
    fallback="Connected via {transport}: {target}",
)
_TOOLS_EXPOSED = msg(
    "tui.mcp.connection_report.tools_exposed",
    fallback="{count} tool available to the model.\n\n{catalog}",
    plural_fallback="{count} tools available to the model.\n\n{catalog}",
    multiline=True,
)
_ADVERTISED_NO_TOOLS = msg(
    "tui.mcp.connection_report.advertised_no_tools",
    fallback="*The server supports tools, but none are available (check the available-tool scope).*",
)
_MORE_ENTRIES = msg("tui.mcp.connection_report.more_entries", fallback="…and {extra_count} more.")
_CAPABILITY_HEADER = msg("tui.mcp.connection_report.header.capability", fallback="Capability")
_ADVERTISED_HEADER = msg("tui.mcp.connection_report.header.advertised", fallback="Advertised")
_PROMPTS_EXPOSED = msg(
    "tui.mcp.connection_report.prompts_exposed",
    fallback="{count} prompt template loaded as a tool.\n\n{catalog}",
    plural_fallback="{count} prompt templates loaded as tools.\n\n{catalog}",
    multiline=True,
)
_PROMPT_LOADING_DISABLED = msg(
    "tui.mcp.connection_report.prompt_loading_disabled",
    fallback="*Prompt loading is disabled for this server.*",
)
_ADVERTISED_NO_PROMPTS = msg(
    "tui.mcp.connection_report.advertised_no_prompts",
    fallback="*The server supports prompt templates, but none are available.*",
)
_PROGRESSIVE_ENABLED = msg(
    "tui.mcp.connection_report.progressive_enabled",
    fallback="Enabled — only these tools are visible initially; the rest load on demand:",
)

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.mcp.adapter import MCPTestReport
    from chrys.service.profiles.agents.schema import MCPServerConfig


_TRANSPORT_OPTIONS = [
    ("HTTP", "http"),
    ("STDIO", "stdio"),
]

_TOOL_ACCESS_ALL = "all"
_TOOL_ACCESS_SELECTED = "selected"
_TOOL_ACCESS_NONE = "none"
_TOOL_ACCESS_OPTIONS = [
    (_TOOL_ACCESS_ALL_LABEL, _TOOL_ACCESS_ALL),
    (_TOOL_ACCESS_SELECTED_LABEL, _TOOL_ACCESS_SELECTED),
    (_TOOL_ACCESS_NONE_LABEL, _TOOL_ACCESS_NONE),
]
_TOOL_LOADING_FULL = "full"
_TOOL_LOADING_PROGRESSIVE = "progressive"
_TOOL_LOADING_OPTIONS = [
    (_TOOL_LOADING_FULL_LABEL, _TOOL_LOADING_FULL),
    (_TOOL_LOADING_PROGRESSIVE_LABEL, _TOOL_LOADING_PROGRESSIVE),
]


def _is_transport(value: str) -> TypeGuard[Literal["stdio", "http"]]:
    """Narrow the schema-backed transport discriminator."""
    return value in {"stdio", "http"}


@dataclass(frozen=True, slots=True)
class _MCPKeyValueRows:
    """Widget-id and placeholder policy for one MCP key/value row list."""

    container_suffix: str
    action_prefix: str
    key_placeholder: MessageDef
    value_placeholder: MessageDef


@dataclass(frozen=True, slots=True)
class _MCPKeyValueDrafts:
    """UI-only key/value row text preserved across card rebuilds."""

    headers: list[tuple[str, str]] | None = None
    env: list[tuple[str, str]] | None = None


@dataclass(slots=True)
class _MCPServerRow:
    """One server in the panel: its config plus the UI-only state a card rebuild carries over."""

    config: MCPServerConfig
    command_line_text: str | None = None
    key_value_drafts: _MCPKeyValueDrafts = field(default_factory=_MCPKeyValueDrafts)
    # Saved servers open folded to their name row; a server added in the panel opens unfolded.
    collapsed: bool = True


_MCP_HEADER_ROWS = _MCPKeyValueRows(
    container_suffix="headers",
    action_prefix="h",
    key_placeholder=_HEADER_NAME_PLACEHOLDER,
    value_placeholder=_HEADER_VALUE_PLACEHOLDER,
)
_MCP_ENV_ROWS = _MCPKeyValueRows(
    container_suffix="env",
    action_prefix="e",
    key_placeholder=_VARIABLE_NAME_PLACEHOLDER,
    value_placeholder=_VALUE_PLACEHOLDER,
)


class MCPConnectionCard(CollapsibleConfigCard):
    """A single MCP server entry with name, transport, and transport-specific fields.

    The header shows the server name, so a collapsed card shows just that name in the server list.
    """

    DEFAULT_CSS = """
    MCPConnectionCard .mcp-row {
        height: auto;
        margin: 0 0 1 0;
    }
    MCPConnectionCard .mcp-row Input {
        margin: 0 1 0 0;
    }
    MCPConnectionCard .mcp-header-row {
        height: auto;
        margin: 0;
    }
    MCPConnectionCard .mcp-allowed-tools-row {
        height: auto;
        margin: 1 0 0 0;
    }
    MCPConnectionCard .mcp-progressive-fields,
    MCPConnectionCard .mcp-policy-fields {
        height: auto;
    }
    MCPConnectionCard .mcp-option-section {
        height: auto;
        margin: 1 0 0 0;
        padding: 1 0 1 2;
        border: solid $tui-border-foreground 15%;
        border-title-color: $text-muted;
    }
    MCPConnectionCard .mcp-tool-access-section {
        margin: 1 0 0 0;
    }
    MCPConnectionCard .mcp-option-section .mcp-label:first-of-type {
        margin-top: 0;
    }
    MCPConnectionCard .mcp-option-section .mcp-policy-fields .mcp-label:first-of-type,
    MCPConnectionCard .mcp-option-section .mcp-progressive-fields .mcp-label:first-of-type {
        margin-top: 1;
    }
    MCPConnectionCard .mcp-option-section Select,
    MCPConnectionCard .mcp-option-section .mcp-policy-fields Input,
    MCPConnectionCard .mcp-option-section .mcp-progressive-fields Input,
    MCPConnectionCard .mcp-naming-input {
        margin: 0 2 0 0;
    }
    MCPConnectionCard .mcp-transport-option-section > Input,
    MCPConnectionCard .mcp-transport-option-section > EnhancedTextArea,
    MCPConnectionCard .mcp-transport-option-section > .mcp-list {
        margin-right: 2;
    }
    MCPConnectionCard .mcp-transport-option-section .mcp-list > .mcp-row:last-child {
        margin-bottom: 0;
    }
    MCPConnectionCard .mcp-field {
        width: 1fr;
        height: auto;
        margin: 0;
    }
    MCPConnectionCard .mcp-label {
        height: 1;
        color: $text-muted;
        margin: 1 0 0 0;
    }
    MCPConnectionCard .mcp-allowed-tools-row .mcp-label {
        width: auto;
        margin: 0 2 0 0;
    }
    MCPConnectionCard Select {
        height: auto;
        margin: 0;
    }
    MCPConnectionCard SelectCurrent {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
    }
    MCPConnectionCard SelectOverlay {
        border: round $tui-border-primary $border-opacity;
        background: $surface;
    }
    MCPConnectionCard .mcp-transport-fields,
    MCPConnectionCard .mcp-common-fields,
    MCPConnectionCard .mcp-list {
        height: auto;
    }
    MCPConnectionCard .mcp-footer-row {
        height: auto;
        margin: 1 0 0 0;
    }
    MCPConnectionCard .mcp-footer-left {
        width: 1fr;
        height: auto;
    }
    MCPConnectionCard .mcp-test-btn {
        min-width: 14;
        height: 1;
    }
    MCPConnectionCard .mcp-test-btn.-disabled {
        color: $text-muted;
    }
    MCPConnectionCard EnhancedTextArea {
        height: 5;
        border: none;
        background: $foreground 8%;
        padding: 0 0;
        margin: 0;
        scrollbar-size-vertical: 1;
    }
    MCPConnectionCard EnhancedTextArea:focus {
        border: none;
        background: $foreground 12%;
    }
    MCPConnectionCard EnhancedTextArea .text-area--cursor-line {
        background: $primary 15%;
    }
    MCPConnectionCard Input {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
        margin: 0;
    }
    MCPConnectionCard Input:focus {
        border: none;
        background: $foreground 12%;
    }
    MCPConnectionCard Checkbox {
        width: auto;
        height: 1;
        color: $text-muted;
        margin: 0 1 0 0;
        padding: 0 2;
        border: solid $tui-border-foreground 15%;
        background: $foreground 6%;
    }
    MCPConnectionCard Checkbox.-on {
        color: $secondary;
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 12%;
    }
    MCPConnectionCard Checkbox > .toggle--button {
        color: $foreground 35%;
        background: $foreground 6%;
    }
    MCPConnectionCard Checkbox.-on > .toggle--button {
        color: $success;
        background: $secondary 12%;
    }
    MCPConnectionCard Checkbox .toggle--label {
        text-style: bold;
    }
    MCPConnectionCard .mcp-skip-tls {
        margin: 1 1 0 0;
    }
    MCPConnectionCard .mcp-bypass-proxy {
        margin: 1 1 1 0;
    }
    MCPConnectionCard .mcp-skip-tls-hint {
        width: 100%;
        height: auto;
        color: $warning;
        margin: 0 1 1 0;
        padding: 0 0 0 3;
    }
    MCPConnectionCard .mcp-checkbox-hint {
        width: 100%;
        height: auto;
        color: $text-disabled;
        margin: 0 1 1 0;
        padding: 0 0 0 3;
    }
    MCPConnectionCard .mcp-checkbox-hint:last-of-type {
        margin-bottom: 0;
    }
    MCPConnectionCard Checkbox.mcp-guidance-checkbox .toggle--label {
        color: $text;
    }
    MCPConnectionCard Checkbox.mcp-guidance-checkbox.-hint-collapsed {
        margin-bottom: 1;
    }
    MCPConnectionCard Checkbox.mcp-guidance-checkbox.-hint-collapsed:last-of-type {
        margin-bottom: 0;
    }
    MCPConnectionCard .mcp-item-row {
        height: auto;
        background: transparent;
        padding: 0;
        margin: 0 0 1 0;
    }
    MCPConnectionCard .mcp-item-row Input {
        margin: 0 1 0 0;
    }
    MCPConnectionCard .mcp-item-row .mcp-item-value-input {
        margin-right: 0;
    }
    MCPConnectionCard .mcp-small-btn {
        min-width: 6;
        height: 1;
        text-style: bold;
        padding: 0 1;
    }
    MCPConnectionCard .mcp-remove-btn {
        min-width: 3;
        height: 1;
        border: none;
        background: transparent;
        color: $error;
        padding: 0;
        content-align: right middle;
        text-align: right;
    }
    """

    class TestRequested(Message):
        """Posted when the user requests a connection test; carries the card, which a later rebuild may re-index."""

        def __init__(self, card: MCPConnectionCard) -> None:
            self.card = card
            super().__init__()

        @property
        def control(self) -> MCPConnectionCard:
            return self.card

    class Removed(CollapsibleConfigCard.RemoveRequested):
        """Posted when the user clicks delete; carries the card, which a later rebuild may re-index."""

        def __init__(self, card: MCPConnectionCard) -> None:
            self.card = card
            super().__init__(card.index)

        @property
        def control(self) -> MCPConnectionCard:
            return self.card

    # Class-level counter for generating unique widget IDs across all card instances.
    _uid_counter: int = 0
    _delete_button_prefix = "mcp-delete-btn"

    def __init__(
        self,
        config: MCPServerConfig,
        index: int,
        *,
        command_line_text: str | None = None,
        key_value_drafts: _MCPKeyValueDrafts | None = None,
        read_only: bool = False,
        collapsed: bool = False,
    ) -> None:
        self._config = config
        self._index = index
        self._command_line_text = command_line_text
        self._key_value_drafts = key_value_drafts or _MCPKeyValueDrafts()
        self._transport: Literal["stdio", "http"] = config.transport if _is_transport(config.transport) else "stdio"
        self._headers_user_modified = False
        self._env_user_modified = False
        super().__init__(index=index, collapsed=collapsed, read_only=read_only)

    @property
    def index(self) -> int:
        """Stable server index assigned by the panel."""
        return self._index

    def _removed_message(self) -> MCPConnectionCard.Removed:
        return self.Removed(self)

    @staticmethod
    def _format_command_line(command: str, args: list[str]) -> str:
        if not command:
            return ""
        parts = [command, *args]
        if get_platform().is_windows:
            return join_windows_command_line(parts)
        return shlex.join(parts)

    @staticmethod
    def _split_command_line(
        command_line: str,
        *,
        render_message: Callable[[MessageRef], str] = format_message,
    ) -> tuple[str, list[str]]:
        text = command_line.strip()
        if not text:
            return "", []
        try:
            parts = split_windows_command_line(text) if get_platform().is_windows else shlex.split(text)
        except ValueError as exc:
            error_message = render_message(MCP_INVALID_COMMAND.bind(detail=DisplayBlock(str(exc))))
            raise ValueError(error_message) from exc
        if not parts:
            return "", []
        return parts[0], parts[1:]

    @staticmethod
    def _parse_allowed_tools(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def _validate_comma_separated_tool_names(
        self,
        value: str,
        *,
        label_definition: MessageDef,
    ) -> list[str]:
        """Validate editable tool-name list syntax without constraining MCP names."""
        if not value.strip():
            return []
        localizer = widget_localizer(self)
        label = render_str(localizer, label_definition.bind())
        names = [item.strip() for item in value.split(",")]
        errors: list[str] = []
        if any(not name for name in names):
            errors.append(
                render_str(
                    localizer,
                    MCP_TOOL_LIST_EMPTY.bind(
                        label=label,
                        item=render_str(localizer, TOOL_NAME_ITEM.bind()),
                    ),
                )
            )
        seen: set[str] = set()
        for name in names:
            if not name:
                continue
            if name in seen:
                errors.append(
                    render_str(
                        localizer,
                        MCP_TOOL_LIST_DUPLICATE.bind(
                            label=label,
                            name=DisplayBlock(name),
                        ),
                    )
                )
            seen.add(name)
        return errors

    @staticmethod
    def _tool_access_mode(allowed_tools: list[str] | None) -> str:
        if allowed_tools is None:
            return _TOOL_ACCESS_ALL
        return _TOOL_ACCESS_SELECTED if allowed_tools else _TOOL_ACCESS_NONE

    def _mounted_tool_access_mode(self) -> str:
        mode = self._tool_access_mode(self._config.allowed_tools)
        with contextlib.suppress(NoMatches):
            mode = str(self.query_one(f"#mcp-tool-access-{self._index}", Select).value)
        return mode

    def _mounted_loading_strategy(self) -> str:
        strategy = _TOOL_LOADING_PROGRESSIVE if self._config.use_progressive_disclosure else _TOOL_LOADING_FULL
        with contextlib.suppress(NoMatches):
            strategy = str(self.query_one(f"#mcp-loading-strategy-{self._index}", Select).value)
        return strategy

    def _sync_tool_policy_fields(self) -> None:
        """Apply the access -> loading strategy -> initial subset hierarchy."""
        with contextlib.suppress(NoMatches):
            access_mode = self._mounted_tool_access_mode()
            selected_fields = self.query_one(f"#mcp-selected-tools-fields-{self._index}", Vertical)
            selected_input = self.query_one(f"#mcp-tools-{self._index}", Input)
            exposure_fields = self.query_one(f"#mcp-model-exposure-fields-{self._index}", Vertical)
            loading = self.query_one(f"#mcp-loading-strategy-{self._index}", Select)
            initial_fields = self.query_one(f"#mcp-always-load-fields-{self._index}", Vertical)
            initial_input = self.query_one(f"#mcp-always-load-{self._index}", Input)

            selected = access_mode == _TOOL_ACCESS_SELECTED
            no_tools = access_mode == _TOOL_ACCESS_NONE
            progressive = self._mounted_loading_strategy() == _TOOL_LOADING_PROGRESSIVE

            selected_fields.display = selected
            selected_input.disabled = not selected or self._read_only
            exposure_fields.display = not no_tools
            loading.disabled = no_tools or self._read_only
            initial_fields.display = not no_tools and progressive
            initial_input.disabled = no_tools or not progressive or self._read_only

    @staticmethod
    def _format_timeout(value: int | None) -> str:
        return "" if value is None else str(value)

    def _parse_key_value_rows(
        self,
        container_id: str,
        add_row_id: str,
        *,
        user_modified: bool,
    ) -> dict[str, str]:
        container = self.query_one(f"#{container_id}", Vertical)
        if not user_modified:
            self.query_one(f"#{add_row_id}", Horizontal)

        parsed: dict[str, str] = {}
        for row in container.query(".mcp-item-row"):
            key = row.query_one(".mcp-item-key-input", Input).value.strip()
            value = row.query_one(".mcp-item-value-input", Input).value.strip()
            if key:
                parsed[key] = value
        return parsed

    def compose(self) -> ComposeResult:
        cfg = self._config
        localizer = widget_localizer(self)
        yield from self.compose_header(
            self._header_title(cfg.name),
            row_class="mcp-header-row",
            title_class="mcp-title",
        )

        with self.card_body():
            with Vertical(classes="mcp-field"):
                yield Label(render_str(localizer, _TRANSPORT.bind()), classes="mcp-label")
                transport = Select(
                    _TRANSPORT_OPTIONS,
                    value=self._transport,
                    id=f"mcp-transport-{self._index}",
                    allow_blank=False,
                )
                transport.disabled = self._read_only
                yield transport
                yield Label(f"[red]*[/red] {escape(render_str(localizer, _SERVER_NAME.bind()))}", classes="mcp-label")
                name = Input(
                    value=cfg.name,
                    placeholder=render_str(localizer, _SERVER_NAME_PLACEHOLDER.bind()),
                    id=f"mcp-name-{self._index}",
                )
                name.disabled = self._read_only
                yield name
                yield Label(render_str(localizer, _DESCRIPTION.bind()), classes="mcp-label")
                description = EnhancedTextArea(
                    cfg.description or "",
                    id=f"mcp-desc-{self._index}",
                )
                description.read_only = self._read_only
                yield description

            with Vertical(
                classes="mcp-transport-fields mcp-option-section mcp-transport-option-section",
                id=f"mcp-transport-fields-{self._index}",
            ) as transport_section:
                transport_section.border_title = self._transport_options_title()
                yield from self._compose_transport_fields(cfg)

            with Vertical(classes="mcp-common-fields"):
                with Vertical(
                    id=f"mcp-prompts-instructions-section-{self._index}",
                    classes="mcp-option-section",
                ) as guidance_section:
                    guidance_section.border_title = render_str(localizer, _PROMPTS_AND_INSTRUCTIONS.bind())
                    expose_instructions_cb = Checkbox(
                        render_str(localizer, _EXPOSE_SERVER_INSTRUCTIONS.bind()),
                        value=cfg.expose_instructions,
                        id=f"mcp-expose-instructions-{self._index}",
                        classes="mcp-guidance-checkbox",
                    )
                    expose_instructions_cb.tooltip = render_str(
                        localizer,
                        _EXPOSE_INSTRUCTIONS_TOOLTIP.bind(app=APP_DISPLAY_NAME),
                    )
                    expose_instructions_cb.disabled = self._read_only
                    expose_instructions_cb.set_class(not cfg.expose_instructions, "-hint-collapsed")
                    yield expose_instructions_cb
                    expose_instructions_hint = Label(
                        render_str(localizer, _EXPOSE_INSTRUCTIONS_HINT.bind()),
                        id=f"mcp-expose-instructions-hint-{self._index}",
                        classes="mcp-checkbox-hint",
                    )
                    expose_instructions_hint.display = cfg.expose_instructions
                    yield expose_instructions_hint
                    load_prompts_cb = Checkbox(
                        render_str(localizer, _EXPOSE_SERVER_PROMPTS.bind()),
                        value=cfg.load_prompts,
                        id=f"mcp-load-prompts-{self._index}",
                        classes="mcp-guidance-checkbox",
                    )
                    load_prompts_cb.tooltip = render_str(localizer, _LOAD_PROMPTS_TOOLTIP.bind(app=APP_DISPLAY_NAME))
                    load_prompts_cb.disabled = self._read_only
                    load_prompts_cb.set_class(not cfg.load_prompts, "-hint-collapsed")
                    yield load_prompts_cb
                    load_prompts_hint = Label(
                        render_str(localizer, _LOAD_PROMPTS_HINT.bind()),
                        id=f"mcp-load-prompts-hint-{self._index}",
                        classes="mcp-checkbox-hint",
                    )
                    load_prompts_hint.display = cfg.load_prompts
                    yield load_prompts_hint

                access_mode = self._tool_access_mode(cfg.allowed_tools)
                with Vertical(
                    id=f"mcp-tool-access-section-{self._index}",
                    classes="mcp-option-section mcp-tool-access-section",
                ) as access_section:
                    access_section.border_title = render_str(localizer, _TOOL_ACCESS.bind())
                    yield Label(render_str(localizer, _PERMITTED_TOOL_SET.bind()), classes="mcp-label")
                    access = Select(
                        [(render_str(localizer, label.bind()), value) for label, value in _TOOL_ACCESS_OPTIONS],
                        value=access_mode,
                        id=f"mcp-tool-access-{self._index}",
                        allow_blank=False,
                    )
                    access.tooltip = render_str(localizer, _TOOL_ACCESS_TOOLTIP.bind())
                    access.disabled = self._read_only
                    yield access
                    selected_tools_fields = Vertical(
                        id=f"mcp-selected-tools-fields-{self._index}",
                        classes="mcp-policy-fields",
                    )
                    selected_tools_fields.display = access_mode == _TOOL_ACCESS_SELECTED
                    with selected_tools_fields:
                        yield Label(render_str(localizer, _SELECTED_TOOL_NAMES.bind()), classes="mcp-label")
                        allowed_tools_input = Input(
                            value=", ".join(cfg.allowed_tools or []),
                            placeholder=render_str(localizer, _SELECTED_TOOLS_PLACEHOLDER.bind()),
                            id=f"mcp-tools-{self._index}",
                        )
                        allowed_tools_input.disabled = access_mode != _TOOL_ACCESS_SELECTED or self._read_only
                        yield allowed_tools_input

                no_tools = access_mode == _TOOL_ACCESS_NONE
                loading_strategy = _TOOL_LOADING_PROGRESSIVE if cfg.use_progressive_disclosure else _TOOL_LOADING_FULL
                model_exposure_fields = Vertical(
                    id=f"mcp-model-exposure-fields-{self._index}",
                    classes="mcp-option-section",
                )
                model_exposure_fields.display = not no_tools
                with model_exposure_fields:
                    model_exposure_fields.border_title = render_str(localizer, _TOOL_LOADING.bind())
                    yield Label(render_str(localizer, _LOADING_STRATEGY.bind()), classes="mcp-label")
                    loading = Select(
                        [(render_str(localizer, label.bind()), value) for label, value in _TOOL_LOADING_OPTIONS],
                        value=loading_strategy,
                        id=f"mcp-loading-strategy-{self._index}",
                        allow_blank=False,
                    )
                    loading.tooltip = render_str(localizer, _PROGRESSIVE_DISCLOSURE_TOOLTIP.bind())
                    loading.disabled = no_tools or self._read_only
                    yield loading
                    always_load_fields = Vertical(
                        id=f"mcp-always-load-fields-{self._index}",
                        classes="mcp-progressive-fields",
                    )
                    always_load_fields.display = not no_tools and cfg.use_progressive_disclosure
                    with always_load_fields:
                        always_load_label = Label(
                            render_str(localizer, _INITIALLY_VISIBLE_TOOLS.bind()),
                            classes="mcp-label",
                        )
                        always_load_label.tooltip = render_str(localizer, _ALWAYS_LOAD_TOOLTIP.bind())
                        yield always_load_label
                        always_load = Input(
                            value=", ".join(cfg.always_load),
                            placeholder=render_str(localizer, _INITIAL_TOOLS_PLACEHOLDER.bind()),
                            id=f"mcp-always-load-{self._index}",
                        )
                        always_load.tooltip = render_str(localizer, _ALWAYS_LOAD_TOOLTIP.bind())
                        always_load.disabled = no_tools or not cfg.use_progressive_disclosure or self._read_only
                        yield always_load

                with Vertical(
                    id=f"mcp-naming-limits-section-{self._index}",
                    classes="mcp-option-section",
                ) as naming_section:
                    naming_section.border_title = render_str(localizer, _NAMING_AND_LIMITS.bind())
                    yield Label(render_str(localizer, _TOOL_NAME_PREFIX.bind()), classes="mcp-label")
                    prefix = Input(
                        value=cfg.tool_name_prefix,
                        placeholder=render_str(localizer, _TOOL_NAME_PREFIX_PLACEHOLDER.bind()),
                        id=f"mcp-prefix-{self._index}",
                        classes="mcp-naming-input",
                    )
                    prefix.disabled = self._read_only
                    yield prefix
                    yield Label(render_str(localizer, _REQUEST_TIMEOUT.bind()), classes="mcp-label")
                    timeout = Input(
                        value=self._format_timeout(cfg.request_timeout),
                        placeholder=render_str(
                            localizer,
                            _REQUEST_TIMEOUT_PLACEHOLDER.bind(default=DEFAULT_CONNECT_TIMEOUT_SECONDS),
                        ),
                        id=f"mcp-timeout-{self._index}",
                        classes="mcp-naming-input",
                    )
                    timeout.disabled = self._read_only
                    yield timeout

            with Horizontal(classes="mcp-footer-row"):
                with Horizontal(classes="mcp-footer-left"):
                    enabled = Checkbox(
                        render_str(localizer, _ENABLED.bind()),
                        value=cfg.enabled,
                        id=f"mcp-enabled-{self._index}",
                    )
                    enabled.disabled = self._read_only
                    yield enabled
                test_button = ConfigAddButton(
                    render_str(localizer, _TEST.bind()),
                    id=f"mcp-test-btn-{self._index}",
                    classes="mcp-test-btn",
                )
                test_button.disabled = self._read_only
                test_button.display = not self._read_only
                yield test_button

    def _header_title(self, name: str) -> str:
        # An unnamed server is told apart by position, with the label its Save errors use.
        return name.strip() or render_str(widget_localizer(self), MCP_SERVER_CONTEXT.bind(index=self._index + 1))

    @on(Input.Changed)
    def _on_name_changed(self, event: Input.Changed) -> None:
        # Not stopped: the config screen's dirty tracking also reads this event.
        if event.input.id == f"mcp-name-{self._index}":
            self.set_title(self._header_title(event.value))

    def _compose_transport_fields(self, cfg: MCPServerConfig) -> ComposeResult:
        localizer = widget_localizer(self)
        if self._transport == "http":
            yield Label(f"[red]*[/red] {escape(render_str(localizer, _URL.bind()))}", classes="mcp-label")
            url = Input(
                value=cfg.url or "",
                placeholder=render_str(localizer, _URL_PLACEHOLDER.bind()),
                id=f"mcp-url-{self._index}",
            )
            url.disabled = self._read_only
            yield url
            skip_tls = not cfg.verify_ssl
            skip_tls_checkbox = Checkbox(
                render_str(localizer, _SKIP_TLS_VERIFICATION.bind()),
                value=skip_tls,
                id=f"mcp-skip-tls-{self._index}",
                classes="mcp-skip-tls",
            )
            skip_tls_checkbox.disabled = self._read_only
            yield skip_tls_checkbox
            hint = Label(
                render_str(localizer, _INSECURE_TLS.bind()),
                id=f"mcp-skip-tls-hint-{self._index}",
                classes="mcp-skip-tls-hint",
            )
            hint.display = skip_tls
            yield hint
            bypass_proxy = Checkbox(
                render_str(localizer, _BYPASS_PROXY.bind()),
                value=cfg.bypass_proxy,
                id=f"mcp-bypass-proxy-{self._index}",
                classes="mcp-bypass-proxy",
            )
            bypass_proxy.disabled = self._read_only
            yield bypass_proxy
            yield Label(render_str(localizer, _HEADERS.bind()), classes="mcp-label")
            with Vertical(classes="mcp-list", id=f"mcp-headers-{self._index}"):
                header_rows = self._key_value_drafts.headers
                if header_rows is None:
                    header_rows = list(cfg.headers.items())
                for key, val in header_rows:
                    yield self._compose_key_value_row(_MCP_HEADER_ROWS, key, val)
                if not header_rows:
                    yield self._compose_key_value_row(_MCP_HEADER_ROWS, "", "")
                yield self._make_key_value_add_row(_MCP_HEADER_ROWS)
            return

        yield Label(f"[red]*[/red] {escape(render_str(localizer, _COMMAND.bind()))}", classes="mcp-label")
        command_line_text = (
            self._command_line_text
            if self._command_line_text is not None
            else self._format_command_line(cfg.command, cfg.args)
        )
        command = EnhancedTextArea(
            command_line_text,
            id=f"mcp-cmd-{self._index}",
            placeholder=render_str(localizer, _COMMAND_PLACEHOLDER.bind()),
        )
        command.read_only = self._read_only
        yield command
        yield Label(render_str(localizer, _ENVIRONMENT_VARIABLES.bind()), classes="mcp-label")
        with Vertical(classes="mcp-list", id=f"mcp-env-{self._index}"):
            env_rows = self._key_value_drafts.env
            if env_rows is None:
                env_rows = list(cfg.env.items())
            for key, val in env_rows:
                yield self._compose_key_value_row(_MCP_ENV_ROWS, key, val)
            if not env_rows:
                yield self._compose_key_value_row(_MCP_ENV_ROWS, "", "")
            yield self._make_key_value_add_row(_MCP_ENV_ROWS)

    def _transport_options_title(self) -> str:
        definition = _HTTP_OPTIONS if self._transport == "http" else _STDIO_OPTIONS
        return render_str(widget_localizer(self), definition.bind())

    @classmethod
    def _next_uid(cls) -> int:
        cls._uid_counter += 1
        return cls._uid_counter

    def _compose_key_value_row(self, spec: _MCPKeyValueRows, key: str, val: str) -> Horizontal:
        uid = f"{self._index}-{self._next_uid()}"
        localizer = widget_localizer(self)
        key_input = Input(
            value=key,
            placeholder=render_str(localizer, spec.key_placeholder.bind()),
            classes="mcp-field mcp-item-key-input",
        )
        key_input.disabled = self._read_only
        value_input = Input(
            value=val,
            placeholder=render_str(localizer, spec.value_placeholder.bind()),
            classes="mcp-field mcp-item-value-input",
        )
        value_input.disabled = self._read_only
        remove_button = Button("✕", classes="mcp-remove-btn", id=f"mcp-{spec.action_prefix}del-{uid}")
        remove_button.disabled = self._read_only
        remove_button.display = not self._read_only
        return Horizontal(
            key_input,
            value_input,
            remove_button,
            classes="mcp-item-row",
        )

    def _key_value_container_id(self, spec: _MCPKeyValueRows) -> str:
        return f"mcp-{spec.container_suffix}-{self._index}"

    def _key_value_add_button_id(self, spec: _MCPKeyValueRows) -> str:
        return f"mcp-{spec.action_prefix}add-{self._index}"

    def _key_value_add_row_id(self, spec: _MCPKeyValueRows) -> str:
        return f"mcp-{spec.action_prefix}add-row-{self._index}"

    def _key_value_delete_prefix(self, spec: _MCPKeyValueRows) -> str:
        return f"mcp-{spec.action_prefix}del-{self._index}-"

    def _mark_key_value_user_modified(self, spec: _MCPKeyValueRows) -> None:
        if spec is _MCP_HEADER_ROWS:
            self._headers_user_modified = True
            return
        if spec is _MCP_ENV_ROWS:
            self._env_user_modified = True
            return
        raise ValueError("Unknown MCP key/value row spec")

    def _make_key_value_add_row(self, spec: _MCPKeyValueRows) -> Horizontal:
        add_button = ConfigAddButton(
            render_str(widget_localizer(self), _ADD.bind()),
            compact=True,
            classes="mcp-small-btn",
            id=self._key_value_add_button_id(spec),
        )
        add_button.disabled = self._read_only
        add_button.display = not self._read_only
        return Horizontal(
            add_button,
            classes="mcp-row",
            id=self._key_value_add_row_id(spec),
        )

    async def _add_key_value_row(self, spec: _MCPKeyValueRows) -> None:
        if self._read_only:
            return
        try:
            container = self.query_one(f"#{self._key_value_container_id(spec)}", Vertical)
            add_row = self.query_one(f"#{self._key_value_add_row_id(spec)}", Horizontal)
        except Exception:
            return

        self._mark_key_value_user_modified(spec)
        await container.mount(self._compose_key_value_row(spec, "", ""), before=add_row)
        container.refresh(layout=True)
        self.refresh(layout=True)

    @on(Checkbox.Changed)
    def _on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if self._read_only:
            return
        hint_id_by_checkbox_id = {
            f"mcp-skip-tls-{self._index}": f"mcp-skip-tls-hint-{self._index}",
            f"mcp-load-prompts-{self._index}": f"mcp-load-prompts-hint-{self._index}",
            f"mcp-expose-instructions-{self._index}": f"mcp-expose-instructions-hint-{self._index}",
        }
        hint_id = hint_id_by_checkbox_id.get(event.checkbox.id or "")
        if hint_id is None:
            return
        with contextlib.suppress(Exception):
            hint = self.query_one(f"#{hint_id}", Label)
            hint.display = bool(event.value)
            event.checkbox.set_class(not event.value, "-hint-collapsed")

    @on(Select.Changed)
    async def _on_select_changed(self, event: Select.Changed) -> None:
        if self._read_only:
            return
        widget_id = event.select.id or ""
        if widget_id in {
            f"mcp-tool-access-{self._index}",
            f"mcp-loading-strategy-{self._index}",
        }:
            self._sync_tool_policy_fields()
            return
        if widget_id != f"mcp-transport-{self._index}":
            return
        transport = str(event.value)
        if not _is_transport(transport) or transport == self._transport:
            return
        self._transport = transport
        await self._rebuild_transport_fields()

    @on(Button.Pressed)
    async def _on_button(self, event: Button.Pressed) -> None:
        if self._read_only:
            return
        button = event.button
        if button.id == f"mcp-test-btn-{self._index}":
            self.post_message(self.TestRequested(self))
            return

        if button.id == self._key_value_add_button_id(_MCP_HEADER_ROWS):
            await self._add_key_value_row(_MCP_HEADER_ROWS)
            return

        if button.id == self._key_value_add_button_id(_MCP_ENV_ROWS):
            await self._add_key_value_row(_MCP_ENV_ROWS)
            return

        button_id = button.id or ""
        for spec in (_MCP_HEADER_ROWS, _MCP_ENV_ROWS):
            if button_id.startswith(self._key_value_delete_prefix(spec)):
                # Await the removal so the row is gone from the DOM before this
                # handler returns and Button.Pressed bubbles to AgentsConfigScreen.
                # The screen's call_after_refresh dirty re-evaluation rebuilds the
                # profile from the mounted rows; if the row were still present (the
                # prior fire-and-forget row.remove()/call_later races lost this on
                # CI) the rebuilt profile would match saved and clear the dirty flag.
                await self._remove_key_value_row(spec, button)
                return

    async def _remove_key_value_row(self, spec: _MCPKeyValueRows, button: Button) -> None:
        if self._read_only:
            return
        self._mark_key_value_user_modified(spec)
        row = button.parent
        if isinstance(row, Widget) and isinstance(row.parent, Vertical):
            container = row.parent
            was_last_row = len(list(container.query(".mcp-item-row"))) <= 1
            if was_last_row:
                row.query_one(".mcp-item-key-input", Input).value = ""
                row.query_one(".mcp-item-value-input", Input).value = ""
                row.refresh(layout=True)
            else:
                await row.remove()
            self.refresh(layout=True)

    async def _rebuild_transport_fields(self) -> None:
        container = self.query_one(f"#mcp-transport-fields-{self._index}", Vertical)
        await container.remove_children()
        container.border_title = self._transport_options_title()
        if self._transport == "http":
            self._headers_user_modified = True
        else:
            self._env_user_modified = True

        # Build the new subtree fully composed before mounting.  Mounting
        # an empty container and then mounting children into it races on
        # Windows: ``await parent.mount(child)`` doesn't always leave
        # ``child.is_attached`` True by the time the next mount fires.
        new_widgets: list[Widget] = []
        if self._transport == "http":
            localizer = widget_localizer(self)
            hint = Label(
                render_str(localizer, _INSECURE_TLS.bind()),
                id=f"mcp-skip-tls-hint-{self._index}",
                classes="mcp-skip-tls-hint",
            )
            hint.display = False
            headers_container = Vertical(
                self._compose_key_value_row(_MCP_HEADER_ROWS, "", ""),
                self._make_key_value_add_row(_MCP_HEADER_ROWS),
                classes="mcp-list",
                id=f"mcp-headers-{self._index}",
            )
            new_widgets.extend(
                [
                    Label(f"[red]*[/red] {escape(render_str(localizer, _URL.bind()))}", classes="mcp-label"),
                    Input(
                        placeholder=render_str(localizer, _URL_PLACEHOLDER.bind()),
                        id=f"mcp-url-{self._index}",
                    ),
                    Checkbox(
                        render_str(localizer, _SKIP_TLS_VERIFICATION.bind()),
                        value=False,
                        id=f"mcp-skip-tls-{self._index}",
                        classes="mcp-skip-tls",
                    ),
                    hint,
                    Checkbox(
                        render_str(localizer, _BYPASS_PROXY.bind()),
                        value=False,
                        id=f"mcp-bypass-proxy-{self._index}",
                        classes="mcp-bypass-proxy",
                    ),
                    Label(render_str(localizer, _HEADERS.bind()), classes="mcp-label"),
                    headers_container,
                ]
            )
        else:
            localizer = widget_localizer(self)
            env_container = Vertical(
                self._compose_key_value_row(_MCP_ENV_ROWS, "", ""),
                self._make_key_value_add_row(_MCP_ENV_ROWS),
                classes="mcp-list",
                id=f"mcp-env-{self._index}",
            )
            new_widgets.extend(
                [
                    Label(f"[red]*[/red] {escape(render_str(localizer, _COMMAND.bind()))}", classes="mcp-label"),
                    EnhancedTextArea(
                        id=f"mcp-cmd-{self._index}",
                        placeholder=render_str(localizer, _COMMAND_PLACEHOLDER.bind()),
                    ),
                    Label(render_str(localizer, _ENVIRONMENT_VARIABLES.bind()), classes="mcp-label"),
                    env_container,
                ]
            )

        await container.mount(*new_widgets)
        container.refresh(layout=True)
        self.refresh(layout=True)

    def get_config(self) -> MCPServerConfig:
        """Read current widget state into an MCPServerConfig."""
        return self._read_config(default_blank_name=True)

    def _snapshot_config(self) -> MCPServerConfig:
        """Read widget state without normalizing incomplete user input."""
        return self._read_config(default_blank_name=False, allow_invalid_command=True)

    def _snapshot_command_line_text(self) -> str | None:
        """Return raw stdio command text for UI-only rebuild preservation."""
        if self._transport != "stdio":
            return None
        with contextlib.suppress(NoMatches):
            return self.query_one(f"#mcp-cmd-{self._index}", EnhancedTextArea).text
        return self._command_line_text

    def _snapshot_key_value_rows(self, spec: _MCPKeyValueRows) -> list[tuple[str, str]] | None:
        """Return raw header/env rows for UI-only rebuild preservation."""
        if spec is _MCP_HEADER_ROWS and self._transport != "http":
            return None
        if spec is _MCP_ENV_ROWS and self._transport != "stdio":
            return None

        with contextlib.suppress(NoMatches):
            container = self.query_one(f"#{self._key_value_container_id(spec)}", Vertical)
            rows: list[tuple[str, str]] = []
            for row in container.query(".mcp-item-row"):
                key = row.query_one(".mcp-item-key-input", Input).value
                value = row.query_one(".mcp-item-value-input", Input).value
                rows.append((key, value))
            return rows

        if spec is _MCP_HEADER_ROWS:
            return self._key_value_drafts.headers
        return self._key_value_drafts.env

    def _snapshot_key_value_drafts(self) -> _MCPKeyValueDrafts:
        """Return raw key/value rows for UI-only rebuild preservation."""
        return _MCPKeyValueDrafts(
            headers=self._snapshot_key_value_rows(_MCP_HEADER_ROWS),
            env=self._snapshot_key_value_rows(_MCP_ENV_ROWS),
        )

    def _read_config(self, *, default_blank_name: bool, allow_invalid_command: bool = False) -> MCPServerConfig:
        from chrys.service.profiles.agents.schema import MCPServerConfig as MCPCfg

        enabled = self._config.enabled
        with contextlib.suppress(NoMatches):
            enabled = bool(self.query_one(f"#mcp-enabled-{self._index}", Checkbox).value)

        name = self._config.name.strip()
        with contextlib.suppress(NoMatches):
            name = self.query_one(f"#mcp-name-{self._index}", Input).value.strip()
        transport = self._transport
        description = self._config.description.strip()
        with contextlib.suppress(NoMatches):
            description = self.query_one(f"#mcp-desc-{self._index}", EnhancedTextArea).text.strip()
        access_mode = self._tool_access_mode(self._config.allowed_tools)
        with contextlib.suppress(NoMatches):
            access_mode = str(self.query_one(f"#mcp-tool-access-{self._index}", Select).value)
        if access_mode == _TOOL_ACCESS_ALL:
            allowed_tools = None
        elif access_mode == _TOOL_ACCESS_NONE:
            allowed_tools = []
        else:
            allowed_tools = list(self._config.allowed_tools or [])
            with contextlib.suppress(NoMatches):
                allowed_tools = self._parse_allowed_tools(self.query_one(f"#mcp-tools-{self._index}", Input).value)
        tool_name_prefix = self._config.tool_name_prefix.strip()
        with contextlib.suppress(NoMatches):
            tool_name_prefix = self.query_one(f"#mcp-prefix-{self._index}", Input).value.strip()

        timeout = self._config.request_timeout
        with contextlib.suppress(NoMatches):
            timeout_text = self.query_one(f"#mcp-timeout-{self._index}", Input).value.strip()
            timeout = None
            if timeout_text:
                with contextlib.suppress(ValueError):
                    timeout = int(timeout_text)

        url = self._config.url if transport == "http" else ""
        command = self._config.command if transport == "stdio" else ""
        args = list(self._config.args) if transport == "stdio" else []
        headers = dict(self._config.headers) if transport == "http" else {}
        env = dict(self._config.env) if transport == "stdio" else {}
        verify_ssl = self._config.verify_ssl if transport == "http" else True
        bypass_proxy = self._config.bypass_proxy if transport == "http" else False

        if transport == "http":
            with contextlib.suppress(NoMatches):
                url = self.query_one(f"#mcp-url-{self._index}", Input).value.strip()
            with contextlib.suppress(NoMatches):
                skip_tls = bool(self.query_one(f"#mcp-skip-tls-{self._index}", Checkbox).value)
                verify_ssl = not skip_tls
            with contextlib.suppress(NoMatches):
                bypass_proxy = bool(self.query_one(f"#mcp-bypass-proxy-{self._index}", Checkbox).value)
            # Keep seeded headers unless the mounted rows can be read as a complete set.
            try:
                parsed_headers = self._parse_key_value_rows(
                    self._key_value_container_id(_MCP_HEADER_ROWS),
                    self._key_value_add_row_id(_MCP_HEADER_ROWS),
                    user_modified=self._headers_user_modified,
                )
            except NoMatches:
                pass
            else:
                headers = parsed_headers
        else:
            with contextlib.suppress(NoMatches):
                command_line = self.query_one(f"#mcp-cmd-{self._index}", EnhancedTextArea).text.strip()
                try:
                    command, args = self._split_command_line(command_line, render_message=self._render_message)
                except ValueError:
                    if not allow_invalid_command:
                        raise
            # Keep seeded env unless the mounted rows can be read as a complete set.
            try:
                parsed_env = self._parse_key_value_rows(
                    self._key_value_container_id(_MCP_ENV_ROWS),
                    self._key_value_add_row_id(_MCP_ENV_ROWS),
                    user_modified=self._env_user_modified,
                )
            except NoMatches:
                pass
            else:
                env = parsed_env

        load_prompts = self._config.load_prompts
        with contextlib.suppress(NoMatches):
            load_prompts = bool(self.query_one(f"#mcp-load-prompts-{self._index}", Checkbox).value)
        expose_instructions = self._config.expose_instructions
        with contextlib.suppress(NoMatches):
            expose_instructions = bool(self.query_one(f"#mcp-expose-instructions-{self._index}", Checkbox).value)
        loading_strategy = _TOOL_LOADING_PROGRESSIVE if self._config.use_progressive_disclosure else _TOOL_LOADING_FULL
        with contextlib.suppress(NoMatches):
            loading_strategy = str(self.query_one(f"#mcp-loading-strategy-{self._index}", Select).value)
        use_progressive_disclosure = access_mode != _TOOL_ACCESS_NONE and loading_strategy == _TOOL_LOADING_PROGRESSIVE
        always_load: list[str] = []
        if use_progressive_disclosure:
            always_load = list(self._config.always_load)
            with contextlib.suppress(NoMatches):
                always_load = self._parse_allowed_tools(self.query_one(f"#mcp-always-load-{self._index}", Input).value)

        if default_blank_name and not name:
            name = f"server-{self._index}"

        return MCPCfg(
            name=name,
            transport=transport,
            command=command,
            args=args,
            encoding=self._config.encoding if transport == "stdio" else None,
            env=env,
            url=url,
            headers=headers,
            resolve_header_templates=self._config.resolve_header_templates if transport == "http" else True,
            terminate_on_close=self._config.terminate_on_close if transport == "http" else None,
            verify_ssl=verify_ssl,
            bypass_proxy=bypass_proxy,
            enabled=enabled,
            description=description,
            tool_name_prefix=tool_name_prefix,
            allowed_tools=allowed_tools,
            request_timeout=timeout,
            max_tool_result_tokens=self._config.max_tool_result_tokens,
            load_prompts=load_prompts,
            expose_instructions=expose_instructions,
            use_progressive_disclosure=use_progressive_disclosure,
            always_load=always_load,
        )

    def validate(self) -> list[str]:
        """Return validation errors for this server."""
        localizer = widget_localizer(self)
        errors: list[str] = []
        name = self.query_one(f"#mcp-name-{self._index}", Input).value.strip()
        display_name = name or render_str(localizer, MCP_SERVER_CONTEXT.bind(index=self._index + 1))

        if not name:
            context = render_str(localizer, MCP_SERVER_CONTEXT.bind(index=self._index + 1))
            errors.append(
                self._render_context_error(
                    context,
                    FIELD_REQUIRED.bind(field=render_str(localizer, NAME_FIELD.bind())),
                )
            )

        if self._transport == "http":
            with contextlib.suppress(Exception):
                url = self.query_one(f"#mcp-url-{self._index}", Input).value.strip()
                if not url:
                    errors.append(self._render_server_error(display_name, MCP_URL_REQUIRED.bind()))
                elif not url.startswith(("http://", "https://")):
                    errors.append(self._render_server_error(display_name, MCP_URL_SCHEME.bind()))
        else:
            with contextlib.suppress(Exception):
                command_line = self.query_one(f"#mcp-cmd-{self._index}", EnhancedTextArea).text.strip()
                if not command_line:
                    errors.append(self._render_server_error(display_name, MCP_COMMAND_REQUIRED.bind()))
                else:
                    try:
                        command, _args = self._split_command_line(command_line, render_message=self._render_message)
                        if not command:
                            errors.append(self._render_server_error(display_name, MCP_COMMAND_REQUIRED.bind()))
                    except ValueError as exc:
                        errors.append(self._render_server_message(display_name, str(exc)))

        timeout_text = self.query_one(f"#mcp-timeout-{self._index}", Input).value.strip()
        if timeout_text:
            try:
                timeout = int(timeout_text)
                if timeout <= 0:
                    errors.append(self._render_server_error(display_name, MCP_TIMEOUT_POSITIVE.bind()))
            except ValueError:
                errors.append(self._render_server_error(display_name, MCP_TIMEOUT_VALID.bind()))

        prefix_text = self.query_one(f"#mcp-prefix-{self._index}", Input).value.strip()

        access_mode = str(self.query_one(f"#mcp-tool-access-{self._index}", Select).value)
        selected_tools_text = self.query_one(f"#mcp-tools-{self._index}", Input).value
        selected_tools = self._parse_allowed_tools(selected_tools_text)
        if access_mode == _TOOL_ACCESS_SELECTED:
            errors.extend(
                self._render_server_message(display_name, error)
                for error in self._validate_comma_separated_tool_names(
                    selected_tools_text,
                    label_definition=_SELECTED_TOOL_NAMES,
                )
            )
            if not selected_tools:
                errors.append(
                    self._render_server_error(
                        display_name,
                        MCP_SELECT_PERMITTED.bind(
                            no_tools=render_str(localizer, _TOOL_ACCESS_NONE_LABEL.bind()),
                        ),
                    )
                )

        loading_strategy = str(self.query_one(f"#mcp-loading-strategy-{self._index}", Select).value)
        progressive = access_mode != _TOOL_ACCESS_NONE and loading_strategy == _TOOL_LOADING_PROGRESSIVE
        prefix_error = validate_mcp_tool_name_prefix(
            prefix_text,
            generated_suffixes=MCP_PROGRESSIVE_CONTROL_TOOL_NAMES if progressive else (),
        )
        if prefix_error is not None:
            errors.append(self._render_server_message(display_name, prefix_error))
        if progressive:
            always_load_text = self.query_one(f"#mcp-always-load-{self._index}", Input).value
            always_load = self._parse_allowed_tools(always_load_text)
            errors.extend(
                self._render_server_message(display_name, error)
                for error in self._validate_comma_separated_tool_names(
                    always_load_text,
                    label_definition=MCP_INITIALLY_VISIBLE_TOOLS_VALIDATION,
                )
            )
            if access_mode == _TOOL_ACCESS_SELECTED:
                outside_scope = [name for name in always_load if name not in set(selected_tools)]
                if outside_scope:
                    errors.append(
                        self._render_server_error(
                            display_name,
                            MCP_INITIAL_TOOLS_PERMITTED.bind(
                                label=render_str(localizer, MCP_INITIALLY_VISIBLE_TOOLS_VALIDATION.bind()),
                                names=DisplayBlock(", ".join(outside_scope)),
                            ),
                        )
                    )

        if self._transport == "http":
            with contextlib.suppress(Exception):
                errors.extend(
                    self._validate_key_value_rows(
                        self._key_value_container_id(_MCP_HEADER_ROWS),
                        _HEADERS,
                        display_name,
                    )
                )

        if self._transport == "stdio":
            with contextlib.suppress(Exception):
                errors.extend(
                    self._validate_key_value_rows(
                        self._key_value_container_id(_MCP_ENV_ROWS),
                        _ENVIRONMENT_VARIABLES,
                        display_name,
                    )
                )

        return errors

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _render_context_error(self, context: str, reference: MessageRef) -> str:
        return self._render_message(
            CONTEXT_ERROR.bind(
                context=DisplayBlock(context),
                message=DisplayBlock(self._render_message(reference)),
            )
        )

    def _render_server_error(self, display_name: str, reference: MessageRef) -> str:
        return self._render_server_message(display_name, self._render_message(reference))

    def _render_server_message(self, display_name: str, message: str) -> str:
        return self._render_message(
            SERVER_ERROR.bind(
                name=DisplayBlock(display_name),
                message=DisplayBlock(message),
            )
        )

    def _validate_key_value_rows(
        self,
        container_id: str,
        label_definition: MessageDef,
        display_name: str,
    ) -> list[str]:
        """Validate that partially-filled key-value rows are not saved silently."""
        localizer = widget_localizer(self)
        label = render_str(localizer, label_definition.bind())
        errors: list[str] = []
        seen: set[str] = set()
        container = self.query_one(f"#{container_id}", Vertical)
        for index, row in enumerate(container.query(".mcp-item-row"), start=1):
            key = row.query_one(".mcp-item-key-input", Input).value.strip()
            value = row.query_one(".mcp-item-value-input", Input).value.strip()
            if not key and not value:
                continue
            if key and not value:
                errors.append(
                    self._render_server_error(
                        display_name,
                        MCP_ROW_VALUE_REQUIRED.bind(
                            label=label,
                            row=index,
                            key=DisplayBlock(key),
                        ),
                    )
                )
            elif value and not key:
                errors.append(
                    self._render_server_error(
                        display_name,
                        MCP_ROW_KEY_REQUIRED.bind(label=label, row=index),
                    )
                )
            if key in seen:
                errors.append(
                    self._render_server_error(
                        display_name,
                        DUPLICATE_KEY_ROW.bind(
                            label=label,
                            row=index,
                            key=DisplayBlock(key),
                        ),
                    )
                )
            if key:
                seen.add(key)
        return errors


class MCPConfigPanel(VerticalScroll):
    """Composable widget for the MCP configuration tab."""

    DEFAULT_CSS = """
    MCPConfigPanel {
        height: 1fr;
        padding: 0 0 0 2;
        scrollbar-size-vertical: 1;
    }
    MCPConfigPanel .mcp-section-title {
        color: $secondary;
        text-style: bold;
        height: 1;
    }
    MCPConfigPanel .mcp-section-desc {
        color: $text-muted;
        height: 1;
        margin: 0 0 1 0;
    }
    MCPConfigPanel .mcp-field {
        width: 1fr;
        height: auto;
        margin: 0 1 0 0;
    }
    MCPConfigPanel #mcp-add-btn {
        min-width: 10;
        height: 1;
        margin: 0;
    }
    MCPConfigPanel .mcp-header-bar {
        height: auto;
        margin: 0 0 1 0;
    }
    MCPConfigPanel #mcp-cards {
        height: auto;
    }
    MCPConfigPanel .mcp-empty {
        margin: 0 2 1 0;
    }
    """

    def __init__(
        self,
        mcp_servers: list[MCPServerConfig] | None = None,
        *,
        card_folds: Sequence[bool] | None = None,
        workspace_cwd: str | None = None,
        read_only: bool = False,
        additional_reserved_tool_names: Callable[[], Collection[str]] | None = None,
        reuse_choices: Callable[[], MCPChoices] | None = None,
    ) -> None:
        servers = list(mcp_servers or [])
        # Folds from an earlier panel for these servers (see ``card_folds``); otherwise saved servers open folded.
        folds = list(card_folds) if card_folds is not None and len(card_folds) == len(servers) else None
        self._rows = [
            _MCPServerRow(server, collapsed=True if folds is None else folds[index])
            for index, server in enumerate(servers)
        ]
        self._read_only = read_only
        self._additional_reserved_tool_names = additional_reserved_tool_names
        self._reuse_choices = reuse_choices
        self._testing: set[int] = set()
        self._adapter = MCPAdapter(
            stdio_cwd=workspace_cwd or None,
            reserved_tool_names=self._current_reserved_tool_names(),
        )
        super().__init__()

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _current_reserved_tool_names(self) -> set[str]:
        names = chrys_reserved_tool_names()
        if self._additional_reserved_tool_names is not None:
            names.update(self._additional_reserved_tool_names())
        return names

    @property
    def test_adapter(self) -> MCPAdapter:
        """Expose adapter for tests."""
        return self._adapter

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with Vertical(classes="mcp-header-bar"), Vertical(classes="mcp-field"):
            yield Label(render_str(localizer, _SERVERS.bind()), classes="mcp-section-title")
            yield Label(render_str(localizer, _SERVERS_DESCRIPTION.bind()), classes="mcp-section-desc")
            add_button = ConfigAddButton(render_str(localizer, _ADD.bind()), id="mcp-add-btn")
            add_button.disabled = self._read_only
            add_button.display = not self._read_only
            yield add_button
        yield Vertical(id="mcp-cards")

    async def on_mount(self) -> None:
        await self._rebuild_cards()

    async def _rebuild_cards(self) -> None:
        container = self.query_one("#mcp-cards", Vertical)
        focused = self.screen.focused
        if focused is not None and container in focused.ancestors and self.focusable:
            # Removing the focused card (its ✕ after a delete) makes Textual focus a neighbour and
            # animate the scroll to it; the panel itself stays put and keeps the keyboard.
            self.screen.set_focus(self, scroll_visible=False)
        replacements = (
            [
                MCPConnectionCard(
                    row.config,
                    index,
                    command_line_text=row.command_line_text,
                    key_value_drafts=row.key_value_drafts,
                    read_only=self._read_only,
                    collapsed=row.collapsed,
                )
                for index, row in enumerate(self._rows)
            ]
            if self._rows
            else [HatchedEmptyState(render_str(widget_localizer(self), _EMPTY.bind()), classes="mcp-empty")]
        )
        # One batch: a layout between the removal and the mount would clamp the scroll to the emptied list.
        with self.app.batch_update():
            await container.remove_children()
            await container.mount(*replacements)

    @on(Button.Pressed, "#mcp-add-btn")
    async def _on_add(self, _event: Button.Pressed) -> None:
        if self._read_only:
            return
        if self._refuse_while_testing(_WAIT_BEFORE_ADDING):
            return
        if self.screen is not self.app.screen:
            return
        choices = self._reuse_choices() if self._reuse_choices is not None else MCPChoices()
        if not choices.choices:
            await self._add_selected(MCPPickerAction.CREATE)
            return
        current = [row.config for row in self._snapshot_rows(preserve_blank_names=True)]
        self.app.push_screen(MCPPickerDialog(choices, current), callback=self._add_selected)

    async def _add_selected(self, result: MCPPickerResult) -> None:
        if result is None or self._read_only:
            return
        if not self.is_attached or self._pruning or self._closing:
            return
        if not composed_live(self.screen) or self.screen is not self.app.screen:
            return
        if self._refuse_while_testing(_WAIT_BEFORE_ADDING):
            return
        from chrys.service.profiles.agents.schema import MCPServerConfig

        # Read live cards again: queued selections must not duplicate a prior addition,
        # and invalid command text must not leave name checks reading a stale agent draft.
        rows = self._snapshot_rows(preserve_blank_names=True)
        current = [row.config for row in rows]
        if result is MCPPickerAction.CREATE:
            names = {mcp_name_key(config.name) for config in current}
            name = "New Server"
            suffix = 2
            while mcp_name_key(name) in names:
                name = f"New Server {suffix}"
                suffix += 1
            config = MCPServerConfig(name=name, transport="stdio")
        else:
            conflict = mcp_choice_conflict(result, current)
            if conflict is not None:
                self.notify(self._render_message(conflict_message(conflict)), markup=False)
                return
            config = deepcopy(result)
        self._rows = rows
        self._rows.insert(0, _MCPServerRow(config, collapsed=False))
        await self._rebuild_cards()
        self.scroll_home(animate=False)
        self.post_message(CardListChanged())

    def _refuse_while_testing(self, reason: MessageDef) -> bool:
        """Tell the user why the card list can't change yet; tests hold card indexes until they finish."""
        if not self._testing:
            return False
        localizer = widget_localizer(self)
        self.notify(
            render_str(localizer, reason.bind()),
            title=render_str(localizer, _MCP_TITLE.bind()),
            timeout=3,
            markup=False,
        )
        return True

    @on(MCPConnectionCard.Removed)
    def _on_remove(self, event: MCPConnectionCard.Removed) -> None:
        card = event.card
        if self._read_only or not self._shows(card) or self._refuse_while_testing(_WAIT_BEFORE_REMOVING):
            return
        dialog = ConfirmDialog(
            title=_DELETE_SERVER_TITLE.bind(),
            message=_DELETE_SERVER_MESSAGE.bind(name=card.header_title),
            confirm_label=_DELETE_SERVER_CONFIRM.bind(),
            confirm_variant="error",
            locale_controller=widget_locale_controller(self),
        )
        self.app.push_screen(dialog, callback=partial(self._remove_confirmed, card))

    async def _remove_confirmed(self, card: MCPConnectionCard, confirmed: bool | None) -> None:
        if not confirmed or self._read_only or self._refuse_while_testing(_WAIT_BEFORE_REMOVING):
            return
        if not self._shows(card):
            # The cards were rebuilt while the dialog was open (or a second ✕ press
            # opened a second dialog): the confirmation names a card that is gone.
            return
        index = card.index
        self._rows = self._snapshot_rows(preserve_blank_names=True)
        if 0 <= index < len(self._rows):
            self._rows.pop(index)
            await self._rebuild_cards()
            self.post_message(CardListChanged())

    @on(MCPConnectionCard.TestRequested)
    def _on_test_requested(self, event: MCPConnectionCard.TestRequested) -> None:
        card = event.card
        if self._read_only or not self._shows(card) or card.index in self._testing:
            return
        # Held until the test ends; adding and removing servers wait for it, so the index stays put.
        self._testing.add(card.index)
        self._set_test_button_busy(card, True)
        self._run_test_requested(card)

    @work(thread=False)
    async def _run_test_requested(self, card: MCPConnectionCard) -> None:
        index = card.index
        server_name = ""
        with contextlib.suppress(Exception):
            server_name = card.query_one(f"#mcp-name-{index}", Input).value.strip()

        dialog = ConnectionTestDialog(server_name=server_name, subject_label=_MCP_SERVER_SUBJECT.bind())
        self.app.push_screen(dialog)

        try:
            errors = card.validate()
            if errors:
                dialog.set_result(success=False, message="\n".join(errors))
                return

            config = card.get_config()
            self._adapter.set_reserved_tool_names(self._current_reserved_tool_names())
            report = await self._adapter.test_connection(config)
        except asyncio.CancelledError as exc:
            logger.info("MCP connection test cancelled: %s", self._exception_chain_summary(exc))
            dialog.set_result(
                success=False,
                message=self._format_connection_error(exc, render_message=self._render_message),
            )
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
        except Exception as exc:
            kind = self._classify_connection_error(exc)
            logger.warning(
                "MCP connection test failed (%s): %s",
                kind,
                self._exception_chain_summary(exc),
                exc_info=exc,
            )
            dialog.set_result(
                success=False,
                message=self._format_connection_error(exc, render_message=self._render_message),
                allow_esc=(kind == "timeout"),
            )
        else:
            # Test doubles may stub test_connection to return None; keep the
            # plain confirmation for that shape instead of crashing the worker.
            if report is None:
                dialog.set_result(success=True, message=self._render_message(_CONNECTION_SUCCESSFUL.bind()))
            else:
                dialog.set_result(
                    success=True,
                    message=_format_mcp_report(config, report, render_message=self._render_message),
                    markdown=True,
                )
        finally:
            self._testing.discard(index)
            self._set_test_button_busy(card, False)

    @staticmethod
    def _exception_chain(exc: BaseException) -> list[BaseException]:
        chain: list[BaseException] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            chain.append(current)
            seen.add(id(current))
            next_exc = current.__cause__ if current.__cause__ is not None else current.__context__
            current = next_exc if isinstance(next_exc, BaseException) else None
        return chain

    @classmethod
    def _classify_connection_error(cls, exc: BaseException) -> str:
        chain = cls._exception_chain(exc)

        if any(isinstance(err, MCPToolConfigurationError) for err in chain):
            return "configuration"

        if any(isinstance(err, asyncio.CancelledError) for err in chain):
            return "cancelled"

        if any(isinstance(err, (TimeoutError, asyncio.TimeoutError)) for err in chain):
            return "timeout"

        with contextlib.suppress(ModuleNotFoundError):
            import httpx

            if any(isinstance(err, httpx.TimeoutException) for err in chain):
                return "timeout"
            if any(isinstance(err, httpx.ConnectError) for err in chain):
                return "connect_failed"
            if any(isinstance(err, httpx.RequestError) for err in chain):
                return "request_failed"

        if any(isinstance(err, (socket.gaierror, ConnectionRefusedError, ConnectionError)) for err in chain):
            return "connect_failed"

        return "unknown"

    @classmethod
    def _exception_chain_summary(cls, exc: BaseException) -> str:
        parts: list[str] = []
        for err in cls._exception_chain(exc):
            text = str(err).strip()
            parts.append(f"{err.__class__.__name__}: {text}" if text else err.__class__.__name__)
        return " <- ".join(parts)

    @classmethod
    def _format_connection_error(
        cls,
        exc: BaseException,
        *,
        render_message: Callable[[MessageRef], str] = format_message,
    ) -> str:
        """Normalize transport/query errors into readable text for the test dialog."""
        kind = cls._classify_connection_error(exc)

        raw = str(exc).strip() or exc.__class__.__name__
        marker = " on TabPane(id='mcp')"
        if marker in raw:
            raw = raw.replace(marker, "")

        if kind == "configuration":
            friendly = render_message(_CONNECTION_CONFIGURATION_CONFLICT.bind())
        elif kind == "cancelled":
            friendly = render_message(_CONNECTION_CANCELLED.bind())
        elif kind == "timeout":
            friendly = render_message(_CONNECTION_TIMED_OUT.bind())
        elif kind == "connect_failed":
            friendly = render_message(_CONNECTION_UNREACHABLE.bind())
        elif kind == "request_failed":
            friendly = render_message(_CONNECTION_REQUEST_FAILED.bind())
        else:
            friendly = render_message(_CONNECTION_FAILED.bind())

        return f"{friendly}\n\n{raw}"

    def _shows(self, card: MCPConnectionCard) -> bool:
        """Whether *card* is one of the mounted cards; a rebuild replaces them all, and re-indexes the new ones."""
        return any(mounted is card for mounted in self.query(MCPConnectionCard))

    def _set_test_button_busy(self, card: MCPConnectionCard, busy: bool) -> None:
        with contextlib.suppress(Exception):
            button = card.query_one(f"#mcp-test-btn-{card.index}", Button)
            if self._read_only:
                button.disabled = True
                button.display = False
                return
            button.disabled = busy
            button.label = render_str(widget_localizer(self), (_TESTING if busy else _TEST).bind())

    def _snapshot_rows(self, *, preserve_blank_names: bool = False) -> list[_MCPServerRow]:
        """Snapshot each card's config and UI-only state, in card order, for a rebuild or ``get_config``."""
        cards = list(self.query(MCPConnectionCard))
        if len(cards) != len(self._rows):
            # Not mounted yet (or mid-rebuild): the rows are still the truth.
            return list(self._rows)
        return [
            _MCPServerRow(
                card._snapshot_config() if preserve_blank_names else card.get_config(),
                command_line_text=card._snapshot_command_line_text(),
                key_value_drafts=card._snapshot_key_value_drafts(),
                collapsed=card.collapsed,
            )
            for card in cards
        ]

    def get_config(self) -> list[MCPServerConfig]:
        """Collect config from all connection cards."""
        return [row.config for row in self._snapshot_rows()]

    def card_folds(self) -> list[bool]:
        """Whether each server's card is folded, in ``get_config`` order.

        The agent config screen rebuilds this panel from the draft when it loads
        the agent again, and passes these back so an unsaved server's card stays
        unfolded.
        """
        cards = list(self.query(MCPConnectionCard))
        if len(cards) != len(self._rows):
            return [row.collapsed for row in self._rows]
        return [card.collapsed for card in cards]

    def _validation_errors_by_card(self) -> list[tuple[MCPConnectionCard, list[str]]]:
        localizer = widget_localizer(self)
        results: list[tuple[MCPConnectionCard, list[str]]] = []
        seen_names: set[str] = set()
        for card in self.query(MCPConnectionCard):
            errors = list(card.validate())
            try:
                name = card.query_one(f"#mcp-name-{card.index}", Input).value.strip()
                if name:
                    lower_name = name.lower()
                    if lower_name in seen_names:
                        errors.append(
                            render_str(
                                localizer,
                                MCP_DUPLICATE_SERVER.bind(name=DisplayBlock(name)),
                            )
                        )
                    seen_names.add(lower_name)
            except Exception:
                pass
            results.append((card, errors))
        return results

    def validate(self) -> list[str]:
        """Validate all connection cards."""
        return [error for _card, errors in self._validation_errors_by_card() for error in errors]

    def unfold_invalid_cards(self, invalid_indexes: Collection[int] = ()) -> None:
        """Unfold the cards that fail validation or sit at *invalid_indexes*, and scroll to the first.

        A folded card hides the fields a Save error names, and the error names
        the first invalid card first.
        """
        invalid = [
            card for card, errors in self._validation_errors_by_card() if errors or card.index in invalid_indexes
        ]
        for card in invalid:
            card.set_collapsed(False, scroll_visible=False)
        if invalid:
            self.call_after_refresh(invalid[0].scroll_visible, animate=False)


# --- Test-dialog Markdown report -------------------------------------------
#
# Every server-controlled string below must pass through ``inline_text`` /
# ``code_span`` so a crafted server cannot forge block structure in the
# rendered report.  Env values and HTTP headers are NEVER rendered — they
# routinely carry secrets.

# Caps keep a pathological server (thousands of tools, book-length
# instructions) from turning the report into an unbounded document.
_MAX_LISTED_ENTRIES = 40
_INSTRUCTIONS_PREVIEW_CHARS = 600
_DESCRIPTION_PREVIEW_CHARS = 200
_IDENTITY_PREVIEW_CHARS = 120


def _preview(value: object, limit: int) -> str:
    flat = inline_text(value or "")
    if len(flat) > limit:
        return flat[:limit].rstrip() + "…"
    return flat


def _format_mcp_report(
    config: MCPServerConfig,
    report: MCPTestReport,
    *,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Render a successful connection test as the dialog's Markdown report."""
    sections = [
        _server_heading(report, render_message),
        _connection_caption(config, render_message),
        f"### {render_message(_CAPABILITIES_HEADING.bind())}",
        _mcp_capabilities_section(report.capabilities, render_message),
        f"### {render_message(_INSTRUCTIONS_HEADING.bind())}",
        _instructions_section(report.instructions, render_message),
        f"### {render_message(_TOOLS_HEADING.bind())}",
        _tools_section(report, render_message),
        f"### {render_message(_PROMPTS_HEADING.bind())}",
        _prompts_section(config, report, render_message),
    ]
    if report.initial_tool_names is not None:
        sections.extend(
            (
                f"### {render_message(_PROGRESSIVE_DISCLOSURE_HEADING.bind())}",
                _progressive_section(report.initial_tool_names, render_message),
            )
        )
    return "\n\n".join(sections)


def _server_heading(report: MCPTestReport, render_message: Callable[[MessageRef], str] = format_message) -> str:
    name = _preview(report.server_name, _IDENTITY_PREVIEW_CHARS)
    title = _preview(report.server_title, _IDENTITY_PREVIEW_CHARS) or name
    parts = []
    if title:
        parts.append(f"**{title}**")
    if name and name != title:
        parts.append(code_span(name))
    if version := _preview(report.server_version, _IDENTITY_PREVIEW_CHARS):
        parts.append(f"v{version}")
    if protocol := _preview(report.protocol_version, _IDENTITY_PREVIEW_CHARS):
        parts.append(f"MCP {code_span(protocol)}")
    if not parts:
        return render_message(_IDENTITY_NOT_REPORTED.bind())
    return " · ".join(parts)


def _connection_caption(config: MCPServerConfig, render_message: Callable[[MessageRef], str] = format_message) -> str:
    # Command line / URL only: env values and headers may carry secrets.
    if config.transport == "stdio":
        command_line = " ".join([config.command, *config.args]).strip()
        target = code_span(command_line) if command_line else render_message(_NO_COMMAND_CONFIGURED.bind())
    else:
        target = code_span(config.url) if config.url.strip() else render_message(_NO_URL_CONFIGURED.bind())
    return render_message(_CONNECTED_VIA.bind(transport=code_span(config.transport), target=target))


def _mcp_capabilities_section(
    advertised: tuple[str, ...],
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    advertised_set = set(advertised)
    if not advertised_set:
        return render_message(NOT_ADVERTISED.bind())
    # Standard groups always render (that is the point of the ✓/— column);
    # server-invented extras only fill the remaining row budget, and each
    # name is clamped — extras are unbounded server-controlled strings.
    extras = sorted((name for name in advertised_set if name not in STANDARD_CAPABILITY_GROUPS), key=str.casefold)
    shown = list(STANDARD_CAPABILITY_GROUPS) + extras[: _MAX_LISTED_ENTRIES - len(STANDARD_CAPABILITY_GROUPS)]
    table = md_table(
        (render_message(_CAPABILITY_HEADER.bind()), render_message(_ADVERTISED_HEADER.bind())),
        [
            (
                code_span(_preview(name, _IDENTITY_PREVIEW_CHARS)),
                SUPPORTED_MARK if name in advertised_set else UNSUPPORTED_MARK,
            )
            for name in shown
        ],
    )
    if hidden := len(extras) - (len(shown) - len(STANDARD_CAPABILITY_GROUPS)):
        table += f"\n\n*{render_message(_MORE_ENTRIES.bind(extra_count=hidden))}*"
    return table


def _instructions_section(
    instructions: str | None,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    flat = _preview(instructions, _INSTRUCTIONS_PREVIEW_CHARS)
    if not flat:
        return render_message(NOT_ADVERTISED.bind())
    # Blockquote + italic wrap: blockquote content is block-parsed, so bare
    # remote text at line start could still forge a heading/bullet — the
    # leading ``*`` keeps our own marker first on the line.
    return f"> *{flat}*"


def _catalog_bullets(
    entries: tuple[tuple[str, str], ...],
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    shown = entries[:_MAX_LISTED_ENTRIES]
    bullets = definition_bullets(
        [(name, "", _preview(description, _DESCRIPTION_PREVIEW_CHARS)) for name, description in shown]
    )
    if len(entries) > len(shown):
        bullets += f"\n- *{render_message(_MORE_ENTRIES.bind(extra_count=len(entries) - len(shown)))}*"
    return bullets


def _tools_section(report: MCPTestReport, render_message: Callable[[MessageRef], str] = format_message) -> str:
    if report.tools:
        return render_message(
            _TOOLS_EXPOSED.bind(
                count=len(report.tools),
                catalog=DisplayBlock(_catalog_bullets(report.tools, render_message)),
            )
        )
    if "tools" in report.capabilities:
        return render_message(_ADVERTISED_NO_TOOLS.bind())
    return render_message(NOT_ADVERTISED.bind())


def _prompts_section(
    config: MCPServerConfig,
    report: MCPTestReport,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    if report.prompts:
        return render_message(
            _PROMPTS_EXPOSED.bind(
                count=len(report.prompts),
                catalog=DisplayBlock(_catalog_bullets(report.prompts, render_message)),
            )
        )
    if not config.load_prompts:
        return render_message(_PROMPT_LOADING_DISABLED.bind())
    if "prompts" in report.capabilities:
        return render_message(_ADVERTISED_NO_PROMPTS.bind())
    return render_message(NOT_ADVERTISED.bind())


def _progressive_section(
    initial_tool_names: tuple[str, ...],
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    lines = [render_message(_PROGRESSIVE_ENABLED.bind()), ""]
    shown = initial_tool_names[:_MAX_LISTED_ENTRIES]
    lines.extend(f"- {code_span(name)}" for name in shown)
    if len(initial_tool_names) > len(shown):
        lines.append(f"- *{render_message(_MORE_ENTRIES.bind(extra_count=len(initial_tool_names) - len(shown)))}*")
    return "\n".join(lines)
