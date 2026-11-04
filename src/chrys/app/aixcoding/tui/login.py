# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Device-code login dialog: start a grant, wait for the browser, dismiss.

The dialog owns one login attempt end to end:

1. ``on_mount`` starts the flow in a background task (``request_device_code``);
2. the code arrives -> show the user code + verification URL, auto-open the
   browser via :mod:`webbrowser` (the URL is ``verification_uri_complete``,
   which pre-fills the code -- the user only clicks "allow");
3. ``complete_login`` polls in the same task; success dismisses with the
   :class:`~chrys.kernel.aixcoding.auth.AccountInfo`, every failure turns the
   dialog into an error message with a close button.

Closing the dialog at any point (cancel button, ``Escape``, backdrop click)
sets the cancel event, which makes the poll loop raise
:class:`~chrys.kernel.aixcoding.auth.errors.DevicePollCancelled` on its next
checkpoint -- nothing is stored on an interrupted login.

Design: aixcoding/docs/batches/tui/design.md
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import VerticalGroup
from textual.widgets import Button, Static

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets import DialogButtonRow, DialogButtonSpec
from chrys.kernel.aixcoding.auth import (
    AccountInfo,
    AuthError,
    DeviceCode,
    DevicePollCancelled,
    LoginSession,
    get_login_session,
)
from chrys.kernel.aixcoding.diagnostics.logging_support import DETAIL_LEVEL

logger = logging.getLogger(__name__)
_auth_log = logging.getLogger("aixcoding.auth")
_detail_log = logging.getLogger("aixcoding.detail.auth")
_DETAIL_LEVEL = DETAIL_LEVEL

if TYPE_CHECKING:
    from textual.app import ComposeResult

_TITLE = "AIxCoding 登录"
_STATUS_CONNECTING = "正在连接登录服务，请稍候…"
_STATUS_WAITING = "已在浏览器打开验证页面，请完成授权…"
_OPEN_BUTTON = "在浏览器中继续"
_CANCEL_BUTTON = "取消"


class LoginDialog(BaseDialog[AccountInfo | None]):
    """Run one device-code login; dismisses with the account or ``None``."""

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
    ]

    CSS_PATH = "login.tcss"

    def __init__(
        self,
        *,
        session: LoginSession | None = None,
        open_browser: Callable[[str], object] | None = None,
        on_login_success: Callable[[AccountInfo], None] | None = None,
    ) -> None:
        self._session = session if session is not None else get_login_session()
        self._open_browser = open_browser if open_browser is not None else webbrowser.open
        self._on_login_success = on_login_success
        self._cancel_event = asyncio.Event()
        # Not ``_task``: that slot belongs to MessagePump's own loop task, and
        # clobbering it makes every settled-wait read this screen as eternally
        # busy (the cancel flow then deadlocks inside Pilot's barrier).
        self._login_task: asyncio.Task[None] | None = None
        self._uri = ""
        super().__init__()

    def compose(self) -> ComposeResult:
        # Hold direct references: the login task starts at Mount -- possibly
        # before compose() children attach -- so it must never query the DOM.
        # Visibility is class-driven (login.tcss keys off ``login-coded``).
        with VerticalGroup(id="login-container") as container:
            container.border_title = Text(_TITLE)
            with VerticalGroup(id="login-inner"):
                self._status = Static(_STATUS_CONNECTING, id="login-status")
                yield self._status
                self._code_view = Static("", id="login-code")
                yield self._code_view
                self._uri_view = Static("", id="login-uri")
                yield self._uri_view
                yield DialogButtonRow(
                    DialogButtonSpec(_OPEN_BUTTON, id="login-open", variant="primary"),
                    DialogButtonSpec(_CANCEL_BUTTON, id="login-cancel", variant="warning"),
                    id="login-buttons",
                )

    def on_mount(self) -> None:
        self._login_task = asyncio.create_task(self._run_login())

    async def _run_login(self) -> None:
        try:
            code = await self._session.request_device_code()
        except AuthError as exc:
            _auth_log.error("登录失败：无法获取登录码，请稍后重试。")
            _detail_log.log(_DETAIL_LEVEL, "获取登录码失败：%s: %s", type(exc).__name__, exc)
            self._show_failure("无法获取登录码，请稍后重试。")
            return
        self._show_code(code)
        try:
            account = await self._session.complete_login(code, cancel_event=self._cancel_event)
        except DevicePollCancelled:
            return
        except AuthError as exc:
            _detail_log.log(_DETAIL_LEVEL, "授权轮询失败：%s: %s", type(exc).__name__, exc)
            self._show_failure("登录未完成，请稍后重试。")
            return
        if self._on_login_success is not None:
            try:
                self._on_login_success(account)
            except Exception:
                logger.exception("Login success callback failed; continuing to dismiss the dialog.")
        self.dismiss_when_topmost(account)

    def _show_code(self, code: DeviceCode) -> None:
        if self.dismiss_requested:
            return
        self._uri = code.verification_uri_complete or code.verification_uri
        self._status.update(_STATUS_WAITING)
        self._code_view.update(Text(code.user_code, style="bold"))
        self._uri_view.update(self._uri)
        self.add_class("login-coded")
        if self._uri:
            self._open_browser(self._uri)

    def _show_failure(self, message: str) -> None:
        if self.dismiss_requested:
            return
        self.remove_class("login-coded")
        self._status.update(Text(message, style="red"))

    @on(Button.Pressed, "#login-open")
    def _on_open(self, _event: Button.Pressed) -> None:
        if self._uri:
            self._open_browser(self._uri)

    @on(Button.Pressed, "#login-cancel")
    def _on_cancel(self, _event: Button.Pressed) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        """Close the dialog; the in-flight poll stops at its next checkpoint."""
        self.dismiss(None)

    def _before_dismiss(self, _result: object | None = None) -> None:
        self._cancel_event.set()
