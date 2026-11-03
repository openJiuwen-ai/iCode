# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""verify_live_credential: same user/info validation as check_silent, zero side effects.

catalog-auth-gating (decision 乙): the catalog pre-flight must never clear the
store or flip delegated state — clearing and the login-indicator refresh stay
on auth's own check path (check_silent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.kernel.aixcoding.auth import AccountInfo, Environment, LoginSession, StoredCredential
from chrys.kernel.aixcoding.auth.crypto import MemoryBackend
from chrys.kernel.aixcoding.auth.delegation import DelegatedCredential
from chrys.kernel.aixcoding.auth.errors import AuthError, AuthNetworkError, AuthServerError


def _session(tmp_path: Path, *, delegated: DelegatedCredential | None = None) -> LoginSession:
    return LoginSession(
        environment=Environment.LOCAL,
        endpoints=("http://127.0.0.1:1/api", "http://127.0.0.1:1/api"),
        config_dir=tmp_path,
        backend=MemoryBackend(),
        delegated=delegated,
    )


def _store_token(session: LoginSession, token: str = "store-token", *, expires_at: float = 0.0) -> None:
    session.store.store(
        session.environment,
        StoredCredential(
            environment_id=session.environment.value,
            user_id="u",
            token=token,
            ehr="1234567",
            expires_at=expires_at,
        ),
    )


class _FakeClient:
    """fetch_user_info stub: returns an account or raises, records the tokens seen."""

    def __init__(self, *, account: AccountInfo | None = None, error: AuthError | None = None) -> None:
        self._account = account
        self._error = error
        self.calls: list[str] = []

    async def fetch_user_info(self, token: str) -> AccountInfo:
        self.calls.append(token)
        if self._error is not None:
            raise self._error
        assert self._account is not None
        return self._account


def _wire(session: LoginSession, client: _FakeClient) -> None:
    session._make_client = lambda: client  # type: ignore[method-assign]


async def test_valid_stored_credential_verifies_without_touching_the_store(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _store_token(session)
    account = AccountInfo(ehr="1234567")
    client = _FakeClient(account=account)
    _wire(session, client)

    assert await session.verify_live_credential() is account
    assert client.calls == ["store-token"]
    assert session.stored_token == "store-token"
    assert session.store.load(session.environment) is not None, "a successful verify must not rewrite the store"


async def test_server_rejection_returns_none_and_never_clears(tmp_path: Path) -> None:
    """The split from check_silent: rejection must NOT clear (auth's own path owns clearing)."""
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthServerError("用户不存在")))

    assert await session.verify_live_credential() is None
    assert session.stored_token == "store-token", "verify-only must not clear a rejected credential"
    assert session.store.load(session.environment) is not None


async def test_network_failure_returns_none_and_keeps_the_credential(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthNetworkError("offline")))

    assert await session.verify_live_credential() is None
    assert session.stored_token == "store-token", "offline is not a logout"


async def test_no_live_credential_short_circuits_before_any_request(tmp_path: Path) -> None:
    session = _session(tmp_path)
    client = _FakeClient(account=AccountInfo())
    _wire(session, client)

    assert await session.verify_live_credential() is None
    assert client.calls == [], "nothing live means no user/info round-trip"


async def test_locally_expired_credential_is_not_verified_online(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _store_token(session, expires_at=1.0)  # long past: stored_token already refuses it locally
    client = _FakeClient(account=AccountInfo())
    _wire(session, client)

    assert await session.verify_live_credential() is None
    assert client.calls == []


async def test_delegated_token_is_the_one_verified_and_state_is_untouched(tmp_path: Path) -> None:
    session = _session(tmp_path, delegated=DelegatedCredential(token="delegated-token", ehr="1234567"))
    _store_token(session)  # the store holds a different token; delegation must win
    client = _FakeClient(error=AuthServerError("用户不存在"))
    _wire(session, client)

    assert await session.verify_live_credential() is None
    assert client.calls == ["delegated-token"], "the verified token is the one stored_token hands out"
    assert session._delegated_rejected is False, "verify-only must not flip delegated state"
    assert session.stored_token == "delegated-token"


@pytest.mark.parametrize("error", [AuthServerError("用户不存在"), AuthNetworkError("offline")])
async def test_repeated_verify_is_read_only_idempotent(tmp_path: Path, error: AuthError) -> None:
    """Concurrent verify-only + silent check is safe: verify never mutates (design assumption 3)."""
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=error))

    assert await session.verify_live_credential() is None
    assert await session.verify_live_credential() is None
    assert session.stored_token == "store-token"
