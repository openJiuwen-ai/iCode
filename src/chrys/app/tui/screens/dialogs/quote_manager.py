# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Modal listing the conversation quotes waiting to ride the next message."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Static

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.removal import remove_children_shielded
from chrys.app.tui.widgets.chat.quote_format import (
    Quote,
    quote_preview,
    source_label_reference,
)
from chrys.app.tui.widgets.dialog_buttons import DialogButtonRow, DialogButtonSpec
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import MessageRef

_QUOTE_DIALOG_TITLE = msg("tui.quote.dialog_title", fallback="Conversation quotes")
_QUOTE_REMOVE = msg("tui.quote.remove", fallback="Remove")
_QUOTE_CLEAR_ALL = msg("tui.quote.clear_all", fallback="Clear all")
_QUOTE_CLOSE = msg("tui.quote.close", fallback="Close")

_REMOVE_BUTTON_PREFIX = "quote-remove-"


def _quote_source_label(quote: Quote, locale_controller: LocaleController | None) -> str:
    reference = source_label_reference(quote.source)
    if locale_controller is None:
        return format_message(reference)
    return render_str(locale_controller.localizer, reference)


class _QuoteRow(HorizontalGroup):
    """One pending quote: source label, preview, and its remove button."""

    DEFAULT_CSS = """
    _QuoteRow {
        height: auto;
        margin: 0 1 0 0;
    }
    _QuoteRow > .quote-row-text {
        width: 1fr;
        height: auto;
        padding: 0 0 0 1;
    }
    _QuoteRow > .quote-row-source {
        color: $accent;
        text-style: bold;
    }
    _QuoteRow > .quote-row-preview {
        color: $text-muted;
    }
    _QuoteRow > Button {
        width: auto;
        min-width: 9;
        height: 1;
        margin: 0 1;
    }
    """

    def __init__(self, quote: Quote, index: int, *, locale_controller: LocaleController | None) -> None:
        super().__init__(id=f"quote-row-{index}")
        self._quote = quote
        self._index = index
        self._locale_controller = locale_controller

    def compose(self) -> ComposeResult:
        yield Static(
            Text(_quote_source_label(self._quote, self._locale_controller), style="bold"),
            classes="quote-row-text quote-row-source",
            markup=False,
        )
        yield Static(
            Text(quote_preview(self._quote.text)),
            classes="quote-row-text quote-row-preview",
            markup=False,
        )
        # The button id ends in the bare index so the dialog's handler parses
        # it with a plain int() — never nest the row id (with its own prefix)
        # into the button id.
        yield Button(Text(self._remove_label()), id=f"{_REMOVE_BUTTON_PREFIX}{self._index}", compact=True)

    def _remove_label(self) -> str:
        reference = _QUOTE_REMOVE.bind()
        if self._locale_controller is None:
            return format_message(reference)
        return render_str(self._locale_controller.localizer, reference)

    def on_click(self, event: events.Click) -> None:
        """Rows are chrome, not selectable text."""
        event.stop()


class QuoteManagerDialog(BaseDialog[None]):
    """List, remove, and clear the quotes attached to the next message."""

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CANCEL_BINDING, show=False, priority=True),
        Binding("c", "clear_all", show=False),
    ]

    CSS_PATH = "quote_manager.tcss"

    def __init__(
        self,
        quotes: tuple[Quote, ...],
        *,
        on_remove: Callable[[int], None],
        on_clear_all: Callable[[], None],
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._locale_controller = locale_controller
        self._on_remove = on_remove
        self._on_clear_all = on_clear_all
        self._quotes: list[Quote] = list(quotes)
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="quote-manager-container") as container:
            container.border_title = Text(self._render_label(_QUOTE_DIALOG_TITLE.bind()))
            with VerticalScroll(id="quote-manager-list"):
                yield from self._compose_rows()
            yield DialogButtonRow(
                DialogButtonSpec(
                    Text(self._render_label(_QUOTE_CLEAR_ALL.bind())), id="quote-clear", variant="warning"
                ),
                DialogButtonSpec(Text(self._render_label(_QUOTE_CLOSE.bind())), id="quote-close", variant="primary"),
                id="quote-manager-buttons",
            )

    def _compose_rows(self) -> ComposeResult:
        for index, quote in enumerate(self._quotes):
            yield _QuoteRow(quote, index, locale_controller=self._locale_controller)

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self.call_after_refresh(self._focus_close_button)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self.query_one("#quote-manager-container", VerticalGroup).border_title = Text(
            self._render_label(_QUOTE_DIALOG_TITLE.bind())
        )
        self.query_one("#quote-clear", Button).label = Text(self._render_label(_QUOTE_CLEAR_ALL.bind()))
        self.query_one("#quote-close", Button).label = Text(self._render_label(_QUOTE_CLOSE.bind()))
        self.call_after_refresh(self._rebuild_rows)

    def _render_label(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def _focus_close_button(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#quote-close", Button).focus()

    async def _rebuild_rows(self) -> None:
        """Re-compose the rows against the current quote list."""
        if not self.is_mounted or self._closing or self._pruning:
            return
        listing = self.query_one("#quote-manager-list", VerticalScroll)
        await remove_children_shielded(listing)
        if self._quotes:
            await listing.mount(*self._compose_rows())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch row-remove, clear, and close by button id."""
        button_id = event.button.id or ""
        if button_id == "quote-clear":
            event.stop()
            if self._quotes:
                self._quotes.clear()
                self._on_clear_all()
                self.call_after_refresh(self._rebuild_rows)
        elif button_id == "quote-close":
            event.stop()
            self.dismiss(None)
        elif button_id.startswith(_REMOVE_BUTTON_PREFIX):
            event.stop()
            self._remove_row(button_id.removeprefix(_REMOVE_BUTTON_PREFIX))

    def _remove_row(self, row_id: str) -> None:
        try:
            index = int(row_id)
        except ValueError:
            return
        if not 0 <= index < len(self._quotes):
            return
        del self._quotes[index]
        self._on_remove(index)
        self.call_after_refresh(self._rebuild_rows)

    def action_clear_all(self) -> None:
        if self._quotes:
            self._quotes.clear()
            self._on_clear_all()
            self.call_after_refresh(self._rebuild_rows)

    def action_close(self) -> None:
        self.dismiss(None)
