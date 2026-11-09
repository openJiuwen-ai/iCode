# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Login/logout actions and the startup silent check for the main screen.

The action bodies live here (not as MainScreen methods) so the screen file
keeps only wiring; the host protocols below name exactly the surface these
actions need -- tests drive them with plain ``SimpleNamespace`` duck slices.

Toasts are Chinese-only constants (ADR-0010). The slash command
description/man constants are the single D2 exception: ``MessageRef``'s
strong typing forces the ``msg(key, fallback=中文)`` shell, registered in the
command table via ``.bind()`` (pot/po/oracle obligations apply, see design).

Design: aixcoding/docs/batches/tui/design.md
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Literal, Protocol

from chrys.foundation.i18n import msg
from chrys.kernel.aixcoding.auth import AccountInfo, get_login_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from textual.screen import Screen

_LOGIN_MANAGED_TOAST = "登录由桌面端管理"
_LOGOUT_MANAGED_TOAST = "登出由桌面端管理"
_NOT_LOGGED_IN_TOAST = "尚未登录"
_LOGGED_OUT_TOAST = "已登出"
_LOGIN_SUCCEEDED_TOAST = "已登录：{name}"
_CREDENTIAL_REJECTED_TOAST = "登录已失效，请重新登录"

# D2 exception (MessageRef strong typing): registered from the command table.
# Keys carry the aixcoding.* prefix (ADR-0010 rule 2: catalog entries stay
# identifiable as fork-owned); msg keys are pure lookup literals with no
# prefix-derivation upstream, so the tui semantic segment simply follows.
DESCRIPTION_LOGIN = msg(
    "aixcoding.tui.commands.description.login",
    fallback="用 AIxCoding 账号登录",
)
DESCRIPTION_LOGOUT = msg(
    "aixcoding.tui.commands.description.logout",
    fallback="登出并清除已存储的凭据",
)
MAN_LOGIN_BODY = msg(
    "aixcoding.tui.man.login.body",
    fallback=(
        "打开 AIxCoding 设备码登录对话框。\n\n"
        "对话框显示用户码并自动在浏览器打开验证\n"
        "页面；在页面上确认授权即可完成登录。\n\n"
        "凭据以操作系统级加密存储，下次启动时\n"
        "静默校验。取消对话框不会存储任何内容。"
    ),
    multiline=True,
)
MAN_LOGOUT_BODY = msg(
    "aixcoding.tui.man.logout.body",
    fallback=(
        "清除当前环境存储的 AIxCoding 凭据并登出。\n\n未登录时执行该命令是安全的：命令会报告\n当前状态，不做任何改动。"
    ),
    multiline=True,
)


class _LoginHostApp(Protocol):
    """The app surface the dialog push needs."""

    def push_screen(
        self,
        screen: Screen[object],
        callback: Callable[[Any], None] | None = None,
    ) -> object: ...


class LoginActionHost(Protocol):
    """The MainScreen surface the login/logout actions need."""

    @property
    def app(self) -> _LoginHostApp: ...

    def notify(
        self,
        message: str,
        *,
        severity: Literal["information", "warning", "error"] = "information",
    ) -> None: ...

    def refresh_login_indicator(self, *, account: AccountInfo | None = None) -> None: ...


class _SilentCheckApp(Protocol):
    """The app surface the rejected-credential hook needs (thread hop)."""

    def call_from_thread(self, callback: Callable[[], object]) -> object: ...


class SilentCheckScreen(Protocol):
    """The screen surface the startup silent check needs."""

    @property
    def app(self) -> _SilentCheckApp: ...

    def notify(
        self,
        message: str,
        *,
        severity: Literal["information", "warning", "error"] = "information",
    ) -> None: ...

    def refresh_login_indicator(self, *, account: AccountInfo | None = None) -> None: ...


def _make_credential_rejected_callback(screen: SilentCheckScreen) -> Callable[[], None]:
    """Build the hook fired when a catalog pre-flight finds the credential rejected.

    catalog-auth-gating (design 方案四): the verify may run on the catalog poll
    thread, so the UI work hops through ``call_from_thread``; when the hook
    already fires on the UI thread (async sync path) that raises RuntimeError
    and we apply directly. The teardown mirrors a manual logout: stop the poll
    (no silent no-credential spinning), toast, refresh the badge.
    """

    def _apply() -> None:
        # Lazy import — the silent check imports this module on every startup.
        from chrys.service.aixcoding.models.catalog import stop_sync_runtime

        stop_sync_runtime()
        screen.notify(_CREDENTIAL_REJECTED_TOAST, severity="warning")
        screen.refresh_login_indicator()

    def _on_rejected() -> None:
        try:
            screen.app.call_from_thread(_apply)
        except RuntimeError:
            _apply()  # already on the UI thread

    return _on_rejected


def open_login_dialog(host: LoginActionHost) -> None:
    """Open the device-code login dialog and toast the outcome."""
    session = get_login_session()
    if session.delegated_credential is not None:
        # The desktop parent already logged in and owns the session.
        host.notify(_LOGIN_MANAGED_TOAST)
        return

    # Lazy: the silent check imports this module on every startup; the dialog
    # class (and its Textual widget definitions) should only load on /login.
    from chrys.app.aixcoding.tui.login import LoginDialog

    def _on_login_success(_account: AccountInfo) -> None:
        # The login changed whose catalog is served: pull one now instead of
        # waiting out an interval that began before it. Lazy import — the
        # silent check imports this module on every startup.
        from chrys.service.aixcoding.models.catalog import resume_sync_runtime

        resume_sync_runtime()

    def _on_login_dismiss(result: object | None) -> None:
        if not isinstance(result, AccountInfo):
            return  # cancelled or unexpected: no toast
        host.notify(_LOGIN_SUCCEEDED_TOAST.format(name=result.display_name))
        host.refresh_login_indicator(account=result)

    host.app.push_screen(LoginDialog(on_login_success=_on_login_success), _on_login_dismiss)


def perform_logout(host: LoginActionHost) -> None:
    """Clear the stored credential, toast the outcome, refresh the badge."""
    session = get_login_session()
    if session.delegated_credential is not None:
        # Only the desktop parent can end its own session.
        host.notify(_LOGOUT_MANAGED_TOAST)
        return
    if session.stored_token is None:
        host.notify(_NOT_LOGGED_IN_TOAST, severity="warning")
        return
    session.logout()
    # A logged-out session has no credential to poll with; stop it so an
    # in-flight sync cannot publish the logged-out account's catalog. Lazy
    # import — the silent check imports this module on every startup.
    from chrys.service.aixcoding.models.catalog import stop_sync_runtime

    stop_sync_runtime()
    host.notify(_LOGGED_OUT_TOAST)
    host.refresh_login_indicator()


async def silent_login_check(screen: SilentCheckScreen) -> None:
    """Validate the stored credential at startup, silently.

    The badge paints the stored state FIRST (logged-out chip, or the stored
    ehr when a credential exists -- 0.28 behaviour via the initial
    refresh_localization sweep): without this the chip stays hidden whenever
    the check itself throws (no credential yet), and "not logged in" would
    never show. Offline is not a logout, a rejected credential self-cleans
    inside the check, and startup must never be disturbed: every outcome of
    the check is swallowed. The refresh afterwards re-reads the session
    rather than the check result -- a network failure keeps the credential
    (stays signed in), a rejection has already cleared it (flips the badge).
    """
    # catalog-auth-gating (方案四): overwrite on every run — the session is a
    # process-wide singleton and must never hold a torn-down screen.
    get_login_session().on_credential_rejected = _make_credential_rejected_callback(screen)
    screen.refresh_login_indicator()
    with contextlib.suppress(Exception):
        account = await get_login_session().check_silent()
        screen.refresh_login_indicator(account=account)
