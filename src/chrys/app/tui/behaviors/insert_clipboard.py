# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ctrl+Insert / Shift+Insert clipboard fallbacks shared by the app and modal screens.

Chrys editors bind the Insert clipboard keys themselves so image paste and
Vim mode gating run first. These fallbacks serve everything else: text
selected on a screen and stock Textual controls. They are non-priority
bindings, so they only run after the focused widget declined the key.

Textual stops the non-priority binding chain at a ``ModalScreen``, which
hides the application-level copy of these bindings from every modal dialog.
Modal roots therefore spread :data:`INSERT_CLIPBOARD_BINDINGS` into their own
``BINDINGS`` and mix in :class:`InsertClipboardScreenMixin` for the actions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from textual import events
from textual.binding import Binding
from textual.widgets import Input, TextArea

from chrys.app.tui.behaviors.right_click_copy import copy_screen_selection
from chrys.app.tui.binding_display import COPY_BINDING, PASTE_BINDING, localized_binding
from chrys.app.tui.clipboard import copy_text_to_clipboards, paste_text_from_clipboards

if TYPE_CHECKING:
    from textual.app import App
    from textual.screen import Screen

COPY_WITH_INSERT_ACTION: Final = "copy_with_insert"
PASTE_WITH_INSERT_ACTION: Final = "paste_with_insert"

INSERT_CLIPBOARD_BINDINGS: Final[tuple[Binding, ...]] = (
    localized_binding("ctrl+insert", COPY_WITH_INSERT_ACTION, COPY_BINDING, show=False),
    localized_binding("shift+insert", PASTE_WITH_INSERT_ACTION, PASTE_BINDING, show=False),
)
"""Non-priority bindings every binding namespace repeats to reach the fallbacks."""


def copy_with_insert(app: App[Any]) -> None:
    """Copy the focused editor selection, then fall back to the screen selection."""
    focused = app.focused
    selected = focused.selected_text if isinstance(focused, Input | TextArea) else ""
    if selected:
        copy_text_to_clipboards(app, selected)
    else:
        copy_screen_selection(app.screen)


def paste_with_insert(app: App[Any]) -> None:
    """Route clipboard text through the same Paste event the terminal driver posts."""
    if text := paste_text_from_clipboards(app):
        app.post_message(events.Paste(text))


_InsertClipboardScreenBase = Screen[Any] if TYPE_CHECKING else object


class InsertClipboardScreenMixin(_InsertClipboardScreenBase):
    """Screen actions backing :data:`INSERT_CLIPBOARD_BINDINGS`.

    Textual only merges ``BINDINGS`` declared on ``DOMNode`` subclasses, so a
    mixin cannot contribute the bindings themselves. Each modal root spreads
    :data:`INSERT_CLIPBOARD_BINDINGS` into its own ``BINDINGS`` alongside
    this mixin; the modal-screen structure test enforces the pairing.
    """

    def action_copy_with_insert(self) -> None:
        copy_with_insert(self.app)

    def action_paste_with_insert(self) -> None:
        paste_with_insert(self.app)
