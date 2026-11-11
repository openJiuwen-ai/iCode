# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shell-mode, focus, and paste coordination for the main screen."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from textual.widget import Widget

from chrys.app.tui.screens.main.ports import InputFocusView, ShellModeView
from chrys.app.tui.screens.main.state import MainScreenState
from chrys.app.tui.widgets import ASK_USER_INTERACTIVE_CLASS
from chrys.app.tui.widgets.btw import BtwPage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel

if TYPE_CHECKING:
    from textual.events import Paste


class ShellModeController:
    """Coordinate shell-mode layout, terminal fullscreen state, focus, and paste guards."""

    def __init__(
        self,
        *,
        state: MainScreenState,
        shell_view: ShellModeView,
        focus_view: InputFocusView,
        set_shell_mode_state: Callable[[bool], None],
        panel_focus_changed: Callable[[], None],
        dismiss_suggestions: Callable[[], None],
        debug: Callable[[str, str], None],
    ) -> None:
        self._state = state
        self._shell_view = shell_view
        self._focus_view = focus_view
        self._set_shell_mode_state = set_shell_mode_state
        # Told after every shell or fullscreen-terminal state change, which
        # moves what ``keeps_panel_focus`` reports.
        self._panel_focus_changed = panel_focus_changed
        self._dismiss_suggestions = dismiss_suggestions
        self._debug = debug

    def apply(self, active: bool) -> None:
        """Toggle between chat and shell layout."""
        if active:
            self._dismiss_suggestions()
        self._state.shell.active = active
        self._panel_focus_changed()
        if active:
            self._shell_view.enter_shell_mode()
        else:
            self._shell_view.exit_shell_mode()
        self._debug("ShellMode", "on" if active else "off")

    def exit_on_escape(self) -> None:
        """Exit shell mode after the terminal reports a double-Escape."""
        if self._state.shell.active:
            self._set_shell_mode_state(False)

    def command_executed(self, command: str) -> None:
        """Record shell command execution for debug sidebar visibility."""
        self._debug("ShellCommand", command[:60])

    def set_alternate_screen_active(self, active: bool) -> None:
        """Enter or exit fullscreen terminal pass-through mode."""
        self._state.shell.fullscreen_terminal = active
        self._panel_focus_changed()
        self._shell_view.set_alternate_screen_active(active)

    def exit_on_shell_closed(self) -> None:
        """Exit shell mode when the shell process exits."""
        if self._state.shell.active:
            self._set_shell_mode_state(False)

    def keeps_panel_focus(self) -> bool:
        """Whether focus that lands on a display-only panel stays there instead of returning to the input."""
        return self._state.shell.active or self._state.shell.fullscreen_terminal

    def on_descendant_focus(self, widget: object) -> None:
        """Redirect display-only panel focus back to the input bar."""
        if self.keeps_panel_focus():
            return
        has_class = getattr(widget, "has_class", None)
        if callable(has_class) and has_class("status-action-btn"):
            return
        if isinstance(widget, ChatPanel | BtwPage):
            return
        ancestors = getattr(widget, "ancestors_with_self", ())
        for ancestor in ancestors:
            if isinstance(ancestor, Widget) and ancestor.has_class(ASK_USER_INTERACTIVE_CLASS):
                return
        for ancestor in ancestors:
            if isinstance(ancestor, ChatPanel | BtwPage | SidebarPanel):
                self._focus_view.focus_input()
                return

    def on_paste(self, event: Paste) -> None:
        """Route image path drops/pastes into the chat input when it is not focused."""
        # Textual has no public default-prevented accessor; skip events already
        # consumed by the focused widget so normal text-area pastes do not double insert.
        # TODO: Replace this private flag if Textual exposes a public accessor.
        if event._no_default_action:
            return
        if self._state.shell.active or self._state.shell.fullscreen_terminal:
            return
        if not self._focus_view.insert_paste_payload(event.text):
            return
        event.prevent_default()
        event.stop()
