# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""External ACP agent configuration panel."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp import RequestError
from acp import schema as acp_schema
from rich.markup import escape
from textual import on, work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, Static

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.agents.panels.markdown_report import (
    NOT_ADVERTISED as _NOT_ADVERTISED,
)
from chrys.app.tui.screens.agents.panels.markdown_report import (
    SUPPORTED_MARK as _SUPPORTED_MARK,
)
from chrys.app.tui.screens.agents.panels.markdown_report import (
    UNSUPPORTED_MARK as _UNSUPPORTED_MARK,
)
from chrys.app.tui.screens.agents.panels.markdown_report import (
    code_span as _code_span,
)
from chrys.app.tui.screens.agents.panels.markdown_report import (
    definition_bullets as _definition_bullets,
)
from chrys.app.tui.screens.agents.panels.markdown_report import (
    inline_text as _inline_text,
)
from chrys.app.tui.screens.agents.panels.markdown_report import (
    md_table as _md_table,
)
from chrys.app.tui.screens.agents.validation_messages import (
    ACP_ALLOW_EXTERNAL_CWD as _ALLOW_EXTERNAL_CWD,
)
from chrys.app.tui.screens.agents.validation_messages import (
    ACP_CONFIG_OPTION_ROW_LABEL,
    ACP_CWD_OUTSIDE,
    ACP_ENVIRONMENT_ROW_LABEL,
    ACP_EXECUTABLE_REQUIRED,
    ACP_EXPANDED_LAUNCH_NUL,
    ACP_HANDSHAKE_TIMEOUT_LABEL,
    ACP_IDLE_TIMEOUT_LABEL,
    ACP_LAUNCH_NUL,
    ACP_TIMEOUT_NUMBER,
    ACP_TIMEOUT_RANGE,
    DUPLICATE_KEY_ROW,
    GREATER_THAN_ZERO,
    KEY_REQUIRED_ROW,
    ZERO_OR_GREATER,
)
from chrys.app.tui.screens.dialogs.connection_test import ConnectionTestDialog
from chrys.app.tui.widgets import Checkbox, ConfigAddButton, Select
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.foundation.i18n import DisplayBlock, MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.acp_client import AcpAgentClient, AcpAgentSpec, PermissionDecision
from chrys.service.profiles.agents.schema import AcpAgentConfig

if TYPE_CHECKING:
    from textual.app import ComposeResult

_TEST_TIMEOUT_SECONDS = 15.0

_ACP_AGENT_SUBJECT = msg("tui.acp.connection_test.subject", fallback="ACP Agent")
_STDERR_TAIL_LABEL = msg("tui.acp.connection_test.stderr_tail", fallback="stderr tail (UI only):")
_CAPABILITIES_HEADING = msg("tui.acp.connection_report.heading.capabilities", fallback="Capabilities")
_MODES_HEADING = msg("tui.acp.connection_report.heading.modes", fallback="Modes")
_MODELS_HEADING = msg("tui.acp.connection_report.heading.models", fallback="Models")
_CONFIG_OPTIONS_HEADING = msg("tui.acp.connection_report.heading.config_options", fallback="Config Options")
_AUTH_METHODS_HEADING = msg("tui.acp.connection_report.heading.auth_methods", fallback="Auth Methods")
_CAPABILITY_HEADER = msg("tui.acp.connection_report.header.capability", fallback="Capability")
_SUPPORTED_HEADER = msg("tui.acp.connection_report.header.supported", fallback="Supported")
_MODEL_HEADER = msg("tui.acp.connection_report.header.model", fallback="Model")
_ID_HEADER = msg("tui.acp.connection_report.header.id", fallback="ID")
_DESCRIPTION_HEADER = msg("tui.acp.connection_report.header.description", fallback="Description")
_CURRENT_CAPTION = msg("tui.acp.connection_report.current", fallback="Current: {label}")
_CURRENT_VALUE = msg("tui.acp.connection_report.current_value", fallback="current: {value}")
_ENV_LABEL = msg("tui.acp.connection_report.env", fallback="Env: {names}")
_IDENTITY_NOT_REPORTED = msg(
    "tui.acp.connection_report.identity_not_reported",
    fallback="*The agent did not report its identity.*",
)
_ACP_AGENT_FALLBACK_TITLE = msg("tui.acp.connection_report.fallback_title", fallback="ACP Agent")
_LAST_MESSAGE_SEGMENT = msg("tui.acp.result.last_segment", fallback="Last message segment")
_FULL_TRANSCRIPT = msg("tui.acp.result.full_transcript", fallback="Full transcript")
_RESULT_MODE_OPTIONS = [
    (_LAST_MESSAGE_SEGMENT, "last_segment"),
    (_FULL_TRANSCRIPT, "transcript"),
]
_STRING = msg("tui.acp.option_type.string", fallback="String")
_BOOLEAN = msg("tui.acp.option_type.boolean", fallback="Boolean")
_OPTION_TYPE_OPTIONS = [
    (_STRING, "string"),
    (_BOOLEAN, "boolean"),
]
_ARGUMENT_PLACEHOLDER = msg("tui.acp.placeholder.argument", fallback="Argument")
_VARIABLE_NAME_PLACEHOLDER = msg("tui.acp.placeholder.variable_name", fallback="Variable name")
_OPTION_ID_PLACEHOLDER = msg("tui.acp.placeholder.option_id", fallback="Option id")
_VALUE_PLACEHOLDER = msg("tui.acp.placeholder.value", fallback="Value")
_ENABLED = msg("tui.acp.enabled", fallback="Enabled")
_EXTERNAL_AGENT = msg("tui.acp.title", fallback="External ACP Agent")
_DESCRIPTION = msg(
    "tui.acp.description",
    fallback="Launch one stdio ACP process for each delegated invocation.",
)
_EXECUTABLE = msg("tui.acp.executable", fallback="Executable")
_EXECUTABLE_PLACEHOLDER = msg(
    "tui.acp.placeholder.executable",
    fallback="Executable path or command",
)
_ARGUMENTS = msg("tui.acp.arguments", fallback="Arguments")
_ARGUMENT = msg("tui.acp.argument", fallback="Argument")
_ENVIRONMENT_VARIABLES = msg("tui.acp.environment_variables", fallback="Environment Variables")
_VARIABLE = msg("tui.acp.variable", fallback="Variable")
_WORKING_DIRECTORY = msg("tui.acp.working_directory", fallback="Working Directory")
_WORKING_DIRECTORY_PLACEHOLDER = msg(
    "tui.acp.placeholder.working_directory",
    fallback="Blank uses workspace primary directory",
)
_ALLOW_EXTERNAL_CWD_DESCRIPTION = msg(
    "tui.acp.allow_external_cwd_description",
    fallback="Permit a working directory that resolves outside the workspace roots",
)
_SESSION_MODE = msg("tui.acp.session_mode", fallback="Session Mode")
_SESSION_MODE_PLACEHOLDER = msg("tui.acp.placeholder.session_mode", fallback="Optional mode id")
_UNSAFE_MODE_CAUTION = msg(
    "tui.acp.unsafe_mode_caution",
    fallback="Caution: this mode may bypass the remote agent's permission prompts.",
)
_MODEL_ID = msg("tui.acp.model_id", fallback="Model ID")
_MODEL_ID_PLACEHOLDER = msg("tui.acp.placeholder.model_id", fallback="Optional remote model id")
_CONFIG_OPTIONS = msg("tui.acp.config_options", fallback="Config Options")
_OPTION = msg("tui.acp.option", fallback="Option")
_BEST_EFFORT = msg("tui.acp.best_effort", fallback="Best effort mode and config options")
_BEST_EFFORT_DESCRIPTION = msg(
    "tui.acp.best_effort_description",
    fallback=(
        "Continue the launch when the agent rejects the session mode or config options above "
        "(the Model ID is always best-effort)"
    ),
)
_RESULT = msg("tui.acp.result", fallback="Result")
_HANDSHAKE_TIMEOUT = msg("tui.acp.handshake_timeout", fallback="Handshake Timeout (seconds)")
_IDLE_TIMEOUT = msg("tui.acp.idle_timeout", fallback="Idle Timeout (seconds; 0 disables)")
_TEST = msg("tui.acp.test", fallback="Test")
_TEST_NOTE = msg(
    "tui.acp.test_note",
    fallback=(
        "The test launches an ephemeral process, opens one session, reports advertised capabilities, then "
        "force-closes it. No prompt is sent."
    ),
)
_ADD_ITEM = msg("tui.acp.add_item", fallback="+ {label}")


class _AcpArgumentRow(Horizontal):
    def __init__(self, index: int, value: str, *, read_only: bool) -> None:
        self.index = index
        self.initial_value = value
        self.read_only = read_only
        super().__init__(classes="acp-list-row acp-argument-row")

    def compose(self) -> ComposeResult:
        value = Input(
            value=self.initial_value,
            placeholder=render_str(widget_localizer(self), _ARGUMENT_PLACEHOLDER.bind()),
            classes="acp-row-value",
        )
        value.disabled = self.read_only
        yield value
        remove = Button("✕", id=f"acp-row-delete-{self.index}", classes="acp-row-delete")
        remove.disabled = self.read_only
        remove.display = not self.read_only
        yield remove

    def value(self) -> str:
        return self.query_one(Input).value


class _AcpKeyValueRow(Horizontal):
    def __init__(
        self,
        index: int,
        key: str,
        value: str,
        *,
        kind: str,
        read_only: bool,
    ) -> None:
        self.index = index
        self.initial_key = key
        self.initial_value = value
        self.kind = kind
        self.read_only = read_only
        super().__init__(classes=f"acp-list-row acp-{kind}-row")

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        key = Input(
            value=self.initial_key,
            placeholder=render_str(
                localizer,
                (_VARIABLE_NAME_PLACEHOLDER if self.kind == "env" else _OPTION_ID_PLACEHOLDER).bind(),
            ),
            classes="acp-row-key",
        )
        key.disabled = self.read_only
        yield key
        value = Input(
            value=self.initial_value,
            placeholder=render_str(localizer, _VALUE_PLACEHOLDER.bind()),
            classes="acp-row-value",
        )
        value.disabled = self.read_only
        yield value
        remove = Button("✕", id=f"acp-row-delete-{self.index}", classes="acp-row-delete")
        remove.disabled = self.read_only
        remove.display = not self.read_only
        yield remove

    def pair(self) -> tuple[str, str]:
        inputs = list(self.query(Input))
        return inputs[0].value.strip(), inputs[1].value


class _AcpConfigOptionRow(Horizontal):
    def __init__(
        self,
        index: int,
        key: str,
        value: str | bool,
        *,
        read_only: bool,
    ) -> None:
        self.index = index
        self.initial_key = key
        self.initial_value = value
        self.read_only = read_only
        super().__init__(classes="acp-list-row acp-option-row")

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        key = Input(
            value=self.initial_key,
            placeholder=render_str(localizer, _OPTION_ID_PLACEHOLDER.bind()),
            classes="acp-row-key",
        )
        key.disabled = self.read_only
        yield key
        value_type = "boolean" if type(self.initial_value) is bool else "string"
        type_select = Select(
            [(render_str(localizer, label.bind()), value) for label, value in _OPTION_TYPE_OPTIONS],
            value=value_type,
            allow_blank=False,
            classes="acp-option-type",
        )
        type_select.disabled = self.read_only
        yield type_select
        string_value = Input(
            value=self.initial_value if isinstance(self.initial_value, str) else "",
            placeholder=render_str(localizer, _VALUE_PLACEHOLDER.bind()),
            classes="acp-option-string",
        )
        string_value.disabled = self.read_only
        string_value.display = value_type == "string"
        yield string_value
        bool_value = Checkbox(
            render_str(localizer, _ENABLED.bind()),
            value=bool(self.initial_value) if type(self.initial_value) is bool else False,
            classes="acp-option-bool",
        )
        bool_value.disabled = self.read_only
        bool_value.display = value_type == "boolean"
        yield bool_value
        remove = Button("✕", id=f"acp-row-delete-{self.index}", classes="acp-row-delete")
        remove.disabled = self.read_only
        remove.display = not self.read_only
        yield remove

    @on(Select.Changed, ".acp-option-type")
    def _on_type_changed(self, event: Select.Changed) -> None:
        is_bool = event.value == "boolean"
        self.query_one(".acp-option-string", Input).display = not is_bool
        self.query_one(".acp-option-bool", Checkbox).display = is_bool

    def pair(self) -> tuple[str, str | bool]:
        key = self.query_one(".acp-row-key", Input).value.strip()
        if self.query_one(".acp-option-type", Select).value == "boolean":
            return key, bool(self.query_one(".acp-option-bool", Checkbox).value)
        return key, self.query_one(".acp-option-string", Input).value


class _AcpTestCallbacks:
    async def on_update(self, seq: int, notification: acp_schema.SessionNotification) -> None:
        _ = seq, notification
        return

    async def on_permission_request(
        self,
        tool_call: acp_schema.ToolCallUpdate,
        options: Sequence[acp_schema.PermissionOption],
    ) -> PermissionDecision:
        _ = tool_call, options
        return PermissionDecision.cancelled()

    async def on_ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        raise RequestError.method_not_found(method)


class AcpConfigPanel(VerticalScroll):
    """Edit and test the single ACP launch configuration on an agent profile."""

    DEFAULT_CSS = """
    AcpConfigPanel {
        height: 1fr;
        /* Left inset only: Textual carves the vertical scrollbar out of the
           content box INSIDE the padding, so right padding would strand dead
           columns between the scrollbar and the pane edge. */
        padding: 0 0 0 2;
        scrollbar-size-vertical: 1;
        scrollbar-gutter: stable;
    }
    /* The right inset rides child margins instead of panel padding for the
       same reason: full-width children would otherwise touch the scrollbar.
       Full-width child rules that set a `margin:` shorthand must keep
       right = 1 themselves — the shorthand outranks this rule. */
    AcpConfigPanel > * {
        margin-right: 1;
    }
    AcpConfigPanel .acp-section-title {
        color: $secondary;
        text-style: bold;
        height: 1;
    }
    AcpConfigPanel .acp-section-desc,
    AcpConfigPanel .acp-note {
        color: $text-muted;
        height: auto;
        margin: 0 1 1 0;
    }
    AcpConfigPanel .acp-label {
        height: 1;
        color: $text-muted;
        margin: 1 0 0 0;
    }
    AcpConfigPanel Checkbox {
        height: 1;
        padding: 0;
        border: none;
        background: transparent;
        /* Align the [ ] glyph with the inputs' 1-cell text inset. */
        margin: 0 0 0 1;
    }
    /* Blank line between the Working Directory input and its checkbox —
       the Best-effort checkbox already gets one from the add-row above.
       Full margin: a single-side override here beats the Checkbox rule
       above for EVERY side, so the left inset must be restated. */
    AcpConfigPanel #acp-allow-external-cwd {
        margin: 1 0 0 1;
    }
    AcpConfigPanel Checkbox > .toggle--button {
        color: $foreground 35%;
        background: $foreground 6%;
    }
    AcpConfigPanel Checkbox.-on > .toggle--button {
        color: $success;
        background: $secondary 12%;
    }
    AcpConfigPanel .acp-option-desc {
        color: $text-muted;
        height: auto;
        width: 1fr;
        margin: 0 1 1 4;
        text-wrap: wrap;
        text-overflow: fold;
    }
    AcpConfigPanel Input {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
    }
    AcpConfigPanel Input:focus {
        border: none;
        background: $foreground 12%;
    }
    AcpConfigPanel Select {
        height: auto;
    }
    AcpConfigPanel SelectCurrent {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
    }
    AcpConfigPanel SelectOverlay {
        height: auto;
        max-height: 12;
        border: round $tui-border-primary $border-opacity;
        background: $surface;
    }
    AcpConfigPanel .acp-list {
        height: auto;
    }
    AcpConfigPanel .acp-list-row {
        height: auto;
        margin: 0 0 1 0;
    }
    AcpConfigPanel .acp-row-key {
        width: 2fr;
        margin-right: 1;
    }
    AcpConfigPanel .acp-row-value,
    AcpConfigPanel .acp-option-string {
        width: 3fr;
    }
    AcpConfigPanel .acp-option-type {
        width: 14;
        margin-right: 1;
    }
    AcpConfigPanel .acp-option-bool {
        width: 3fr;
    }
    AcpConfigPanel .acp-row-delete {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        background: transparent;
        color: $error;
        /* Must stay 0: config.tcss re-asserts this at #ac-tabs specificity —
           a squeezed content box makes Button's line-pad corrupt strip
           widths (see the note beside `#ac-tabs Button`). */
        padding: 0;
        content-align: center middle;
        text-align: center;
    }
    AcpConfigPanel .acp-add-row,
    AcpConfigPanel #acp-test {
        width: auto;
        min-width: 10;
        margin: 0 0 1 0;
    }
    AcpConfigPanel #acp-test {
        margin: 1 0 1 0;
    }
    AcpConfigPanel .acp-caution {
        color: $warning;
        background: $warning 12%;
        height: auto;
        margin: 1 1 1 0;
        padding: 0 1;
    }
    AcpConfigPanel .acp-timeout-row {
        height: auto;
    }
    AcpConfigPanel .acp-timeout-field {
        width: 1fr;
        height: auto;
    }
    /* Gap between the two timeout fields only — a trailing margin on the
       last field would pull it out of line with the inputs above. */
    AcpConfigPanel .acp-timeout-gap {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        config: AcpAgentConfig,
        *,
        workspace_cwd: str | None = None,
        workspace_roots: list[str] | None = None,
        read_only: bool = False,
    ) -> None:
        self._config = config
        self._workspace_cwd = workspace_cwd or ""
        self._workspace_roots = [root for root in workspace_roots or [] if root]
        if not self._workspace_roots and self._workspace_cwd:
            self._workspace_roots = [self._workspace_cwd]
        self._read_only = read_only
        self._next_index = 0
        self._testing = False
        super().__init__()

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _index(self) -> int:
        index = self._next_index
        self._next_index += 1
        return index

    def compose(self) -> ComposeResult:
        config = self._config
        localizer = widget_localizer(self)
        yield Label(render_str(localizer, _EXTERNAL_AGENT.bind()), classes="acp-section-title")
        yield Label(
            render_str(localizer, _DESCRIPTION.bind()),
            classes="acp-section-desc",
        )
        yield Label(f"[red]*[/red] {escape(render_str(localizer, _EXECUTABLE.bind()))}", classes="acp-label")
        command = Input(
            value=config.command,
            placeholder=render_str(localizer, _EXECUTABLE_PLACEHOLDER.bind()),
            id="acp-command",
        )
        command.disabled = self._read_only
        yield command

        yield Label(render_str(localizer, _ARGUMENTS.bind()), classes="acp-label")
        yield Vertical(
            *(_AcpArgumentRow(self._index(), value, read_only=self._read_only) for value in config.args),
            id="acp-args",
            classes="acp-list",
        )
        yield self._add_button(_ARGUMENT, "acp-add-arg")

        yield Label(render_str(localizer, _ENVIRONMENT_VARIABLES.bind()), classes="acp-label")
        yield Vertical(
            *(
                _AcpKeyValueRow(self._index(), key, value, kind="env", read_only=self._read_only)
                for key, value in config.env.items()
            ),
            id="acp-env",
            classes="acp-list",
        )
        yield self._add_button(_VARIABLE, "acp-add-env")

        yield Label(render_str(localizer, _WORKING_DIRECTORY.bind()), classes="acp-label")
        cwd = Input(
            value=config.cwd,
            placeholder=render_str(localizer, _WORKING_DIRECTORY_PLACEHOLDER.bind()),
            id="acp-cwd",
        )
        cwd.disabled = self._read_only
        yield cwd
        allow_external = Checkbox(
            render_str(localizer, _ALLOW_EXTERNAL_CWD.bind()),
            value=config.allow_external_cwd,
            id="acp-allow-external-cwd",
        )
        allow_external.disabled = self._read_only
        yield allow_external
        yield Label(
            render_str(localizer, _ALLOW_EXTERNAL_CWD_DESCRIPTION.bind()),
            classes="acp-option-desc",
        )

        yield Label(render_str(localizer, _SESSION_MODE.bind()), classes="acp-label")
        mode = Input(
            value=config.session_mode,
            placeholder=render_str(localizer, _SESSION_MODE_PLACEHOLDER.bind()),
            id="acp-session-mode",
        )
        mode.disabled = self._read_only
        yield mode
        caution = Static(
            render_str(localizer, _UNSAFE_MODE_CAUTION.bind()),
            id="acp-mode-caution",
            classes="acp-caution",
        )
        caution.display = self._unsafe_mode(config.session_mode)
        yield caution

        yield Label(render_str(localizer, _MODEL_ID.bind()), classes="acp-label")
        model = Input(
            value=config.model_id,
            placeholder=render_str(localizer, _MODEL_ID_PLACEHOLDER.bind()),
            id="acp-model-id",
        )
        model.disabled = self._read_only
        yield model

        yield Label(render_str(localizer, _CONFIG_OPTIONS.bind()), classes="acp-label")
        yield Vertical(
            *(
                _AcpConfigOptionRow(self._index(), key, value, read_only=self._read_only)
                for key, value in config.config_options.items()
            ),
            id="acp-options",
            classes="acp-list",
        )
        yield self._add_button(_OPTION, "acp-add-option")
        best_effort = Checkbox(
            render_str(localizer, _BEST_EFFORT.bind()),
            value=config.best_effort_options,
            id="acp-best-effort",
        )
        best_effort.disabled = self._read_only
        yield best_effort
        yield Label(
            render_str(localizer, _BEST_EFFORT_DESCRIPTION.bind()),
            classes="acp-option-desc",
        )

        yield Label(render_str(localizer, _RESULT.bind()), classes="acp-label")
        result_mode = Select(
            [(render_str(localizer, label.bind()), value) for label, value in _RESULT_MODE_OPTIONS],
            value=config.result_mode,
            allow_blank=False,
            id="acp-result-mode",
        )
        result_mode.disabled = self._read_only
        yield result_mode

        with Horizontal(classes="acp-timeout-row"):
            with Vertical(classes="acp-timeout-field acp-timeout-gap"):
                yield Label(render_str(localizer, _HANDSHAKE_TIMEOUT.bind()), classes="acp-label")
                handshake = Input(value=str(config.handshake_timeout_seconds), id="acp-handshake-timeout")
                handshake.disabled = self._read_only
                yield handshake
            with Vertical(classes="acp-timeout-field"):
                yield Label(render_str(localizer, _IDLE_TIMEOUT.bind()), classes="acp-label")
                idle = Input(value=str(config.idle_timeout_seconds), id="acp-idle-timeout")
                idle.disabled = self._read_only
                yield idle

        test = ConfigAddButton(render_str(localizer, _TEST.bind()), id="acp-test")
        test.disabled = self._read_only
        test.display = not self._read_only
        yield test
        yield Static(
            render_str(localizer, _TEST_NOTE.bind()),
            classes="acp-note",
        )

    def _add_button(self, label: MessageDef, button_id: str) -> Button:
        localizer = widget_localizer(self)
        button = ConfigAddButton(
            render_str(localizer, _ADD_ITEM.bind(label=render_str(localizer, label.bind()))),
            id=button_id,
            classes="acp-add-row",
        )
        button.disabled = self._read_only
        button.display = not self._read_only
        return button

    @staticmethod
    def _unsafe_mode(value: str) -> bool:
        # Known prompt-skipping mode families: Claude Code acceptEdits /
        # bypassPermissions, Codex full-access, Gemini CLI yolo. Separator
        # stripping catches full_access/fullAccess spellings too.
        folded = value.casefold()
        compact = folded.replace("-", "").replace("_", "").replace(" ", "")
        return "accept" in folded or "bypass" in folded or "fullaccess" in compact or "yolo" in folded

    @on(Input.Changed, "#acp-session-mode")
    def _on_mode_changed(self, event: Input.Changed) -> None:
        self.query_one("#acp-mode-caution", Static).display = self._unsafe_mode(event.value)

    @on(Button.Pressed, "#acp-add-arg")
    async def _on_add_arg(self, _event: Button.Pressed) -> None:
        await self.query_one("#acp-args", Vertical).mount(_AcpArgumentRow(self._index(), "", read_only=self._read_only))

    @on(Button.Pressed, "#acp-add-env")
    async def _on_add_env(self, _event: Button.Pressed) -> None:
        await self.query_one("#acp-env", Vertical).mount(
            _AcpKeyValueRow(self._index(), "", "", kind="env", read_only=self._read_only)
        )

    @on(Button.Pressed, "#acp-add-option")
    async def _on_add_option(self, _event: Button.Pressed) -> None:
        await self.query_one("#acp-options", Vertical).mount(
            _AcpConfigOptionRow(self._index(), "", "", read_only=self._read_only)
        )

    @on(Button.Pressed, ".acp-row-delete")
    async def _on_delete_row(self, event: Button.Pressed) -> None:
        parent = event.button.parent
        if isinstance(parent, _AcpArgumentRow | _AcpKeyValueRow | _AcpConfigOptionRow):
            await parent.remove()

    def get_config(self) -> AcpAgentConfig:
        """Harvest separate executable, argument, environment, and option fields."""
        arguments = [row.value() for row in self.query(_AcpArgumentRow)]
        env = {
            key: value for row in self.query(_AcpKeyValueRow) if row.kind == "env" for key, value in [row.pair()] if key
        }
        options = {key: value for row in self.query(_AcpConfigOptionRow) for key, value in [row.pair()] if key}
        result_value = self.query_one("#acp-result-mode", Select).value
        return AcpAgentConfig(
            command=self.query_one("#acp-command", Input).value.strip(),
            args=arguments,
            env=env,
            cwd=self.query_one("#acp-cwd", Input).value.strip(),
            allow_external_cwd=bool(self.query_one("#acp-allow-external-cwd", Checkbox).value),
            session_mode=self.query_one("#acp-session-mode", Input).value.strip(),
            model_id=self.query_one("#acp-model-id", Input).value.strip(),
            config_options=options,
            best_effort_options=bool(self.query_one("#acp-best-effort", Checkbox).value),
            result_mode="transcript" if result_value == "transcript" else "last_segment",
            handshake_timeout_seconds=self._float_value(
                "#acp-handshake-timeout",
                self._config.handshake_timeout_seconds,
            ),
            idle_timeout_seconds=self._float_value(
                "#acp-idle-timeout",
                self._config.idle_timeout_seconds,
            ),
        )

    def _float_value(self, selector: str, fallback: float) -> float:
        with contextlib.suppress(ValueError):
            return float(self.query_one(selector, Input).value.strip())
        return fallback

    def validate(self) -> list[str]:
        localizer = widget_localizer(self)
        errors: list[str] = []
        if not self.query_one("#acp-command", Input).value.strip():
            errors.append(render_str(localizer, ACP_EXECUTABLE_REQUIRED.bind()))
        errors.extend(self._validate_key_value_rows())
        errors.extend(
            self._validate_timeout(
                "#acp-handshake-timeout",
                ACP_HANDSHAKE_TIMEOUT_LABEL,
                allow_zero=False,
            )
        )
        errors.extend(self._validate_timeout("#acp-idle-timeout", ACP_IDLE_TIMEOUT_LABEL, allow_zero=True))
        config = self.get_config()
        if any("\0" in value for value in (config.command, config.cwd, *config.args, *config.env.values())):
            errors.append(render_str(localizer, ACP_LAUNCH_NUL.bind()))
        expanded, template_errors = self._expanded_launch_values(config)
        errors.extend(template_errors)
        if expanded is not None:
            if any(
                "\0" in value
                for value in (expanded["command"], expanded["cwd"], *expanded["args"], *expanded["env"].values())
            ):
                errors.append(render_str(localizer, ACP_EXPANDED_LAUNCH_NUL.bind()))
            errors.extend(self._validate_cwd(config, expanded["cwd"]))
        return errors

    def _expanded_launch_values(self, config: AcpAgentConfig) -> tuple[dict[str, Any] | None, list[str]]:
        """Expand ``{{ENV}}`` templates exactly like real invocation does.

        Validation and the Test spawn must both operate on the EXPANDED
        values — validating the literal template while launching the expanded
        one lets a template that expands outside the workspace bypass the
        containment check.
        """
        try:
            expanded: dict[str, Any] = {
                "command": resolve_env_templates(config.command, location="ACP command", allow_commands=False),
                "args": tuple(resolve_env_templates(value, location="ACP argument") for value in config.args),
                "env": {
                    key: resolve_env_templates(value, location=f"ACP environment {key!r}")
                    for key, value in config.env.items()
                },
                "cwd": (
                    resolve_env_templates(config.cwd, location="ACP cwd", allow_commands=False)
                    if config.cwd
                    else self._workspace_cwd
                ),
            }
        except ValueError as exc:
            return None, [str(exc)]
        return expanded, []

    def _validate_key_value_rows(self) -> list[str]:
        localizer = widget_localizer(self)
        errors: list[str] = []
        for case_insensitive, label_definition, rows in (
            (True, ACP_ENVIRONMENT_ROW_LABEL, list(self.query(_AcpKeyValueRow))),
            (False, ACP_CONFIG_OPTION_ROW_LABEL, list(self.query(_AcpConfigOptionRow))),
        ):
            label = render_str(localizer, label_definition.bind())
            seen: set[str] = set()
            for index, row in enumerate(rows, start=1):
                key, value = row.pair()
                has_value = bool(value) if isinstance(value, str) else True
                if not key and has_value:
                    errors.append(render_str(localizer, KEY_REQUIRED_ROW.bind(label=label, row=index)))
                compare = key.casefold() if case_insensitive else key
                if key and compare in seen:
                    errors.append(
                        render_str(
                            localizer,
                            DUPLICATE_KEY_ROW.bind(label=label, row=index, key=DisplayBlock(key)),
                        )
                    )
                if key:
                    seen.add(compare)
        return errors

    def _validate_timeout(self, selector: str, label_definition: MessageDef, *, allow_zero: bool) -> list[str]:
        localizer = widget_localizer(self)
        label = render_str(localizer, label_definition.bind())
        raw = self.query_one(selector, Input).value.strip()
        try:
            value = float(raw)
        except ValueError:
            return [render_str(localizer, ACP_TIMEOUT_NUMBER.bind(label=label))]
        if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
            qualifier_definition = ZERO_OR_GREATER if allow_zero else GREATER_THAN_ZERO
            qualifier = render_str(localizer, qualifier_definition.bind())
            return [render_str(localizer, ACP_TIMEOUT_RANGE.bind(label=label, qualifier=qualifier))]
        return []

    def _validate_cwd(self, config: AcpAgentConfig, expanded_cwd: str) -> list[str]:
        if not config.cwd or config.allow_external_cwd or not self._workspace_roots:
            return []
        candidate = Path(expanded_cwd)
        if not candidate.is_absolute():
            candidate = Path(self._workspace_cwd) / candidate
        resolved = os.path.realpath(candidate)
        for root in self._workspace_roots:
            try:
                if os.path.commonpath(
                    [os.path.normcase(resolved), os.path.normcase(os.path.realpath(root))]
                ) == os.path.normcase(os.path.realpath(root)):
                    return []
            except ValueError:
                continue
        return [render_str(widget_localizer(self), ACP_CWD_OUTSIDE.bind())]

    @on(Button.Pressed, "#acp-test")
    def _on_test(self, _event: Button.Pressed) -> None:
        if self._testing or self._read_only:
            return
        self._testing = True
        self.query_one("#acp-test", Button).disabled = True
        self._run_test()

    @work(thread=False)
    async def _run_test(self) -> None:
        dialog = ConnectionTestDialog(
            server_name=self.query_one("#acp-command", Input).value.strip(),
            subject_label=_ACP_AGENT_SUBJECT.bind(),
        )
        self.app.push_screen(dialog)
        client: AcpAgentClient | None = None
        try:
            errors = self.validate()
            if errors:
                dialog.set_result(False, "\n".join(errors))
                return
            config = self.get_config()
            # ignore_cleanup_errors: on Windows the killed agent process can
            # release its stderr.log handle a beat after force_close returns;
            # a leaked test tempdir beats crashing the worker.
            with tempfile.TemporaryDirectory(prefix="chrys-acp-test-", ignore_cleanup_errors=True) as temp_dir:
                # realpath: macOS $TMPDIR lives under the /var -> private/var
                # symlink, and the secure stderr-log open refuses symlinked
                # parent components.
                temp_root = Path(os.path.realpath(temp_dir))
                # Spec construction stays inside the try: a template that no
                # longer resolves must land in the dialog, not leave it
                # spinning on an unhandled worker exception.
                try:
                    spec = self._test_spec(config, temp_root / "stderr.log")
                    client = AcpAgentClient(spec, _AcpTestCallbacks())
                    async with asyncio.timeout(_TEST_TIMEOUT_SECONDS):
                        await client.connect()
                        handshake = await client.open_session()
                    dialog.set_result(
                        True,
                        _format_handshake(handshake, render_message=self._render_message),
                        markdown=True,
                    )
                except Exception as exc:
                    message = _sanitized_test_error(exc)
                    if client is not None and (tail := client.stderr_tail.strip()):
                        message += f"\n\n{self._render_message(_STDERR_TAIL_LABEL.bind())}\n{tail[-4096:]}"
                    dialog.set_result(False, message, allow_esc=isinstance(exc, TimeoutError))
                finally:
                    if client is not None:
                        await client.force_close()
                    client = None
        finally:
            if client is not None:
                await client.force_close()
            self._testing = False
            with contextlib.suppress(Exception):
                self.query_one("#acp-test", Button).disabled = self._read_only

    def _test_spec(self, config: AcpAgentConfig, stderr_path: Path) -> AcpAgentSpec:
        expanded, errors = self._expanded_launch_values(config)
        if expanded is None:
            raise ValueError(errors[0])
        cwd_path = Path(expanded["cwd"])
        if not cwd_path.is_absolute():
            cwd_path = Path(self._workspace_cwd) / cwd_path
        cwd = os.path.realpath(cwd_path)
        roots = tuple(dict.fromkeys(os.path.realpath(root) for root in self._workspace_roots))
        return AcpAgentSpec(
            command=expanded["command"],
            args=expanded["args"],
            env=expanded["env"],
            cwd=cwd,
            stderr_log_path=stderr_path,
            additional_directories=tuple(root for root in roots if root != cwd),
            session_mode=config.session_mode,
            model_id=config.model_id,
            config_options=config.config_options,
            best_effort_options=config.best_effort_options,
            handshake_timeout_seconds=config.handshake_timeout_seconds,
            idle_timeout_seconds=config.idle_timeout_seconds,
        )


def _format_handshake(
    handshake: Any,
    *,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Render the handshake as the Markdown report shown by the test dialog."""
    return _handshake_markdown(
        agent=_handshake_payload(handshake.agent_info),
        capabilities=_handshake_payload(handshake.capabilities),
        modes=_handshake_payload(handshake.modes),
        models=_handshake_payload(handshake.models),
        config_options=_handshake_payload(handshake.config_options),
        auth_methods=_handshake_payload(handshake.auth_methods),
        render_message=render_message,
    )


def _handshake_payload(value: Any) -> Any:
    """Dump an ACP schema object (or tuple of them) to plain wire-shaped data."""
    if value is None:
        return None
    if isinstance(value, list | tuple):
        return [_handshake_payload(item) for item in value]
    # agent-client-protocol 0.10.1 declares ``auth`` as
    # ``AgentAuthCapabilities | None`` but gives it a bare-dict default.  An
    # omitted field therefore reaches Pydantic's serializer as the wrong
    # runtime type and emits a warning.  An explicitly supplied ``auth={}``
    # is validated into AgentAuthCapabilities, so normalizing only the raw
    # dict preserves that meaningful distinction at the SDK boundary.
    if (
        isinstance(value, acp_schema.AgentCapabilities)
        and "auth" not in value.model_fields_set
        and isinstance(value.auth, dict)
    ):
        value = value.model_copy(update={"auth": None})
    return value.model_dump(by_alias=True, exclude_none=True)


def _handshake_markdown(
    *,
    agent: Any,
    capabilities: Any,
    modes: Any,
    models: Any,
    config_options: Any,
    auth_methods: Any,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    sections = [
        _agent_heading(agent, render_message),
        f"### {render_message(_CAPABILITIES_HEADING.bind())}",
        _capabilities_section(capabilities, render_message),
        f"### {render_message(_MODES_HEADING.bind())}",
        _modes_section(modes, render_message),
        f"### {render_message(_MODELS_HEADING.bind())}",
        _models_section(models, render_message),
        f"### {render_message(_CONFIG_OPTIONS_HEADING.bind())}",
        *(_config_options_blocks(config_options, render_message) or [render_message(_NOT_ADVERTISED.bind())]),
        f"### {render_message(_AUTH_METHODS_HEADING.bind())}",
        _auth_methods_section(auth_methods, render_message),
    ]
    return "\n\n".join(sections)


def _agent_heading(agent: Any, render_message: Callable[[MessageRef], str] = format_message) -> str:
    if not isinstance(agent, dict):
        return render_message(_IDENTITY_NOT_REPORTED.bind())
    name = _inline_text(agent.get("name") or "")
    title = _inline_text(agent.get("title") or "") or name or render_message(_ACP_AGENT_FALLBACK_TITLE.bind())
    parts = [f"**{title}**"]
    if name and name != title:
        parts.append(_code_span(name))
    if version := _inline_text(agent.get("version") or ""):
        parts.append(f"v{version}")
    return " · ".join(parts)


def _capability_rows(payload: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten the capability tree into (dotted key, mark) rows.

    Booleans map to supported/unsupported marks; nested empty objects are
    presence markers (e.g. ``sessionCapabilities.fork``); other scalars are
    shown verbatim. Unknown agents stay renderable because nothing here is
    schema-specific beyond stripping the ``Capabilities`` key suffix.
    """
    if not isinstance(payload, dict):
        return []
    rows: list[tuple[str, str]] = []
    for key, value in payload.items():
        if key.startswith("_"):
            continue
        label = key.removesuffix("Capabilities") or key
        path = f"{prefix}.{label}" if prefix else label
        if isinstance(value, bool):
            rows.append((path, _SUPPORTED_MARK if value else _UNSUPPORTED_MARK))
        elif isinstance(value, dict):
            if value:
                rows.extend(_capability_rows(value, path))
            elif prefix:
                rows.append((path, _SUPPORTED_MARK))
        elif value is not None:
            rows.append((path, _code_span(value)))
    return rows


def _capabilities_section(
    capabilities: Any,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    rows = sorted(_capability_rows(capabilities), key=lambda row: row[0].casefold())
    if not rows:
        return render_message(_NOT_ADVERTISED.bind())
    return _md_table(
        (render_message(_CAPABILITY_HEADER.bind()), render_message(_SUPPORTED_HEADER.bind())),
        [(_code_span(path), mark) for path, mark in rows],
    )


def _current_caption(
    items: Sequence[dict],
    current: Any,
    id_key: str,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """One-line "Current: …" caption naming the active mode/model."""
    if current is None:
        return ""
    name = next((str(item.get("name") or "") for item in items if item.get(id_key) == current), "")
    label = f"**{_inline_text(name)}** ({_code_span(current)})" if name else _code_span(current)
    return render_message(_CURRENT_CAPTION.bind(label=label))


def _modes_section(modes: Any, render_message: Callable[[MessageRef], str] = format_message) -> str:
    if not isinstance(modes, dict):
        return render_message(_NOT_ADVERTISED.bind())
    items = [item for item in modes.get("availableModes") or [] if isinstance(item, dict)]
    if not items:
        return render_message(_NOT_ADVERTISED.bind())
    entries = [
        (
            str(item.get("name") or item.get("id") or ""),
            _code_span(item.get("id") or ""),
            str(item.get("description") or ""),
        )
        for item in items
    ]
    blocks = []
    if caption := _current_caption(items, modes.get("currentModeId"), "id", render_message):
        blocks.append(caption)
    blocks.append(_definition_bullets(entries))
    return "\n\n".join(blocks)


def _models_section(models: Any, render_message: Callable[[MessageRef], str] = format_message) -> str:
    if not isinstance(models, dict):
        return render_message(_NOT_ADVERTISED.bind())
    items = [item for item in models.get("availableModels") or [] if isinstance(item, dict)]
    rows = [
        (
            str(item.get("name") or item.get("modelId") or ""),
            _code_span(item.get("modelId") or ""),
            str(item.get("description") or ""),
        )
        for item in items
    ]
    if not rows:
        return render_message(_NOT_ADVERTISED.bind())
    blocks = []
    if caption := _current_caption(items, models.get("currentModelId"), "modelId", render_message):
        blocks.append(caption)
    blocks.append(
        _md_table(
            (
                render_message(_MODEL_HEADER.bind()),
                render_message(_ID_HEADER.bind()),
                render_message(_DESCRIPTION_HEADER.bind()),
            ),
            rows,
        )
    )
    return "\n\n".join(blocks)


def _config_options_blocks(
    options: Any,
    render_message: Callable[[MessageRef], str] = format_message,
) -> list[str]:
    blocks: list[str] = []
    for option in options or []:
        if not isinstance(option, dict):
            continue
        name = str(option.get("name") or option.get("id") or "")
        header = [f"**{_inline_text(name)}**", _code_span(option.get("id") or "")]
        if kind := _inline_text(option.get("type") or ""):
            header.append(kind)
        current = option.get("currentValue")
        if current is not None:
            header.append(render_message(_CURRENT_VALUE.bind(value=_code_span(_scalar_code(current)))))
        blocks.append(" · ".join(header))
        if description := _inline_text(option.get("description") or ""):
            blocks.append(f"*{description}*")
        if choices := _select_choice_entries(option.get("options") or [], current):
            blocks.append(_definition_bullets(choices))
    return blocks


def _select_choice_entries(items: Any, current: Any, group: str = "") -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "options" in item:
            # SessionConfigSelectGroup: flatten with a "group / choice" label.
            entries.extend(_select_choice_entries(item.get("options"), current, group=str(item.get("name") or "")))
            continue
        value = item.get("value")
        name = str(item.get("name") or value or "")
        if group:
            name = f"{group} / {name}"
        entries.append((name, _code_span(_scalar_code(value)), str(item.get("description") or "")))
    return entries


def _auth_methods_section(
    auth_methods: Any,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    entries: list[tuple[str, str, str]] = []
    for method in auth_methods or []:
        if not isinstance(method, dict):
            continue
        detail = _inline_text(method.get("description") or "")
        env_names = [
            str(var.get("name")) for var in method.get("vars") or [] if isinstance(var, dict) and var.get("name")
        ]
        if env_names:
            joined = ", ".join(_code_span(name) for name in env_names)
            detail = f"{detail} {render_message(_ENV_LABEL.bind(names=joined))}".strip()
        entries.append(
            (
                str(method.get("name") or method.get("id") or ""),
                _code_span(method.get("id") or ""),
                detail,
            )
        )
    if not entries:
        return render_message(_NOT_ADVERTISED.bind())
    return _definition_bullets(entries)


def _scalar_code(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _sanitized_test_error(exc: BaseException) -> str:
    detail = getattr(exc, "detail", "")
    if not isinstance(detail, str) or not detail:
        detail = str(exc)
    line = detail.replace("\r", " ").replace("\n", " ").strip()
    return (line or type(exc).__name__)[:500]
