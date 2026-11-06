# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The floating ``Quote`` pill shown when a transcript drag ends.

Pressing it puts a ``[Quote #n]`` label for the selection into the chat
draft at the cursor. The pill is an ``overlay: screen`` child of the main
screen, so it never takes transcript space, and its exact screen placement
is pinned by the screen's ``arrange`` from ``target_region`` — the same
mechanism that keeps ``ScrollToBottomButton`` fixed to the viewport. Hidden
widgets are skipped by painting and hit testing, so an invisible pill never
blocks clicks.
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


class QuoteSelectionPopup(ClickAffordance):
    """Single-action pill offering to quote the finished drag selection."""

    class Requested(Message):
        """Posted when the pill is pressed."""

    # Chrome floating over the transcript: never joins selections, and it
    # must not resolve as a selection anchor either.
    ALLOW_SELECT = False

    # Fixed pill width; must match the ``width`` rule in DEFAULT_CSS below.
    WIDTH_CELLS = 9

    DEFAULT_CSS = """
    QuoteSelectionPopup {
        overlay: screen;
        visibility: hidden;
        margin: 0;
        width: 9;
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
        # The exact screen region the owning screen pins into its arrangement.
        self._target_region: Region | None = None

    @staticmethod
    def _label_content(definition: MessageDef, locale_controller: LocaleController | None) -> Content:
        bound = definition.bind()
        if locale_controller is None:
            return Content.from_text(format_message(bound), markup=False)
        return render_content(locale_controller.localizer, bound)

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
    def target_region(self) -> Region | None:
        """The screen region the pill is pinned to while shown."""
        return self._target_region

    def on_mouse_down(self, event: MouseDown) -> None:
        """Act on the press: an in-place mouse-up clears the selection, and with it this pill, before any click."""
        if event.button != 1:
            # Only the left button quotes; another press (a middle-click paste) goes on as usual.
            return
        event.stop()
        self.post_message(self.Requested())

    def place_near(self, pointer: Offset, *, within: Region | None = None) -> None:
        """Present the pill beside the drag end, kept inside *within* (the screen when none).

        Prefers one row below the pointer; at the area's bottom edge it flips
        to the row above so it never covers the selection's last line.
        """
        try:
            area = within if within is not None else Region(0, 0, *self.screen.size)
        except NoScreen:
            return
        width = self.WIDTH_CELLS
        x = min(max(pointer.x, area.x), max(area.x, area.right - width))
        y = pointer.y + 1
        if y >= area.bottom:
            y = pointer.y - 1
        y = min(max(y, area.y), max(area.y, area.bottom - 1))
        self._target_region = Region(x, y, width, 1)
        set_widget_visibility_without_layout(self, True)
        self._shown = True

    def hide(self) -> None:
        """Withdraw the pill; clicks pass through to the transcript again."""
        self._target_region = None
        if not self._shown:
            return
        set_widget_visibility_without_layout(self, False)
        self._shown = False
