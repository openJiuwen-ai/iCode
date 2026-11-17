# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EnhancedInput — terminal-friendly single-line input with conventional clipboard keys."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from textual.actions import SkipAction
from textual.widgets import Input

from chrys.app.tui.binding_display import COPY_BINDING, PASTE_BINDING, localized_binding
from chrys.app.tui.clipboard import copy_text_to_clipboards, paste_text_from_clipboards, uses_native_windows_clipboard

if TYPE_CHECKING:
    from textual import events


class EnhancedInput(Input):
    """``Input`` variant with conventional select-all and clipboard behavior.

    Textual's built-in ``Input`` maps ``Ctrl+A`` to ``home`` and reserves
    select-all for ``Ctrl+Shift+A``.  Chrys uses ``Home`` for cursor-start,
    so user-editable single-line fields follow the common terminal/editor
    convention and select the field contents with ``Ctrl+A``. Native paste
    actions prefer the current OS clipboard over Textual's process-local cache;
    browser-host sessions stay scoped to their own browser clipboard.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("ctrl+insert", "copy", COPY_BINDING, show=False),
        localized_binding("shift+insert", "paste", PASTE_BINDING, show=False),
    ]

    async def _on_key(self, event: events.Key) -> None:
        if event.key != "ctrl+a":
            return
        event.stop()
        event.prevent_default()
        self.select_all()

    def action_copy(self) -> None:
        """Copy the selection to Textual and host OS clipboards."""
        selected = self.selected_text
        if not selected:
            raise SkipAction
        copy_text_to_clipboards(self.app, selected)

    def action_cut(self) -> None:
        """Cut only after the copy request has been accepted."""
        selected = self.selected_text
        if not selected:
            return
        if not copy_text_to_clipboards(self.app, selected) and uses_native_windows_clipboard():
            return
        self.delete_selection()

    def action_paste(self) -> None:
        """Paste the first line from the freshest frontend-safe clipboard."""
        text = paste_text_from_clipboards(self.app)
        if not text:
            return
        line = text.splitlines()[0]
        if not line:
            return
        start, end = self.selection
        self.replace(line, start, end)
