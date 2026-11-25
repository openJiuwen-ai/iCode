# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic confirmation dialog — reusable yes/no modal overlay."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar, Literal

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual._path import CSSPathType
from textual.binding import Binding
from textual.containers import VerticalGroup, VerticalScroll
from textual.css.query import NoMatches
from textual.errors import NoWidget
from textual.widgets import Button, Static, Tooltip

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets import DialogButtonRow, DialogButtonSpec
from chrys.app.tui.widgets.task_summary import TaskSummary, TaskSummaryData, display_command
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.geometry import Size

    from chrys.app.tui.i18n import LocaleController


_CONFIRM_TITLE = msg("tui.confirm.title", fallback="Confirm")
_CONFIRM_MESSAGE = msg("tui.confirm.message", fallback="Are you sure?")
_CONFIRM_LABEL = msg("tui.confirm.button.confirm", fallback="Confirm")
_CANCEL_LABEL = msg("tui.confirm.button.cancel", fallback="Cancel")

_DEFAULT_CONFIRM_TITLE = _CONFIRM_TITLE.bind()
_DEFAULT_CONFIRM_MESSAGE = _CONFIRM_MESSAGE.bind()
_DEFAULT_CONFIRM_LABEL = _CONFIRM_LABEL.bind()
_DEFAULT_CANCEL_LABEL = _CANCEL_LABEL.bind()

type ConfirmLabel = MessageRef | str
type ConfirmMessage = ConfirmLabel | Text


class ConfirmDialog(BaseDialog[bool]):
    """Compact confirmation dialog.

    Dismisses with ``True`` (confirmed) or ``False`` (cancelled / Esc).
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
        Binding("left", "switch_focus(-1)", show=False),
        Binding("right", "switch_focus(1)", show=False),
    ]

    CSS_PATH = "confirm.tcss"
    _minimum_width = 43
    _initial_button = "#confirm-yes"

    def __init__(
        self,
        title: ConfirmLabel = _DEFAULT_CONFIRM_TITLE,
        message: ConfirmMessage = _DEFAULT_CONFIRM_MESSAGE,
        confirm_label: ConfirmLabel = _DEFAULT_CONFIRM_LABEL,
        cancel_label: ConfirmLabel | None = _DEFAULT_CANCEL_LABEL,
        confirm_variant: str = "primary",
        *,
        locale_controller: LocaleController | None = None,
        alternate_label: ConfirmLabel | None = None,
        bold_message_prefix: bool = False,
        tasks: tuple[TaskSummaryData, ...] = (),
        impact: tuple[ConfirmLabel, ...] = (),
        message_align: Literal["center", "left"] = "center",
    ) -> None:
        self._locale_controller = locale_controller
        self._title_message = title
        self._message_value = message
        self._confirm_label_message = confirm_label
        self._cancel_label_message = cancel_label
        self._alternate_label_message = alternate_label
        self.selected_alternate = False
        self._bold_message_prefix = bold_message_prefix
        self._tasks = tasks
        self._impact = impact
        self._title = self._render_message(title)
        self._message = self._render_dialog_message(message)
        self._confirm_label = self._render_message(confirm_label)
        self._cancel_label = self._render_message(cancel_label) if cancel_label is not None else None
        self._confirm_variant = confirm_variant
        # A message with structured details (arguments, quoted text) reads as a left-aligned block.
        super().__init__(classes="-left-message" if message_align == "left" else None)

    def compose(self) -> ComposeResult:
        self._title = self._render_message(self._title_message)
        self._message = self._render_dialog_message(self._message_value)
        with VerticalGroup(id="confirm-container") as container:
            container.border_title = Text(self._title)
            container.styles.width = self._dialog_width(self.app.size.width)
            with VerticalGroup(id="confirm-inner"):
                yield from self.compose_message()
                confirm_label = self._render_message(self._confirm_label_message)
                specs = [DialogButtonSpec(Text(confirm_label), id="confirm-yes", variant=self._confirm_variant)]
                if self._alternate_label_message is not None:
                    alternate_label = self._render_message(self._alternate_label_message)
                    specs.append(DialogButtonSpec(Text(alternate_label), id="confirm-alternate"))
                if self._cancel_label_message is not None:
                    cancel_label = self._render_message(self._cancel_label_message)
                    specs.append(DialogButtonSpec(Text(cancel_label), id="confirm-no", variant="warning"))
                yield DialogButtonRow(*specs, id="confirm-buttons")

    def compose_message(self) -> ComposeResult:
        body = self._render_dialog_message(self._message_value)
        message = Static(body if isinstance(body, Text) else Text(body), id="confirm-message")
        if self._tasks:
            yield message
            with VerticalScroll(id="notice-scroll", classes="confirm-scroll"):
                for task in self._tasks:
                    yield TaskSummary(task, self._render_message)
            yield Static(Text(self._impact_text()), id="confirm-impact")
        else:
            with VerticalScroll(id="notice-scroll", classes="confirm-scroll"):
                yield message

    def _impact_text(self) -> str:
        return "\n".join(self._render_message(item) for item in self._impact)

    def _render_message(self, message: ConfirmLabel) -> str:
        if isinstance(message, str):
            return message
        controller = self._locale_controller
        return format_message(message) if controller is None else render_str(controller.localizer, message)

    def _render_dialog_message(self, message: ConfirmMessage) -> str | Text:
        if isinstance(message, Text):
            return message
        rendered = self._render_message(message)
        if not self._bold_message_prefix:
            return rendered
        text = Text(rendered)
        prefix_end = rendered.find("\n")
        text.stylize("bold", 0, len(rendered) if prefix_end < 0 else prefix_end)
        return text

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        # Descendants have mounted, but the screen has no layout yet. Use the
        # viewport to size the buttons and scrolling body before the first paint.
        self._apply_dialog_size(self.app.size)
        # Focus waits for the first layout.
        self.call_after_refresh(self._focus_confirm_button)

    def on_resize(self) -> None:
        self.call_after_refresh(self._size_dialog)

    def _maybe_clear_tooltip(self, _: object) -> None:
        # A long command tooltip can cover its anchor. Its first layout must
        # not dismiss it merely because the tooltip is now under the pointer.
        anchor = self._tooltip_widget
        if anchor is not None and anchor.has_class("task-command") and anchor.region.contains(*self.app.mouse_position):
            with contextlib.suppress(NoWidget):
                under_mouse, _region = self.get_widget_at(*self.app.mouse_position)
                if isinstance(under_mouse, Tooltip):
                    return
        super()._maybe_clear_tooltip(_)

    def _dialog_width(self, available_width: int) -> int:
        """Use the same content-based width for the first frame and later resizes."""
        message = self._message.plain if isinstance(self._message, Text) else self._message
        needed = max(cell_len(self._title) + 6, *(cell_len(line) + 6 for line in message.split("\n")))
        for task in self._tasks:
            # A preview has two lines; commands can widen only to the last tier.
            needed = max(needed, (max(map(cell_len, display_command(task.command).split("\n"))) + 1) // 2 + 7)
        minimum = 64 if self._tasks else self._minimum_width
        desired = next((tier for tier in (minimum, 80, 100) if tier >= needed), 100)
        return max(8, min(desired, int(available_width * 0.9)))

    def _size_dialog(self) -> None:
        if not self.is_mounted or not self.is_attached or self.dismiss_requested:
            return
        self._apply_dialog_size(self.size)

    def _apply_dialog_size(self, viewport: Size) -> None:
        width = self._dialog_width(viewport.width)
        container = self.query_one("#confirm-container")
        container.styles.width = width
        buttons = list(self.query_one("#confirm-buttons").query(Button))
        stacked = sum(max(16, cell_len(str(button.label)) + 4) + 2 for button in buttons) + 2 > width - 2
        row = self.query_one("#confirm-buttons")
        row.styles.layout = "vertical" if stacked else "horizontal"
        row.styles.height = 3 * len(buttons) if stacked else 3
        for button in buttons:
            button.styles.min_width = 0 if stacked else 16
            button.styles.width = "1fr" if stacked else "auto"
        content_width = max(2, width - 7)
        for task_row in self.query(TaskSummary):
            task_row.refresh_preview(content_width)
        fixed = 2 + (3 * len(buttons) if stacked else 3)
        if self._tasks:
            impact = self.query_one("#confirm-impact", Static)
            impact.update(Text(self._impact_text()))
            for widget in (self.query_one("#confirm-message", Static), impact):
                fixed += widget.get_content_height(viewport, viewport, max(2, width - 6)) + 2
        self.query_one(".confirm-scroll").styles.max_height = max(1, int(viewport.height * 0.9) - fixed)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self._title = self._render_message(self._title_message)
        self._message = self._render_dialog_message(self._message_value)
        self._confirm_label = self._render_message(self._confirm_label_message)
        self.query_one("#confirm-container").border_title = Text(self._title)
        self.query_one("#confirm-message", Static).update(
            self._message if isinstance(self._message, Text) else Text(self._message)
        )
        self.query_one("#confirm-yes", Button).label = Text(self._confirm_label)
        if self._cancel_label_message is not None:
            self._cancel_label = self._render_message(self._cancel_label_message)
            self.query_one("#confirm-no", Button).label = Text(self._cancel_label)
        if self._alternate_label_message is not None:
            self.query_one("#confirm-alternate", Button).label = Text(
                self._render_message(self._alternate_label_message)
            )
        self._size_dialog()

    def _focus_confirm_button(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one(self._initial_button, Button).focus()

    @on(Button.Pressed, "#confirm-yes")
    def _on_confirm(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-alternate")
    def _on_alternate(self, event: Button.Pressed) -> None:
        self.selected_alternate = True
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(False)

    def action_switch_focus(self, direction: int = 1) -> None:
        if self._cancel_label is None:
            return
        yes = self.query_one("#confirm-yes", Button)
        no = self.query_one("#confirm-no", Button)
        if self._alternate_label_message is not None:
            alternate = self.query_one("#confirm-alternate", Button)
            buttons = (yes, alternate, no)
            focused = next((index for index, button in enumerate(buttons) if button.has_focus), None)
            (yes if focused is None else buttons[(focused + direction) % len(buttons)]).focus()
        elif yes.has_focus:
            no.focus()
        else:
            yes.focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _default_dismiss_result(self) -> bool:  # type: ignore[override]
        return False


class StartupSessionChoiceDialog(ConfirmDialog):
    """Focus Retry so an Enter left in the input queue cannot start a new session."""

    AUTO_FOCUS = "#confirm-no"
    _initial_button = "#confirm-no"


class NoticeDialog(ConfirmDialog):
    """Scrollable feedback and cancellable progress using the standard dialog controls."""

    CSS_PATH: ClassVar[CSSPathType | None] = ["confirm.tcss", "notice.tcss"]
    _minimum_width = 64

    def finish(self) -> None:
        self.dismiss_when_topmost(False)

    def _focus_confirm_button(self) -> None:
        if self.is_attached and self.app.screen is self and not self.dismiss_requested:
            super()._focus_confirm_button()
