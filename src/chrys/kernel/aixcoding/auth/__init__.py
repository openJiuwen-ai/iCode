# ruff: noqa: RUF001
# (Chinese log messages keep full-width punctuation by design,
#  aixcoding/docs/batches/logging-routing-fixes/design.md signed off 2026-11-05)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AIxCoding device-code login: protocol client, storage, and crypto backends.

Public API -- import from :mod:`chrys.kernel.aixcoding.auth` rather than the submodules::

    from chrys.kernel.aixcoding.auth import AuthClient, CredentialStore, Environment, get_backend
    from chrys.kernel.aixcoding.auth import LoginSession, get_login_session, login_token_header_value
    from chrys.kernel.aixcoding.auth import DelegatedCredential, detect_delegation
"""

import logging

from .client import AuthClient
from .crypto import get_backend
from .delegation import (
    COMPAT_EHR_VARIABLE,
    COMPAT_TOKEN_VARIABLE,
    DELEGATED_EHR_VARIABLE,
    DELEGATED_NAME_VARIABLE,
    DELEGATED_TOKEN_VARIABLE,
    DelegatedCredential,
    detect_delegation,
)
from .errors import (
    AuthError,
    AuthNetworkError,
    AuthServerError,
    DevicePollCancelled,
    DevicePollDenied,
    DevicePollTimeout,
    ProtectUnavailable,
)
from .session import LoginSession, get_login_session
from .storage import CredentialStore, default_config_dir
from .types import (
    CREDENTIAL_TTL_SECONDS,
    AccountInfo,
    DeviceCode,
    Environment,
    StoredCredential,
    TokenResult,
)

# AIxCoding logging (batches/logging-routing-fixes, A1): literal-name loggers on
# the aixcoding subtree — the former __name__ logger (chrys.kernel.aixcoding.auth)
# sat outside it, its DEBUG records silenced by root's INFO threshold; zero-
# dependency style established in session.py.
_detail_log = logging.getLogger("aixcoding.detail.auth")

#: AIxCoding detail logging: mirrors DETAIL_LEVEL in diagnostics.logging_support
#: (ADR-0017); duplicated locally — kernel cross-subpackage imports are
#: forbidden (test_layering), same as session.py.
_DETAIL_LEVEL = 5


def login_token_header_value() -> str:
    """The login token as the ``token`` header value, or ``""`` when absent.

    The send-time consumer of the stored login credential: AIxCoding's LLM
    gateway authenticates traffic with the token the login flow stored. The
    engine (and its LLM clients) are built before login can complete, so a
    construction-time snapshot would pin an empty token for the whole
    session -- resolve this per request instead. Reading the session performs
    no network I/O, and every failure (no session, broken storage, degraded
    backend) degrades to ``""`` so an unauthenticated or broken session simply
    sends no header -- a downstream that resolves this per request gets
    login/logout on the next request, never a failed call.
    """
    try:
        stored = get_login_session().stored_token
    except Exception:
        # Read on every request, so a public-level record per call would spam
        # the log — detail volume only (batches/logging-routing-fixes, A1).
        _detail_log.log(_DETAIL_LEVEL, "读取登录凭证失败，token 请求头未附加。", exc_info=True)
        return ""
    return (stored or "").strip()


__all__ = [
    "COMPAT_EHR_VARIABLE",
    "COMPAT_TOKEN_VARIABLE",
    "CREDENTIAL_TTL_SECONDS",
    "DELEGATED_EHR_VARIABLE",
    "DELEGATED_NAME_VARIABLE",
    "DELEGATED_TOKEN_VARIABLE",
    "AccountInfo",
    "AuthClient",
    "AuthError",
    "AuthNetworkError",
    "AuthServerError",
    "CredentialStore",
    "DelegatedCredential",
    "DeviceCode",
    "DevicePollCancelled",
    "DevicePollDenied",
    "DevicePollTimeout",
    "Environment",
    "LoginSession",
    "ProtectUnavailable",
    "StoredCredential",
    "TokenResult",
    "default_config_dir",
    "detect_delegation",
    "get_backend",
    "get_login_session",
    "login_token_header_value",
]
