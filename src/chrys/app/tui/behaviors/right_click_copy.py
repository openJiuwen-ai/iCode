# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared right-click copy helpers for selectable TUI surfaces."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, cast

from textual.errors import NoWidget
from textual.events import Event, MouseDown, MouseMove
from textual.screen import Screen
from textual.widgets import TextArea

from chrys.app.tui.clipboard import copy_text_to_clipboards

_RightClickScreenCopyBase = Screen[Any] if TYPE_CHECKING else object


def copy_screen_selection(screen: Screen, *, clear: bool = False) -> bool:
    """Copy the screen-level selected text and optionally clear it."""
    defer = getattr(screen, "defer_selected_text", None)
    if defer is not None and defer(
        partial(copy_screen_selection, screen, clear=clear), key=(copy_screen_selection, clear)
    ):
        return True
    selected = screen.get_selected_text()
    if not selected:
        return False
    copy_text_to_clipboards(screen.app, selected)
    if clear:
        screen.clear_selection()
    return True


def cancel_textual_mouse_chain(screen: Screen) -> None:
    """Cancel Textual's private mouse bookkeeping after right-click copy."""
    # Textual internals: these private fields drive mouse-up selection
    # clearing and synthetic Click generation. Keep this aligned with
    # Textual's App.on_event / Screen._forward_event during upgrades.
    screen.app._mouse_down_widget = None
    screen._mouse_down_offset = None
    screen._selecting = False


def discard_detached_selection_hit(screen: Screen, event: Event) -> bool:
    """Consume mouse events that would reach a widget removed since the last layout.

    Until the next layout the screen still hits such a widget. Textual focuses what a press hits,
    and requires the widget it starts or extends a selection on to have a parent.
    """
    if event.is_forwarded or not isinstance(event, (MouseDown, MouseMove)):
        return False
    if isinstance(event, MouseDown):
        # With or without a selection on the screen, and also where no offset in the widget comes back.
        try:
            widget, _region = screen.get_widget_at(event.x, event.y)
        except NoWidget:
            return False
    elif screen._selecting:
        # Only a drag extending a selection needs the widget under a move to have a parent.
        widget, _offset = screen.get_widget_and_offset_at(event.x, event.y)
        if widget is None:
            return False
    else:
        return False
    if widget.is_attached:
        return False

    screen.clear_selection()
    cancel_textual_mouse_chain(screen)
    event.prevent_default()
    event.stop()
    return True


def _right_clicked_editor(screen: Screen, event: MouseDown) -> TextArea | None:
    """Resolve the editor under a right-click, including its scrollbars."""
    try:
        widget, _region = screen.get_widget_at(event.screen_x, event.screen_y)
    except NoWidget:
        return None
    return next((ancestor for ancestor in widget.ancestors_with_self if isinstance(ancestor, TextArea)), None)


class RightClickScreenCopyMixin(_RightClickScreenCopyBase):
    """Mixin for screens whose right-click should copy screen-selected text.

    Windows Terminal's native right-click copy is unavailable while Textual
    owns mouse reporting, so Chrys mirrors that workflow for selectable UI.
    Put this mixin before ``Screen`` / ``ModalScreen`` in the class bases so
    ``super()._forward_event`` resolves to Textual's screen implementation.
    """

    def on_mouse_down(self, event: MouseDown) -> None:
        """Copy selected screen text on right-click."""
        if event.button != 3 or _right_clicked_editor(cast(Screen, self), event) is not None:
            return
        self._handle_right_click_copy(event)

    def _handle_right_click_copy(self, event: MouseDown) -> bool:
        if event.button != 3:
            return False
        screen = cast(Screen, self)
        if not copy_screen_selection(screen, clear=True):
            return False
        cancel_textual_mouse_chain(screen)
        event.prevent_default()
        event.stop()
        return True

    def _forward_event(self, event: Event) -> None:
        """Intercept right-click copy before Textual mutates selection state."""
        screen = cast(Screen, self)
        # Ahead of the detached-widget check, so a right-click over a widget just removed still copies.
        if isinstance(event, MouseDown) and not event.is_forwarded and event.button == 3:
            editor = _right_clicked_editor(screen, event)
            if editor is None:
                if self._handle_right_click_copy(event):
                    return
            elif editor.is_attached and screen.app.mouse_captured is None:
                # Textual starts a new screen selection before delivering the
                # press. Give the editor the existing selection first.
                handle_copy = getattr(editor, "handle_right_click_copy", None)
                if handle_copy is not None and handle_copy(event):
                    return
        if discard_detached_selection_hit(screen, event):
            return
        super()._forward_event(event)

    def _copy_selected_text(self, *, clear: bool = False) -> bool:
        return copy_screen_selection(cast(Screen, self), clear=clear)


def copy_text_area_or_screen_selection(text_area: TextArea, *, clear: bool = False) -> bool:
    """Copy a text-area selection, falling back to the owning screen selection."""
    selected = text_area.selected_text
    if selected:
        copy_text_to_clipboards(text_area.app, selected)
        if clear:
            _, end = text_area.selection
            text_area.selection = type(text_area.selection).cursor(end)
        return True
    return copy_screen_selection(text_area.screen, clear=clear)


def handle_text_area_right_click_copy(text_area: TextArea, event: MouseDown) -> bool:
    """Copy selected text for a text-area right-click and consume the event."""
    if event.button != 3:
        return False
    if not copy_text_area_or_screen_selection(text_area, clear=True):
        return False
    cancel_textual_mouse_chain(text_area.screen)
    event.prevent_default()
    event.stop()
    return True
