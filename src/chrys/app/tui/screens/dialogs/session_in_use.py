# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An explicit, idle-only takeover for a contested interactive chat restore."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from textual.widgets import Button

from chrys.app.tui.screens.dialogs.confirm import SESSION_IN_USE_OK, SESSION_IN_USE_TITLE, ConfirmDialog
from chrys.app.tui.session_takeover import query_owner
from chrys.foundation.i18n import DisplayBlock, msg
from chrys.foundation.util.lock import FileLock
from chrys.service.state.store import session_active_owner_path
from chrys.service.state.takeover import TakeoverOwner, read_owner_record

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController

_TAKE = msg("tui.main.session_in_use.take", fallback="Take Over")
_MESSAGE = msg(
    "tui.main.session_in_use.take_message",
    fallback="{message}\n\nTake Over closes the other instance and opens this conversation here.",
    multiline=True,
)


def lock_available(path: Path, timeout: float) -> bool:
    try:
        with FileLock(path.with_suffix(".lock"), timeout=timeout):
            return True
    except TimeoutError, OSError:
        return False


class SessionInUseDialog(ConfirmDialog):
    AUTO_FOCUS = "#confirm-no"
    _initial_button = "#confirm-no"

    def __init__(
        self,
        message: str,
        sessions_root: Path,
        session_id: str,
        generation: str,
        *,
        locale_controller: LocaleController | None,
    ) -> None:
        super().__init__(
            SESSION_IN_USE_TITLE.bind(),
            message,
            _TAKE.bind(),
            SESSION_IN_USE_OK.bind(),
            confirm_variant="error",
            locale_controller=locale_controller,
        )
        self.add_class("-warning-border")
        self._sessions_root = sessions_root
        self._owner_path = self._current_owner_path(session_id)
        self._session_id = session_id
        self._generation = generation
        self._peer: TakeoverOwner | None = None
        self._polling = False
        self._plain_message = message
        self.owner_changed = False

    def _current_owner_path(self, session_id: str) -> Path:
        path = session_active_owner_path(self._sessions_root, session_id)
        return path.parent.resolve() / path.name

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        button = self.query_one("#confirm-yes", Button)
        button.display = False
        button.disabled = True
        self.set_interval(1.0, self._refresh_owner)
        self.call_after_refresh(self._refresh_owner)

    def action_switch_focus(self, direction: int = 1) -> None:
        if self.query_one("#confirm-yes", Button).display:
            super().action_switch_focus(direction)

    async def _refresh_owner(self) -> None:
        if self._polling or self.dismiss_requested or self.app.screen is not self:
            return
        self._polling = True
        try:
            await self._update_offer()
        finally:
            self._polling = False

    async def _update_offer(self) -> None:
        record = await asyncio.to_thread(read_owner_record, self._owner_path)
        if self.dismiss_requested or not self.is_attached:
            return
        if self._current_owner_path(self._session_id) != self._owner_path or (
            record and record.get("takeover_generation") != self._generation
        ):
            self._peer = None
            self.owner_changed = True
            self.dismiss_when_topmost(False)
            return
        if self._peer is None and record:
            self._peer = await asyncio.to_thread(
                TakeoverOwner.read, self._owner_path, self._session_id, self._generation
            )
        available = bool(self._peer and await query_owner(self._peer, "status"))
        if not available:
            available = await asyncio.to_thread(lock_available, self._owner_path, 0)
        if self.dismiss_requested or not self.is_attached:
            return
        button = self.query_one("#confirm-yes", Button)
        if button.has_focus and not available:
            self._focus_confirm_button()
        if button.display != available:
            button.disabled = not available
            button.display = available
            self._message_value = (
                _MESSAGE.bind(message=DisplayBlock(self._plain_message)) if available else self._plain_message
            )
            self.refresh_localization()

    async def take_over(self) -> None:
        if self._peer is not None and self._current_owner_path(self._session_id) == self._owner_path:
            accepted = await query_owner(self._peer, "take")
            if accepted:
                await asyncio.to_thread(lock_available, self._owner_path, 15.0)
        # A missed reply, owner change or natural exit is resolved by normal restore.
