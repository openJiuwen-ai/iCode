# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentsScreen — modal for browsing and switching agent profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.containers import VerticalGroup
from textual.content import Content
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.picker_text import wrap_description
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.profiles.agents.registry import AgentProfileRegistry

_AGENTS = msg("tui.agent_picker.title", fallback="Agents")


class AgentsScreen(BaseDialog[str | None]):
    """Modal for switching agent profiles.

    Single click selects and dismisses. Current profile is dimmed and disabled.
    Escape cancels.
    """

    DEFAULT_CSS = """
    AgentsScreen {
        align: center middle;
    }
    AgentsScreen > #container {
        width: 60;
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
    AgentsScreen > #container > OptionList {
        height: auto;
        max-height: 100%;
        border: none;
        padding: 0 0 0 1;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "dismiss", CLOSE_BINDING),
    ]

    def __init__(self, registry: AgentProfileRegistry, current_profile: str) -> None:
        self._registry = registry
        self._current_profile = current_profile
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="container") as container:
            container.border_title = render_str(widget_localizer(self), _AGENTS.bind())
            yield OptionList()

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        ol = self.query_one(OptionList)
        # Estimate usable width: container width - border(2) - padding(1) - scrollbar(1)
        content_width = ol.size.width or 54
        profiles = self._registry.list_profiles()
        for i, p in enumerate(profiles):
            if i > 0:
                ol.add_option(None)
            label = p.display_name or p.name
            is_current = label == self._current_profile
            desc = wrap_description(p.description, content_width) if p.description else ""
            prefix = "◦ " if is_current else ""
            if is_current:
                content = Content.assemble(
                    (f"{prefix}{label}", "bold #888888"),
                    (f"\n{desc}", "#888888") if desc else "",
                )
            else:
                content = Content.assemble(
                    (f"{prefix}{label}", "bold"),
                    f"\n{desc}" if desc else "",
                )
            ol.add_option(Option(content, id=p.name, disabled=is_current))

    @on(OptionList.OptionSelected)
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        """Single click selects and dismisses."""
        if event.option.id:
            self.dismiss(event.option.id)
