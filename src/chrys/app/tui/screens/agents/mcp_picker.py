# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Select an existing MCP configuration or create a new server card."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

from textual import on
from textual.containers import VerticalGroup
from textual.content import Content
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_locale_controller, widget_localizer
from chrys.app.tui.screens.agents.mcp_choices import MCPChoices, MCPConflict, MCPConflictKind, mcp_choice_conflict
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.picker_text import wrap_description
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.profiles.agents.schema import MCPServerConfig

_TITLE = msg("tui.mcp_picker.title", fallback="Add MCP Server")
_CREATE = msg("tui.mcp_picker.create", fallback="+ New MCP configuration…")
_SOURCES = msg("tui.mcp_picker.sources", fallback="Sources: {names}")
_ADDED = msg("tui.mcp_picker.added", fallback="Already added")
_NAME_CONFLICT = msg("tui.mcp_picker.name_conflict", fallback="Same name already exists with different settings")
_CONNECTION_CONFLICT = msg("tui.mcp_picker.connection_conflict", fallback="Connection already configured: {name}")
_DISABLED = msg("tui.mcp_picker.disabled", fallback="Disabled")
_HIDDEN = msg(
    "tui.mcp_picker.hidden",
    fallback="{count} invalid configuration hidden",
    plural_fallback="{count} invalid configurations hidden",
)


def conflict_message(conflict: MCPConflict) -> MessageRef:
    """Use the same reason in the picker and a rejected late selection."""
    if conflict.kind is MCPConflictKind.ADDED:
        return _ADDED.bind()
    if conflict.kind is MCPConflictKind.NAME:
        return _NAME_CONFLICT.bind()
    return _CONNECTION_CONFLICT.bind(name=sanitize_legacy_scalar(conflict.existing_name))


def mcp_target_summary(config: MCPServerConfig) -> str:
    """Show a program hint or URL host, never arguments, credentials or queries."""
    if config.transport == "stdio":
        # Handwritten YAML may put arguments in command; truncate before looking for path separators.
        command = config.command.split(maxsplit=1)
        return sanitize_legacy_scalar(command[0].replace("\\", "/").rsplit("/", 1)[-1]) if command else ""
    try:
        return sanitize_legacy_scalar(urlsplit(config.url).hostname or "")
    except ValueError:
        return ""


class MCPPickerAction(StrEnum):
    CREATE = "create"


type MCPPickerResult = MCPServerConfig | MCPPickerAction | None


class MCPPickerDialog(BaseDialog[MCPPickerResult]):
    """Single click or Enter copies a choice; Escape and backdrop clicks cancel."""

    DEFAULT_CSS = """
    MCPPickerDialog { align: center middle; }
    MCPPickerDialog > #container {
        width: 68;
        max-width: 90%;
        max-height: 90%;
        height: auto;
        background: $surface;
        border: round $tui-border-primary $border-opacity;
        border-title-align: left;
        border-title-color: $primary;
        padding: 0;
        overflow-x: hidden;
    }
    MCPPickerDialog > #container > OptionList {
        height: auto;
        max-height: 100%;
        border: none;
        padding: 0 0 0 1;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS: ClassVar[list] = [localized_binding("escape", "dismiss", CLOSE_BINDING)]

    def __init__(self, choices: MCPChoices, current: Sequence[MCPServerConfig]) -> None:
        self._choices = choices
        self._current = current
        self._results: dict[str, MCPPickerResult] = {}
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="container") as container:
            container.border_title = render_str(widget_localizer(self), _TITLE.bind())
            yield OptionList()

    def on_mount(self) -> None:
        controller = widget_locale_controller(self)
        if controller is not None:
            controller.register_surface(self)
        self.refresh_localization()

    def on_unmount(self) -> None:
        controller = widget_locale_controller(self)
        if controller is not None:
            controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        options.clear_options()
        self._results.clear()
        localizer = widget_localizer(self)
        self.query_one("#container").border_title = render_str(localizer, _TITLE.bind())
        width = options.size.width or 62
        choices = [
            (index, choice, mcp_choice_conflict(choice.config, self._current))
            for index, choice in enumerate(self._choices.choices)
        ]
        # Stable grouping preserves reading order within the selectable and disabled groups.
        for index, choice, conflict in sorted(choices, key=lambda entry: entry[2] is not None):
            config = choice.config
            details = [" · ".join(part for part in (config.transport.upper(), mcp_target_summary(config)) if part)]
            sources = ", ".join(sanitize_legacy_scalar(source) for source in choice.sources)
            details.append(render_str(localizer, _SOURCES.bind(names=sources)))
            if config.description:
                details.append(sanitize_legacy_scalar(config.description))
            if not config.enabled:
                details.append(render_str(localizer, _DISABLED.bind()))
            if conflict is not None:
                details.append(render_str(localizer, conflict_message(conflict)))
            content = Content.assemble(
                (sanitize_legacy_scalar(config.name), "bold"),
                "\n" + "\n".join(wrap_description(detail, width) for detail in details),
            )
            option_id = f"server:{index}"
            options.add_option(Option(content, id=option_id, disabled=conflict is not None))
            if conflict is None:
                self._results[option_id] = config
        if self._choices.choices:
            options.add_option(None)
        create = render_str(localizer, _CREATE.bind())
        hidden = (
            "\n" + wrap_description(render_str(localizer, _HIDDEN.bind(count=self._choices.hidden_count)), width)
            if self._choices.hidden_count
            else ""
        )
        options.add_option(Option(Content.assemble((create, "bold"), hidden), id="action:create"))
        self._results["action:create"] = MCPPickerAction.CREATE
        if highlighted is not None:
            options.highlighted = highlighted
        else:
            options.highlighted = next(index for index, option in enumerate(options.options) if not option.disabled)

    @on(OptionList.OptionSelected)
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if self.dismiss_requested or self.app.screen is not self:
            return
        result = self._results.get(event.option.id or "")
        if result is not None:
            self.dismiss(result)
