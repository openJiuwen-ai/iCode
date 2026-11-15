# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Instructions configuration panel — composable widget for the Instructions tab."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Label

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.agents.prompt_editor import PromptTextArea
from chrys.app.tui.screens.agents.validation_messages import FIELDS_REQUIRED, INSTRUCTIONS_FIELD
from chrys.app.tui.widgets import EnhancedTextArea
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from textual.app import ComposeResult

_SYSTEM_INSTRUCTIONS = msg("tui.agent_config.instructions.title", fallback="System Instructions")
_SYSTEM_INSTRUCTIONS_DESCRIPTION = msg(
    "tui.agent_config.instructions.description",
    fallback="Define the agent's system prompt and behavior guidelines",
)


class InstructionsConfigPanel(VerticalScroll):
    """Composable widget for editing agent profile instructions."""

    DEFAULT_CSS = """
    InstructionsConfigPanel {
        height: 1fr;
        padding: 0 1 0 2;
        scrollbar-size-vertical: 1;
    }
    InstructionsConfigPanel .ic-section-title {
        color: $secondary;
        text-style: bold;
        height: 1;
    }
    InstructionsConfigPanel .ic-section-desc {
        color: $text-muted;
        height: 1;
        margin: 0 0 1 0;
    }
    InstructionsConfigPanel EnhancedTextArea {
        height: 1fr;
        border: none;
        background: $foreground 8%;
        padding: 0 0;
        margin: 0;
        scrollbar-size-vertical: 1;
    }
    InstructionsConfigPanel EnhancedTextArea:focus {
        border: none;
        background: $foreground 12%;
    }
    InstructionsConfigPanel EnhancedTextArea .text-area--cursor-line {
        background: $primary 15%;
    }
    """

    def __init__(self, instructions: str, *, prompt_locked: bool = False) -> None:
        self._instructions = instructions
        self._prompt_locked = prompt_locked
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        yield Label(render_str(localizer, _SYSTEM_INSTRUCTIONS.bind()), classes="ic-section-title")
        yield Label(render_str(localizer, _SYSTEM_INSTRUCTIONS_DESCRIPTION.bind()), classes="ic-section-desc")
        yield PromptTextArea(self._instructions, locked=self._prompt_locked, id="ic-instructions")

    def get_config(self) -> str:
        """Return the current instructions text."""
        with contextlib.suppress(NoMatches):
            return self.query_one("#ic-instructions", EnhancedTextArea).text
        return self._instructions

    def validate(self) -> list[str]:
        """Validate that instructions are not empty."""
        if not self.query_one("#ic-instructions", EnhancedTextArea).text.strip():
            localizer = widget_localizer(self)
            return [
                render_str(
                    localizer,
                    FIELDS_REQUIRED.bind(field=render_str(localizer, INSTRUCTIONS_FIELD.bind())),
                )
            ]
        return []
