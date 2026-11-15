# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Readonly builtin prompt viewers with an explicit customization route."""

from __future__ import annotations

from textual import events
from textual.message import Message

from chrys.app.tui.widgets import EnhancedTextArea
from chrys.foundation.i18n import msg

LOCKED_TITLE = msg("tui.agent_config.prompts.locked_title", fallback="Built-in prompts are read-only")
LOCKED_MESSAGE = msg(
    "tui.agent_config.prompts.locked_message",
    fallback="System and compaction prompts are managed and updated by {app}. Clone this agent to customize them.",
)
CLONE_AND_EDIT = msg("tui.agent_config.prompts.clone_and_edit", fallback="Clone and edit")


class PromptTextArea(EnhancedTextArea):
    """Reject edits from the first event while allowing selection, copy and scrolling."""

    class CustomizeRequested(Message):
        pass

    def __init__(self, text: str, *, locked: bool = False, id: str | None = None) -> None:
        self._prompt_locked = locked
        self._customization_pending = False
        super().__init__(text, read_only=locked, id=id)

    def _request_customization(self) -> None:
        if self._prompt_locked and not self._customization_pending:
            self._customization_pending = True
            self.post_message(self.CustomizeRequested())

    def reset_customization_request(self) -> None:
        """Allow another edit attempt after the owning screen closes its dialog."""
        self._customization_pending = False

    def on_click(self, event: events.Click) -> None:
        # Textual also emits Click when a drag ends inside this widget.
        if event.button == 1 and self.selection.is_empty:
            self._request_customization()

    def on_key(self, event: events.Key) -> None:
        if event.is_printable or event.key in {"backspace", "delete", "enter", "ctrl+v", "super+v"}:
            self._request_customization()

    def on_paste(self, _event: events.Paste) -> None:
        self._request_customization()
