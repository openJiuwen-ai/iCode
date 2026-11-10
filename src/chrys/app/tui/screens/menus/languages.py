# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LanguagesScreen — modal for selecting the requested display locale."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.containers import VerticalGroup
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_content, render_text, widget_localizer
from chrys.app.tui.language import LANGUAGE_OPTIONS, LANGUAGE_PICKER_TITLE
from chrys.app.tui.screens.dialogs.base import BaseDialog

if TYPE_CHECKING:
    from textual.app import ComposeResult


_DOUBLE_CLICK_THRESHOLD = 0.4


class LanguagesScreen(BaseDialog[str | None]):
    """Select a requested locale without previewing highlights."""

    DEFAULT_CSS = """
    LanguagesScreen {
        align: center middle;
    }
    LanguagesScreen > #container {
        width: 48;
        max-width: 90%;
        max-height: 90%;
        height: auto;
        background: $surface;
        border: round $tui-border-primary $border-opacity;
        border-title-align: left;
        border-title-color: $tui-border-title-primary;
        padding: 0;
        overflow-x: hidden;
    }
    LanguagesScreen > #container > OptionList {
        height: auto;
        max-height: 100%;
        border: none;
        padding: 0 0 0 1;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CLOSE_BINDING),
    ]

    def __init__(self, current_locale: str) -> None:
        self._current_locale = current_locale
        self._last_selected: str | None = None
        self._last_selected_time: float = 0
        self._dismissed = False
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="container"):
            yield OptionList()

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        localizer = widget_localizer(self)
        container = self.query_one("#container", VerticalGroup)
        container.border_title = render_content(localizer, LANGUAGE_PICKER_TITLE.bind())
        options = self.query_one(OptionList)
        for index, (requested_locale, definition) in enumerate(LANGUAGE_OPTIONS):
            options.add_option(Option(render_text(localizer, definition.bind()), id=requested_locale))
            if requested_locale == self._current_locale:
                options.highlighted = index

    @on(OptionList.OptionSelected)
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        """Double-click confirms; a first click only selects the row."""
        requested_locale = event.option.id
        if not requested_locale:
            return
        now = monotonic()
        if self._last_selected == requested_locale and (now - self._last_selected_time) < _DOUBLE_CLICK_THRESHOLD:
            event.stop()
            self._safe_dismiss(requested_locale)
        else:
            self._last_selected = requested_locale
            self._last_selected_time = now

    def on_key(self, event) -> None:
        """Enter confirms the highlighted locale exactly once."""
        if event.key != "enter":
            return
        event.stop()
        event.prevent_default()
        option = self.query_one(OptionList).highlighted_option
        if option is not None and option.id:
            self._safe_dismiss(option.id)

    def action_cancel(self) -> None:
        self._safe_dismiss(None)

    def _dismiss_clicked_outside(self) -> None:
        self.action_cancel()

    def _safe_dismiss(self, result: str | None) -> None:
        if self._dismissed:
            return
        self._dismissed = True
        self.dismiss(result)
