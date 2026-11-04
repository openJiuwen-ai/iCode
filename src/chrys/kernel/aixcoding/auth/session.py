# ruff: noqa: RUF001
# (Chinese log messages keep full-width punctuation by design,
#  aixcoding/docs/batches/auth-logging/design.md signed off 2026-10-29)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Login session: the boundary between the login feature and everything after.

The device-code flow itself lives in :class:`~chrys.kernel.aixcoding.auth.client.AuthClient`
and persistence in :class:`~chrys.kernel.aixcoding.auth.storage.CredentialStore`; this module
binds them into the single object the rest of the app talks to::

    session = get_login_session()

    account = await session.check_silent()      # startup: None => not logged in
    if account is None:
        code = await session.request_device_code()
        ...                                     # UI shows the code, opens browser
        account = await session.complete_login(code)
    token = session.stored_token                # downstream consumers attach this
    user_id = session.stored_user_id            # the ehr, whichever credential is live
    session.logout()                            # /logout

When iCode runs as a child of the AIxCoding desktop, the parent's login is
detected as a :class:`~chrys.kernel.aixcoding.auth.delegation.DelegatedCredential` and
shadows the store -- see :mod:`chrys.kernel.aixcoding.auth.delegation` for the contract.

The module-level :func:`get_login_session` is a process-wide singleton on
purpose: when the crypto backend degrades to :class:`~chrys.kernel.aixcoding.auth.crypto.MemoryBackend`
the credential lives inside the backend instance, so every caller must share
one session or "logged in" state would fragment.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from .client import AuthClient
from .crypto import ProtectBackend
from .delegation import DelegatedCredential, detect_delegation
from .environments import resolve_endpoints, resolve_environment
from .errors import AuthError, AuthServerError, DevicePollCancelled
from .storage import CredentialStore, default_config_dir
from .types import (
    AccountInfo,
    DeviceCode,
    Environment,
    StoredCredential,
)

# AIxCoding auth logging: state-transition records on the "aixcoding.auth" logger
# (the aixcoding.* subtree sinks/panel switch live in kernel/aixcoding/diagnostics),
# design aixcoding/docs/batches/auth-logging/design.md (signed off 2026-10-29; decoupled
# 2026-10-30: plain getLogger, no diagnostics import -- kernel cross-subpackage
# imports are architecturally dead-ended, see design). Sensitive fields (ehr) go to
# the file-only "aixcoding.detail.auth" logger instead (ADR-0017, detail-logging
# batch): that subtree never propagates, so its records stay in aixcoding.detail.log
# and out of the F6 panel and the upstream debug log.
_log = logging.getLogger("aixcoding.auth")
_detail_log = logging.getLogger("aixcoding.detail.auth")

#: AIxCoding detail logging: level of the file-only channel; mirrors
#: ``DETAIL_LEVEL`` in diagnostics.logging_support (ADR-0017). Duplicated
#: locally because kernel cross-subpackage imports are forbidden
#: (test_layering); a test pins both definitions together.
_DETAIL_LEVEL = 5


def _reason(exc: BaseException, *, with_cause: bool = True) -> str:
    """Render the failure reason: class names only, never message/traceback."""
    text = f"exc={type(exc).__name__}"
    if with_cause and exc.__cause__ is not None:
        text += f" cause={type(exc.__cause__).__name__}"
    return text


#: Fixed poll cadence: the production reference ignores the server-sent
#: ``result.interval`` and repolls every second.
DEFAULT_POLL_INTERVAL = 1.0


def _default_backend() -> ProtectBackend:
    """Resolve the OS crypto backend lazily (probe runs only when needed)."""
    from .crypto import get_backend

    return get_backend()


class LoginSession:
    """One environment's login state: silent check, login flow, logout."""

    def __init__(
        self,
        *,
        environment: Environment | None = None,
        endpoints: tuple[str, str] | None = None,
        config_dir: Path | None = None,
        backend: ProtectBackend | None = None,
        http: httpx.AsyncClient | None = None,
        delegated: DelegatedCredential | None = None,
    ) -> None:
        """Build a session; every argument is optional (tests inject fakes).

        ``endpoints`` overrides environment-based URL resolution with an
        explicit ``(auth_url, data_url)`` -- used by tests against the loopback
        mock and by any deployment with unusual addressing.  ``delegated``
        pins a parent-provided credential; ``None`` (the default) auto-detects
        one from the frozen process environment.
        """
        self.environment = environment or resolve_environment()
        self._endpoints = endpoints
        self._store = CredentialStore(
            config_dir if config_dir is not None else default_config_dir(),
            backend if backend is not None else _default_backend(),
        )
        self._http = http
        self._delegated = delegated if delegated is not None else detect_delegation()
        self._delegated_rejected = False
        # AIxCoding catalog-auth-gating (decision 乙 revised 2026-11-04): the app
        # layer registers this hook (overwriting on re-registration, since the
        # session outlives any screen); fired once per server-rejected verify.
        self.on_credential_rejected: Callable[[], None] | None = None

    @property
    def store(self) -> CredentialStore:
        """The credential store -- exposed for tests and migrations."""
        return self._store

    @property
    def _active_delegated(self) -> DelegatedCredential | None:
        if self._delegated is None or self._delegated_rejected:
            return None
        return self._delegated

    @property
    def delegated_credential(self) -> DelegatedCredential | None:
        """The parent-provided credential while it shadows the store.

        ``None`` covers both "standalone process" and "the parent's token was
        rejected server-side" -- after a rejection the session falls back to
        the stored credential, if any, instead of reporting a login that no
        longer works.
        """
        return self._active_delegated

    @property
    def stored_token(self) -> str | None:
        """The live token, or ``None`` when absent/expired/corrupt.

        A delegation (desktop parent) wins over the store; after the server
        rejects the delegated token the store answers again.  This is the
        hand-off point for downstream consumers (post-login features attach
        it to their requests); it never performs network I/O.
        """
        delegated = self._active_delegated
        if delegated is not None:
            return delegated.token
        credential = self._store.load(self.environment)
        if credential is None or credential.is_expired or not credential.token:
            return None
        return credential.token

    @property
    def stored_user_id(self) -> str | None:
        """The logged-in user's id (``ehr``), or ``None`` without a live credential.

        The counterpart of :attr:`stored_token`: same source, same precedence
        (a delegation shadows the store), no network I/O. It exists because a
        request may need to *name* the user and not just authenticate as them
        — the remote model config is served per user, and falls back to a
        public default for an empty one.
        """
        delegated = self._active_delegated
        if delegated is not None:
            return delegated.ehr or None
        credential = self._store.load(self.environment)
        if credential is None or credential.is_expired or not credential.token:
            return None
        return credential.user_id or credential.ehr or None

    def _make_client(self) -> AuthClient:
        if self._endpoints is not None:
            auth_url, data_url = self._endpoints
        else:
            auth_url, data_url = resolve_endpoints(self.environment)
        return AuthClient(auth_url, data_url, http=self._http)

    def _clear_rejected_credential(self) -> None:
        """The single clearing point for a server-rejected live credential.

        AIxCoding catalog-auth-gating (review F8): shared by ``check_silent``
        and ``verify_live_credential`` so both paths apply identical semantics —
        a delegated credential only stops shadowing (the parent owns it), a
        stored one is erased so the next login starts clean.
        """
        if self._active_delegated is not None:
            self._delegated_rejected = True
        else:
            self._store.clear(self.environment)

    def _notify_credential_rejected(self) -> None:
        """Fire the app-layer hook; a callback failure must never eat the clear."""
        callback = self.on_credential_rejected
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            # AIxCoding 方案六 (B5): detail volume only.
            _detail_log.log(_DETAIL_LEVEL, "登录失效通知回调失败；env=%s %s", self.environment.value, _reason(exc))

    async def check_silent(self) -> AccountInfo | None:
        """Startup check: the live credential validated against ``user/info``.

        ``None`` means "needs a login": nothing live, TTL elapsed, or the
        server could not be reached.  A stored credential the server rejects
        (``用户不存在``) is cleared so the next login starts clean, while a
        network failure keeps it -- being offline is not a logout.  A
        delegated credential follows the same split with one difference: the
        parent owns it, so a rejection only stops it from shadowing the store
        (nothing local is destroyed; the parent's re-login respawns us).
        """
        delegated = self._active_delegated
        if delegated is not None:
            try:
                account = await self._make_client().fetch_user_info(delegated.token)
            except AuthServerError as exc:
                _log.error(
                    "桌面端传入的登录已失效，改用本机登录；env=%s %s",
                    self.environment.value,
                    _reason(exc, with_cause=False),
                )
                self._clear_rejected_credential()
                # AIxCoding 方案六 (A5): double-write with the reason chain on detail.
                _detail_log.log(
                    _DETAIL_LEVEL,
                    "桌面端传入的登录已失效，改用本机登录；env=%s %s",
                    self.environment.value,
                    _reason(exc, with_cause=False),
                )
                return None
            except AuthError as exc:
                # AIxCoding 方案六: technical detail — detail volume only, never the panel.
                _detail_log.log(
                    _DETAIL_LEVEL,
                    "登录检查未完成（网络不通），登录状态保留；env=%s %s",
                    self.environment.value,
                    _reason(exc),
                )
                return None
            _log.info("已登录（桌面端登录）；env=%s", self.environment.value)
            _detail_log.log(_DETAIL_LEVEL, "已登录（桌面端登录）；ehr=%s env=%s", account.ehr, self.environment.value)
            return account
        credential = self._store.load(self.environment)
        if credential is None or credential.is_expired or not credential.token:
            _log.info("未登录（首次使用或登录已过期）；env=%s", self.environment.value)
            # AIxCoding 方案六 (A1): user-facing events are double-written.
            _detail_log.log(_DETAIL_LEVEL, "未登录（首次使用或登录已过期）；env=%s", self.environment.value)
            return None
        try:
            account = await self._make_client().fetch_user_info(credential.token)
        except AuthServerError as exc:
            # AIxCoding auth logging: clear first, then record — a record claiming
            # "cleared" must never precede the clear itself (peer review C1).
            self._clear_rejected_credential()
            _log.error(
                "本机保存的登录已失效，已自动清除，请重新登录；env=%s %s",
                self.environment.value,
                _reason(exc, with_cause=False),
            )
            # AIxCoding 方案六 (A4): double-write with the reason chain on detail.
            _detail_log.log(
                _DETAIL_LEVEL,
                "本机保存的登录已失效，已自动清除，请重新登录；env=%s %s",
                self.environment.value,
                _reason(exc, with_cause=False),
            )
            return None
        except AuthError as exc:
            # AIxCoding 方案六: technical detail — detail volume only.
            _detail_log.log(
                _DETAIL_LEVEL,
                "登录检查未完成（网络不通），登录信息保留；env=%s %s",
                self.environment.value,
                _reason(exc),
            )
            return None
        _log.info("已登录（本机保存的登录）；env=%s", self.environment.value)
        _detail_log.log(_DETAIL_LEVEL, "已登录（本机保存的登录）；ehr=%s env=%s", account.ehr, self.environment.value)
        return account

    async def verify_live_credential(self) -> AccountInfo | None:
        """Pre-flight live validation for the model catalog fetch.

        AIxCoding catalog-auth-gating (decision 乙, revised 2026-11-04): same
        live-credential resolution (delegation wins, so the verified token is
        always the one :attr:`stored_token` hands out) and the same ``user/info``
        validation as :meth:`check_silent`. Side-effect-free EXCEPT for a server
        rejection: an :class:`AuthServerError` clears the credential through the
        shared clearing point and fires :attr:`on_credential_rejected`, so the
        UI badge cannot keep claiming "logged in" (covers the PROD anomaly
        cases: backend reset, deleted account). Offline keeps the credential —
        being offline is not a logout.

        Failures are attributed in the logs HERE (the caller only records the
        skip event): rejection writes one user-facing main-volume record
        without server internals, while the detail volume carries the server
        message and exception chain. Offline carries the exception class chain
        on detail only; the successful round-trip also goes to detail, so a
        skipped sync is diagnosable from that log alone. ``None`` means
        unverified (nothing live, locally expired, server-rejected, or offline).
        """
        token = self.stored_token
        if not token:
            return None
        _detail_log.log(_DETAIL_LEVEL, "登录预验证请求 user/info；env=%s", self.environment.value)
        try:
            account = await self._make_client().fetch_user_info(token)
        except AuthServerError as exc:
            was_delegated = self._active_delegated is not None
            # Clear first, then record and notify (peer review C1 ordering).
            self._clear_rejected_credential()
            # AIxCoding 方案六 (B3 revised): the rejection is a user-facing state
            # change — a terse main-volume record (same wording as check_silent's
            # A4/A5, no server internals) alongside the detail technical record.
            if was_delegated:
                _log.error("桌面端传入的登录已失效，改用本机登录；env=%s", self.environment.value)
            else:
                _log.error("本机保存的登录已失效，已自动清除，请重新登录；env=%s", self.environment.value)
            _detail_log.log(
                _DETAIL_LEVEL,
                "登录预验证：服务端拒绝凭证，已自动清除，请重新登录；env=%s %s",
                self.environment.value,
                exc,  # server-side message (e.g. 用户不存在); never carries the token
            )
            _detail_log.log(
                _DETAIL_LEVEL,
                "登录预验证被拒详情；env=%s %s",
                self.environment.value,
                _reason(exc),
            )
            self._notify_credential_rejected()
            return None
        except AuthError as exc:
            # AIxCoding 方案六 (B4): detail volume only.
            _detail_log.log(
                _DETAIL_LEVEL,
                "登录预验证：网络不通，本轮跳过，凭证保留；env=%s %s",
                self.environment.value,
                _reason(exc),
            )
            return None
        _detail_log.log(_DETAIL_LEVEL, "登录预验证通过；ehr=%s env=%s", account.ehr, self.environment.value)
        return account

    async def request_device_code(self) -> DeviceCode:
        """Start a device authorization (the UI shows the returned code)."""
        return await self._make_client().request_device_code()

    async def complete_login(
        self,
        device_code: DeviceCode,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AccountInfo:
        """Poll the grant to its end, store the credential, return the user.

        Raises on every non-happy path (:class:`~chrys.kernel.aixcoding.auth.errors.AuthError`
        family); nothing is stored unless the full chain succeeds.  Under an
        active delegation the store write still happens but stays shadowed
        until the delegated token is rejected -- the parent's session wins.
        """
        client = self._make_client()
        try:
            token = await client.poll_token(
                device_code.device_code,
                interval=DEFAULT_POLL_INTERVAL,
                cancel_event=cancel_event,
            )
            account = await client.fetch_user_info(token.token)
        except DevicePollCancelled:
            _log.info("取消登录；env=%s", self.environment.value)
            _detail_log.log(_DETAIL_LEVEL, "取消登录；env=%s", self.environment.value)  # 方案六 A8
            raise
        except AuthError as exc:
            _log.error("登录失败：登录未完成，请稍后重试；env=%s", self.environment.value)
            # AIxCoding 方案六 (A7): double-write with the reason chain on detail.
            _detail_log.log(_DETAIL_LEVEL, "登录失败；env=%s %s", self.environment.value, _reason(exc))
            raise
        self._store.store(
            self.environment,
            StoredCredential.issued_now(
                environment_id=self.environment.value,
                user_id=account.ehr,
                token=token.token,
                refresh_token=token.refresh_token,
                ehr=account.ehr,
            ),
        )
        _log.info("登录成功；env=%s", self.environment.value)
        _detail_log.log(_DETAIL_LEVEL, "登录成功；ehr=%s env=%s", account.ehr, self.environment.value)
        return account

    def logout(self) -> None:
        """Remove the credential for this environment (idempotent).

        A no-op while a delegation is active: the parent app owns that
        session, and only its own logout (or expiry) can end it -- the child
        cannot revoke the token server-side anyway.
        """
        if self._active_delegated is not None:
            _log.info("跳过登出（当前使用桌面端登录，由桌面端管理）；env=%s", self.environment.value)
            _detail_log.log(
                _DETAIL_LEVEL, "跳过登出（当前使用桌面端登录，由桌面端管理）；env=%s", self.environment.value
            )  # 方案六 A10
            return
        self._store.clear(self.environment)
        _log.info("已登出；env=%s", self.environment.value)
        _detail_log.log(_DETAIL_LEVEL, "已登出；env=%s", self.environment.value)  # 方案六 A9


_default_session: LoginSession | None = None


def get_login_session() -> LoginSession:
    """The process-wide session (see the module docstring for why it's one)."""
    global _default_session
    if _default_session is None:
        _default_session = LoginSession()
    return _default_session
