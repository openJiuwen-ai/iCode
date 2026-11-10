# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ApprovalModeScreen — modal for switching approval mode."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.content import Content
from textual.widgets import OptionList

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets.chrome.app_header import APPROVAL_MODE_MESSAGES
from chrys.app.tui.widgets.option_menu import MenuOption, MenuOptionList, OptionMenu
from chrys.foundation.i18n import MessageDef, msg
from chrys.service.approval.policy import ApprovalMode

if TYPE_CHECKING:
    from textual.app import ComposeResult

_MODE_MANUAL_DESCRIPTION = msg(
    "tui.approval_mode.description.manual",
    fallback="You approve each call that needs approval",
)
MODE_AUTO_DESCRIPTION = msg(
    "tui.approval_mode.description.auto",
    fallback="Auto-approves safe calls, flags suspicious ones",
)
MODE_BYPASS_DESCRIPTION = msg(
    "tui.approval_mode.description.bypass",
    fallback="All tool calls run without approval",
)
_MODE_BYPASS_RELAY_DESCRIPTION = msg(
    "tui.approval_mode.description.bypass_relay",
    fallback="All tool calls run without approval — relay-enhanced review is NOT active",
)
_APPROVAL_MODE_TITLE = msg("tui.approval_mode.title", fallback="Approval Mode")

_MODE_DESCRIPTIONS: dict[ApprovalMode, MessageDef] = {
    ApprovalMode.MANUAL: _MODE_MANUAL_DESCRIPTION,
    ApprovalMode.AUTO: MODE_AUTO_DESCRIPTION,
    ApprovalMode.BYPASS: MODE_BYPASS_DESCRIPTION,
}


class ApprovalModeScreen(BaseDialog[ApprovalMode | None]):
    """Modal for switching approval mode.

    Single click selects and dismisses. Current mode is dimmed and disabled.
    Escape cancels.
    """

    DEFAULT_CSS = "ApprovalModeScreen { align: center middle; }"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CLOSE_BINDING),
    ]

    def __init__(self, current_mode: ApprovalMode, *, relay_active: bool = False) -> None:
        self._current_mode = current_mode
        # While the session's model is a relay, bypass would silence the
        # enhanced review entirely — the option says so instead of implying
        # the same protection as other modes.
        self._relay_active = relay_active
        self._dismissed = False
        super().__init__()

    def compose(self) -> ComposeResult:
        with OptionMenu(id="container") as container:
            container.border_title = Content.from_text(
                render_str(widget_localizer(self), _APPROVAL_MODE_TITLE.bind()),
                markup=False,
            )
            yield MenuOptionList()

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        localizer = widget_localizer(self)
        self.query_one(MenuOptionList).set_items(
            [
                MenuOption(
                    render_str(localizer, APPROVAL_MODE_MESSAGES[mode].bind()),
                    render_str(
                        localizer,
                        (
                            _MODE_BYPASS_RELAY_DESCRIPTION
                            if mode is ApprovalMode.BYPASS and self._relay_active
                            else _MODE_DESCRIPTIONS[mode]
                        ).bind(),
                    ),
                    id=mode.value,
                    current=mode == self._current_mode,
                )
                for mode in ApprovalMode
            ]
        )

    @on(OptionList.OptionSelected)
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id:
            event.stop()
            self._safe_dismiss(ApprovalMode(event.option.id))

    def action_cancel(self) -> None:
        self._safe_dismiss(None)

    def _dismiss_clicked_outside(self) -> None:
        self._safe_dismiss(None)

    def _safe_dismiss(self, result: ApprovalMode | None) -> None:
        if self._dismissed:
            return
        # Local guard gates side effects; the mixin guard only protects Textual's dismiss.
        self._dismissed = True
        with contextlib.suppress(Exception):
            self.query_one("#container").display = False
        self.dismiss(result)
