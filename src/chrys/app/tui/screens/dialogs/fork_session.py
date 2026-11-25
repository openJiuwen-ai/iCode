# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ForkSessionDialog — choose what to do after creating a session fork."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup
from textual.widgets import Button, Static

from chrys.app.tui.clipboard import copy_text_to_clipboards, notify_after_copy
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.terminal.fork_tab import TAB_UNAVAILABLE, ForkTabTarget
from chrys.app.tui.terminal.launcher import TerminalLaunchError, launch_new_chrys_window
from chrys.app.tui.widgets import ChrysLoadingIndicator, DialogButtonRow, DialogButtonSpec
from chrys.foundation.branding import APP_COMMAND
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.util.session_ids import session_short_id

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController

ForkSessionResult = Literal["stay", "switch", "new_window", "new_tab"]
ForkSessionState = Literal["loading", "success", "error"]
type ForkSessionMessage = MessageRef | str

_FORK_CREATING = msg("tui.fork_session.creating", fallback="Creating session fork...")
_FORK_CREATED = msg("tui.fork_session.created", fallback="Created fork {fork_short_id}.")
_FORK_TITLE_LOADING = msg("tui.fork_session.title.loading", fallback="Forking Session")
_FORK_TITLE_ERROR = msg("tui.fork_session.title.error", fallback="Fork Failed")
_FORK_TITLE_SUCCESS = msg("tui.fork_session.title.success", fallback="Session Forked")
_FORK_SWITCH = msg("tui.fork_session.button.switch", fallback="Switch")
_FORK_OPEN_NEW_WINDOW = msg("tui.fork_session.button.open_new_window", fallback="Open in New Window")
_FORK_NEW_TAB = msg("tui.fork_session.button.new_tab", fallback="Open in New Tab")
_FORK_COPY = msg("tui.fork_session.button.copy_command", fallback="Copy Resume Command")
_FORK_STAY = msg("tui.fork_session.button.stay", fallback="Stay")
_FORK_OK = msg("tui.fork_session.button.ok", fallback="Ok")


class ForkSessionDialog(BaseDialog[ForkSessionResult | None]):
    """Dialog shown after the backend creates a forked session."""

    BINDINGS: ClassVar[list] = [
        Binding("escape", "dismiss_after_result", show=False, priority=True),
        Binding("left", "focus_previous", show=False),
        Binding("right", "focus_next", show=False),
    ]

    CSS_PATH = "fork_session.tcss"

    def __init__(
        self,
        fork_id: str = "",
        *,
        show_new_window: bool = True,
        tab_target: ForkTabTarget | None = None,
        cwd: str = "",
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._locale_controller = locale_controller
        self._fork_id = fork_id
        self._show_new_window = show_new_window
        self._tab_target = tab_target
        self._cwd = cwd
        self._open_error: ForkSessionMessage | None = None if tab_target else TAB_UNAVAILABLE.bind()
        self._opening = False
        self._state: ForkSessionState = "success" if fork_id else "loading"
        self._message_value: ForkSessionMessage = (
            _FORK_CREATED.bind(fork_short_id=session_short_id(fork_id)) if fork_id else _FORK_CREATING.bind()
        )
        self._message = self._render_message(self._message_value)
        self._mounted = False
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="fork-session-container") as container:
            container.border_title = Text(self._title)
            with VerticalGroup(id="fork-session-inner"):
                yield ChrysLoadingIndicator(id="fork-session-loading")
                yield Static(Text(self._render_message(self._message_value)), id="fork-session-message")
                success_specs = [
                    DialogButtonSpec(
                        Text(self._render_message(_FORK_SWITCH.bind())),
                        id="fork-session-switch",
                        variant="primary",
                    )
                ]
                if self._tab_target or self._show_new_window:
                    success_specs.append(
                        DialogButtonSpec(Text(self._open_label), id="fork-session-open", variant="success")
                    )
                success_specs.append(
                    DialogButtonSpec(
                        Text(self._render_message(_FORK_STAY.bind())),
                        id="fork-session-stay",
                        variant="warning",
                    )
                )
                yield Static(id="fork-session-tab-reason")
                yield Static(id="fork-session-command")
                with HorizontalGroup(id="fork-session-copy-row"):
                    yield Button(Text(self._render_message(_FORK_COPY.bind())), id="fork-session-copy", compact=True)
                yield DialogButtonRow(*success_specs, id="fork-session-buttons")
                yield DialogButtonRow(
                    DialogButtonSpec(
                        Text(self._render_message(_FORK_OK.bind())),
                        id="fork-session-ok",
                        variant="warning",
                    ),
                    id="fork-session-error-buttons",
                )

    def on_mount(self) -> None:
        self._mounted = True
        self._apply_state()

    @property
    def _title(self) -> str:
        return self._render_message(self._title_message)

    @property
    def _title_message(self) -> MessageRef:
        if self._state == "loading":
            return _FORK_TITLE_LOADING.bind()
        if self._state == "error":
            return _FORK_TITLE_ERROR.bind()
        return _FORK_TITLE_SUCCESS.bind()

    def set_success(self, fork_id: str) -> None:
        """Swap the loading state for the success action row."""
        self._fork_id = fork_id
        self._state = "success"
        self._message_value = _FORK_CREATED.bind(fork_short_id=session_short_id(fork_id))
        self._message = self._render_message(self._message_value)
        self._apply_state()

    def set_error(self, message: str) -> None:
        """Swap the loading state for an error result."""
        self._state = "error"
        self._message_value = message
        self._message = self._render_message(self._message_value)
        self._apply_state()

    def _apply_state(self) -> None:
        # Owners may still report once its children went, as removal starts, or never mounted, as
        # the app shuts down. Buffer as before mount; nothing will render it.
        if not self._mounted or not self._composed_live:
            return

        self.remove_class("-loading", "-success", "-error")
        self.add_class(f"-{self._state}")
        self.query_one("#fork-session-container", VerticalGroup).border_title = Text(
            self._render_message(self._title_message)
        )
        self.query_one("#fork-session-loading", ChrysLoadingIndicator).display = self._state == "loading"
        self.query_one("#fork-session-message", Static).update(Text(self._render_message(self._message_value)))
        success = self._state == "success"
        reason = self.query_one("#fork-session-tab-reason", Static)
        reason.display = success and self._open_error is not None
        if self._open_error is not None:
            reason.update(Text(self._render_message(self._open_error)))
        command = self.query_one("#fork-session-command", Static)
        command.display = success
        command.update(Text(self._resume_command))
        self.query_one("#fork-session-copy-row", HorizontalGroup).display = success
        if self._tab_target or self._show_new_window:
            self.query_one("#fork-session-open", Button).label = Text(self._open_label)
        self.query_one("#fork-session-buttons", HorizontalGroup).display = self._state == "success"
        self.query_one("#fork-session-error-buttons", HorizontalGroup).display = self._state == "error"
        if self._state == "success":
            self.query_one("#fork-session-switch", Button).focus()
        elif self._state == "error":
            self.query_one("#fork-session-ok", Button).focus()

    @on(Button.Pressed, "#fork-session-stay")
    def _on_stay(self, event: Button.Pressed) -> None:
        event.stop()
        if self._state != "success" or self._opening:
            return
        self.dismiss("stay")

    @on(Button.Pressed, "#fork-session-switch")
    def _on_switch(self, event: Button.Pressed) -> None:
        event.stop()
        if self._state != "success" or self._opening:
            return
        self.dismiss("switch")

    @property
    def _open_label(self) -> str:
        label = _FORK_NEW_TAB if self._tab_target else _FORK_OPEN_NEW_WINDOW
        return self._render_message(label.bind())

    @property
    def _resume_command(self) -> str:
        return f"{APP_COMMAND} -s {self._fork_id}"

    @on(Button.Pressed, "#fork-session-copy")
    def _on_copy(self, event: Button.Pressed) -> None:
        event.stop()
        if self._state == "success":
            dispatched = copy_text_to_clipboards(self.app, self._resume_command)
            notify_after_copy(self, self._render_message(COPIED_TITLE.bind()), dispatched=dispatched, markup=False)

    @on(Button.Pressed, "#fork-session-open")
    def _on_open(self, event: Button.Pressed) -> None:
        event.stop()
        if self._state != "success" or self._opening or self.dismiss_requested:
            return
        self._opening = True
        self.query_one("#fork-session-buttons").disabled = True
        event.button.loading = True
        self.run_worker(self._open_fork())

    async def _open_fork(self) -> None:
        target = self._tab_target
        try:
            if target:
                self._open_error = await target.open(self._fork_id, self._cwd)
                if self._open_error:
                    self._tab_target = None
            else:
                await launch_new_chrys_window(self._fork_id, cwd=self._cwd)
                self._open_error = None
        except TerminalLaunchError as exc:
            self._open_error = exc.display_message or sanitize_legacy_scalar(str(exc))
        except OSError as exc:
            self._open_error = sanitize_legacy_scalar(str(exc))
        finally:
            self._opening = False
        if not self._composed_live:
            return
        if self._open_error is None:
            self.dismiss_when_topmost("new_tab" if target else "new_window")
            return
        button = self.query_one("#fork-session-open", Button)
        button.loading = False
        button.display = self._tab_target is not None or self._show_new_window
        self.query_one("#fork-session-buttons").disabled = False
        self._apply_state()
        self.query_one("#fork-session-copy", Button).focus()

    @on(Button.Pressed, "#fork-session-ok")
    def _on_ok(self, event: Button.Pressed) -> None:
        event.stop()
        if self._state == "loading":
            return
        self.dismiss(None)

    def action_dismiss_after_result(self) -> None:
        if self._state == "loading" or self._opening:
            return
        self.dismiss(self._default_dismiss_result())

    def action_focus_previous(self) -> None:
        self._focus_relative(-1)

    def action_focus_next(self) -> None:
        self._focus_relative(1)

    def _focus_relative(self, offset: int) -> None:
        buttons = [button for button in self.query(Button) if self._button_is_visible(button)]
        if not buttons:
            return
        current = next((index for index, button in enumerate(buttons) if button.has_focus), 0)
        buttons[(current + offset) % len(buttons)].focus()

    @staticmethod
    def _button_is_visible(button: Button) -> bool:
        return button.display and button.parent is not None and button.parent.display

    def _allow_click_outside_dismiss(self) -> bool:
        return self._state != "loading" and not self._opening

    def _default_dismiss_result(self) -> ForkSessionResult | None:  # type: ignore[override]
        return "stay" if self._state == "success" else None

    def _render_message(self, message: ForkSessionMessage) -> str:
        if isinstance(message, str):
            return message
        controller = self._locale_controller
        return format_message(message) if controller is None else render_str(controller.localizer, message)
