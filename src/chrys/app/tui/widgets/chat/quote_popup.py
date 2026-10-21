# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The floating ``Quote`` pill shown when a transcript drag ends.

The pill is an ``overlay: screen`` child of the main screen, so it never
takes transcript space, and its exact screen placement is pinned by the
screen's ``arrange`` from ``target_region`` — the same mechanism that keeps
``ScrollToBottomButton`` fixed to the viewport. Hidden widgets are skipped
by painting and hit testing, so an invisible pill never blocks clicks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.content import Content
from textual.dom import NoScreen
from textual.events import MouseDown
from textual.geometry import Offset, Region
from textual.message import Message

from chrys.app.tui.i18n import render_content
from chrys.app.tui.util.visibility import set_widget_visibility_without_layout
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import MessageDef

_QUOTE_ACTION = msg("tui.quote.action", fallback="Quote")

_QUOTE_GLYPH = "❝"


class QuoteSelectionPopup(ClickAffordance):
    """Single-action pill offering to quote the finished drag selection."""

    class Requested(Message):
        """Posted when the quote affordance is clicked."""

    # Chrome floating over the transcript: never joins selections, and it
    # must not resolve as a selection anchor either.
    ALLOW_SELECT = False

    CLICK_MESSAGE = Requested

    # Fixed pill width; must match the ``width`` rule in DEFAULT_CSS below.
    WIDTH_CELLS = 12

    DEFAULT_CSS = """
    QuoteSelectionPopup {
        overlay: screen;
        visibility: hidden;
        margin: 0;
        width: 12;
        height: 1;
        padding: 0 1;
        background: $foreground 8%;
        color: $text-muted;
        text-style: dim;
        content-align: center middle;
        pointer: pointer;
    }
    QuoteSelectionPopup:hover {
        background: $foreground 15%;
        color: $accent;
        text-style: not dim;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__(self._render_label(locale_controller))
        self._locale_controller = locale_controller
        self._shown = False
        # True between a MouseDown on the pill and its Click: the screen's
        # mouse-up selection clearing must not withdraw the pill in that
        # window, or the Click would hit the widget beneath instead.
        self._pressed = False
        # The exact screen region the owning screen pins into its arrangement.
        self._target_region: Region | None = None

    @staticmethod
    def _label_content(definition: MessageDef, locale_controller: LocaleController | None) -> Content:
        bound = definition.bind()
        if locale_controller is None:
            return Content.from_text(f"{_QUOTE_GLYPH} {format_message(bound)}", markup=False)
        return Content.assemble(_QUOTE_GLYPH, " ", render_content(locale_controller.localizer, bound))

    @classmethod
    def _render_label(cls, locale_controller: LocaleController | None) -> Content:
        return cls._label_content(_QUOTE_ACTION, locale_controller)

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self.update(self._render_label(self._locale_controller), layout=False)

    @property
    def is_shown(self) -> bool:
        """Whether the pill is currently presented."""
        return self._shown

    @property
    def pressed(self) -> bool:
        """Whether a press began on the pill and its click is still pending."""
        return self._pressed

    @property
    def target_region(self) -> Region | None:
        """The screen region the pill is pinned to while shown."""
        return self._target_region

    def on_mouse_down(self, event: MouseDown) -> None:
        """Claim the press so the click lands on the pill, not the transcript."""
        event.stop()
        self._pressed = True

    def place_near(self, pointer: Offset) -> None:
        """Present the pill beside the drag end, clamped onto the screen.

        Prefers one row below the pointer; near the bottom edge it flips to
        the row above so it never covers the selection's last line.
        """
        try:
            screen_size = self.screen.size
        except NoScreen:
            return
        width = self.WIDTH_CELLS
        x = min(max(pointer.x, 0), max(0, screen_size.width - width))
        y = pointer.y + 1
        if y >= screen_size.height:
            y = max(0, pointer.y - 1)
        self._target_region = Region(x, y, width, 1)
        set_widget_visibility_without_layout(self, True)
        self._shown = True

    def hide(self) -> None:
        """Withdraw the pill; clicks pass through to the transcript again."""
        self._pressed = False
        self._target_region = None
        if not self._shown:
            return
        set_widget_visibility_without_layout(self, False)
        self._shown = False
