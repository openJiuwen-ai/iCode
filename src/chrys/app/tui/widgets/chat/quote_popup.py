# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The floating Quote and Copy actions shown when a transcript drag ends.

The main screen pins the one-row overlay to ``target_region`` on arrange.
It takes no transcript space, and its hidden children never intercept clicks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from textual.content import Content
from textual.dom import NoScreen
from textual.events import MouseDown
from textual.geometry import Offset, Region
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.copy_messages import COPY_TITLE
from chrys.app.tui.i18n import render_content
from chrys.app.tui.util.visibility import set_widget_visibility_without_layout
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import MessageDef

_QUOTE_ACTION = msg("tui.quote.action", fallback="Quote")

QUOTE_INPUT_UNAVAILABLE = msg(
    "tui.quote.input_unavailable",
    fallback="The message input cannot take a quote right now.",
)


type SelectionAction = Literal["quote", "copy"]


class _SelectionButton(ClickAffordance):
    ALLOW_SELECT = False

    def __init__(self, action: SelectionAction, label: Content) -> None:
        super().__init__(label, classes=f"selection-action -{action}")
        self._action = action

    def on_mouse_down(self, event: MouseDown) -> None:
        """Act on press, before mouse-up clears the selection and hides the overlay."""
        if event.button != 1:
            return
        event.stop()
        self.post_message(QuoteSelectionPopup.Requested(self._action))


class _SelectionDivider(Static):
    ALLOW_SELECT = False


class QuoteSelectionPopup(Widget):
    """Offer to quote or copy the finished drag selection."""

    class Requested(Message):
        """Posted when an action is pressed."""

        def __init__(self, action: SelectionAction = "quote") -> None:
            super().__init__()
            self.action = action

    # Chrome floating over the transcript: never joins selections, and it
    # must not resolve as a selection anchor either.
    ALLOW_SELECT = False

    # The preferred width matches DEFAULT_CSS. The minimum fits both labels and the divider.
    WIDTH_CELLS = 18
    MIN_WIDTH_CELLS = 10

    DEFAULT_CSS = """
    QuoteSelectionPopup {
        layout: horizontal;
        overflow: hidden hidden;
        overlay: screen;
        visibility: hidden;
        margin: 0;
        width: 18;
        height: 1;
    }
    QuoteSelectionPopup > .selection-action,
    QuoteSelectionPopup > .selection-divider {
        height: 1;
        background: $foreground 8%;
        color: $text-muted;
        text-style: dim;
        content-align: center middle;
    }
    QuoteSelectionPopup > .selection-divider {
        width: 1;
    }
    QuoteSelectionPopup > .selection-action {
        pointer: pointer;
    }
    QuoteSelectionPopup > .selection-action.-quote {
        width: 9fr;
        min-width: 5;
    }
    QuoteSelectionPopup > .selection-action.-copy {
        width: 8fr;
        min-width: 4;
    }
    QuoteSelectionPopup > .selection-action:hover {
        background: $foreground 15%;
        color: $accent;
        text-style: not dim;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        self._quote_button = _SelectionButton("quote", self._label_content(_QUOTE_ACTION, locale_controller))
        self._copy_button = _SelectionButton("copy", self._label_content(COPY_TITLE, locale_controller))
        self._divider = _SelectionDivider("│", classes="selection-divider")
        super().__init__(self._quote_button, self._divider, self._copy_button)
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

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self._quote_button.update(self._label_content(_QUOTE_ACTION, self._locale_controller), layout=False)
        self._copy_button.update(self._label_content(COPY_TITLE, self._locale_controller), layout=False)

    @property
    def is_shown(self) -> bool:
        """Whether the overlay is currently presented."""
        return self._shown

    @property
    def target_region(self) -> Region | None:
        """The screen region the overlay is pinned to while shown."""
        return self._target_region

    def is_divider_at(self, offset: Offset) -> bool:
        """Return whether the visible divider covers this screen cell."""
        return self._shown and offset in self._divider.region

    def place_near(self, pointer: Offset, *, within: Region | None = None) -> None:
        """Present the actions beside the drag end, inside the visible part of *within*.

        Prefer one row below the pointer. At the bottom edge, use the row
        above when available.
        """
        try:
            area = Region(0, 0, *self.screen.size)
        except NoScreen:
            return
        if within is not None:
            area = area.intersection(within)
        width = min(self.WIDTH_CELLS, area.width)
        if width < self.MIN_WIDTH_CELLS or area.height == 0:
            self.hide()
            return
        x = min(max(pointer.x, area.x), area.right - width)
        y = pointer.y + 1
        if y >= area.bottom:
            y = pointer.y - 1
        y = min(max(y, area.y), area.bottom - 1)
        self._target_region = Region(x, y, width, 1)
        set_widget_visibility_without_layout(self, True)
        self._shown = True

    def hide(self) -> None:
        """Withdraw the overlay; clicks pass through to the transcript again."""
        self._target_region = None
        if not self._shown:
            return
        set_widget_visibility_without_layout(self, False)
        self._shown = False
