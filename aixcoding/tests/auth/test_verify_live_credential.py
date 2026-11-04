# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""verify_live_credential: pre-flight validation with attributed failure logs.

catalog-auth-gating (decision 乙, revised 2026-11-04): the verify is read-only
for offline/no-credential outcomes, but a server rejection clears the credential
through the shared clearing point and fires ``on_credential_rejected`` — the UI
badge must not keep claiming "logged in". Rejection writes one user-facing
record without server internals on the main volume; request, response and
failure attribution stay on the detail volume, so a skipped catalog sync is
diagnosable from the detail log alone.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from chrys.kernel.aixcoding.auth import AccountInfo, Environment, LoginSession, StoredCredential
from chrys.kernel.aixcoding.auth.crypto import MemoryBackend
from chrys.kernel.aixcoding.auth.delegation import DelegatedCredential
from chrys.kernel.aixcoding.auth.errors import AuthNetworkError, AuthServerError
from chrys.kernel.aixcoding.diagnostics.logging_support import DETAIL_LEVEL


@pytest.fixture(autouse=True)
def _aix_logger_isolation() -> Iterator[None]:
    """Pin the ``aixcoding`` logger tree so caplog sees the records (auth F5 pattern)."""
    root = logging.getLogger("aixcoding")
    saved_handlers = root.handlers[:]
    saved_propagate = root.propagate
    saved_level = root.level
    root.handlers.clear()
    root.propagate = True
    yield
    root.handlers[:] = saved_handlers
    root.propagate = saved_propagate
    root.setLevel(saved_level)


@pytest.fixture
def detail_capture() -> Iterator[list[logging.LogRecord]]:
    """Capture the file-only detail subtree (ADR-0017), which caplog cannot see."""
    detail = logging.getLogger("aixcoding.detail")
    saved = (detail.handlers[:], detail.propagate, detail.level)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture()
    capture.setLevel(logging.NOTSET)
    logging.addLevelName(DETAIL_LEVEL, "DETAIL")  # normally done by setup_logging
    detail.setLevel(DETAIL_LEVEL)
    detail.propagate = False  # mirror the production preset
    detail.addHandler(capture)
    yield records
    detail.handlers[:] = saved[0]
    detail.propagate, detail.level = saved[1], saved[2]


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

    def __init__(self, *, account: AccountInfo | None = None, error: Exception | None = None) -> None:
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


async def test_server_rejection_clears_the_credential_and_notifies(tmp_path: Path) -> None:
    """Decision 乙 revised: rejection clears through the shared point AND fires the hook."""
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthServerError("user/info rejected the token: 用户不存在")))
    calls: list[str] = []
    session.on_credential_rejected = lambda: calls.append("fired")

    assert await session.verify_live_credential() is None
    assert session.stored_token is None, "a rejected credential must not survive the verify"
    assert session.store.load(session.environment) is None
    assert calls == ["fired"]


async def test_server_rejection_without_a_hook_still_clears(tmp_path: Path) -> None:
    """Pre-registration (startup sync): clear only, no notification, no error (N3)."""
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthServerError("用户不存在")))

    assert await session.verify_live_credential() is None
    assert session.stored_token is None


async def test_network_failure_returns_none_and_keeps_the_credential(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthNetworkError("offline")))
    calls: list[str] = []
    session.on_credential_rejected = lambda: calls.append("fired")

    assert await session.verify_live_credential() is None
    assert session.stored_token == "store-token", "offline is not a logout"
    assert calls == [], "offline must not fire the rejected hook"


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


async def test_delegated_rejection_stops_shadowing_and_falls_back_to_the_store(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    session = _session(tmp_path, delegated=DelegatedCredential(token="delegated-token", ehr="1234567"))
    _store_token(session)  # the store holds a different token; delegation must win
    client = _FakeClient(error=AuthServerError("用户不存在"))
    _wire(session, client)

    with caplog.at_level(logging.WARNING, logger="aixcoding.auth"):
        assert await session.verify_live_credential() is None
    assert client.calls == ["delegated-token"], "the verified token is the one stored_token hands out"
    assert session._delegated_rejected is True, "a rejected delegation stops shadowing (parent owns it)"
    assert session.stored_token == "store-token", "the store answers again after the rejection"
    assert session.store.load(session.environment) is not None, "only the delegation is dropped, not the store"
    main = [r for r in caplog.records if r.name == "aixcoding.auth"]
    assert len(main) == 1
    assert main[0].levelno == logging.ERROR
    assert main[0].getMessage() == "桌面端传入的登录已失效，改用本机登录；env=local"
    assert "用户不存在" not in caplog.text
    details = [r.getMessage() for r in detail_capture]
    rejection = next(m for m in details if "服务端拒绝" in m)
    assert "用户不存在" in rejection


async def test_repeated_offline_verify_keeps_the_credential(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthNetworkError("offline")))

    assert await session.verify_live_credential() is None
    assert await session.verify_live_credential() is None
    assert session.stored_token == "store-token"


async def test_repeated_rejection_is_idempotent(tmp_path: Path) -> None:
    """The second verify finds nothing live and short-circuits: one request, one notification."""
    session = _session(tmp_path)
    _store_token(session)
    client = _FakeClient(error=AuthServerError("用户不存在"))
    _wire(session, client)
    calls: list[str] = []
    session.on_credential_rejected = lambda: calls.append("fired")

    assert await session.verify_live_credential() is None
    assert await session.verify_live_credential() is None
    assert client.calls == ["store-token"]
    assert calls == ["fired"]
    assert session.stored_token is None


async def test_callback_failure_does_not_break_the_clear(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """F7-3: a raising hook is swallowed and logged on the detail volume only (B5)."""
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthServerError("用户不存在")))

    def _boom() -> None:
        raise RuntimeError("app already torn down")

    session.on_credential_rejected = _boom
    with caplog.at_level(logging.WARNING, logger="aixcoding.auth"):
        assert await session.verify_live_credential() is None
    assert session.stored_token is None
    assert "通知回调失败" not in caplog.text, "B5: callback failures stay off the main volume"
    assert any("通知回调失败" in r.getMessage() for r in detail_capture)


async def test_rejection_logs_a_user_event_on_main_and_technical_details_on_detail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """B3: one terse A4 user event on main; server attribution remains on detail only."""
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthServerError("user/info rejected the token: 用户不存在")))

    with caplog.at_level(logging.WARNING, logger="aixcoding.auth"):
        assert await session.verify_live_credential() is None

    main = [r for r in caplog.records if r.name == "aixcoding.auth"]
    assert len(main) == 1, "B3: exactly one user-facing rejection record on the main volume"
    assert main[0].levelno == logging.ERROR
    assert main[0].getMessage() == "本机保存的登录已失效，已自动清除，请重新登录；env=local"
    assert all(term not in caplog.text for term in ("用户不存在", "预验证", "AuthServerError"))
    details = [r.getMessage() for r in detail_capture]
    assert any("登录预验证请求" in m for m in details)
    rejection = next(m for m in details if "服务端拒绝" in m)
    assert "用户不存在" in rejection, "the server's own message is the attribution"
    assert any("登录预验证被拒详情" in m for m in details)
    assert all("store-token" not in m for m in details), "the token never reaches the detail volume"
    assert "store-token" not in caplog.text


async def test_offline_is_attributed_and_mirrored_to_detail(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(error=AuthNetworkError("offline")))

    with caplog.at_level(logging.WARNING, logger="aixcoding.auth"):
        assert await session.verify_live_credential() is None

    assert [r for r in caplog.records if r.name == "aixcoding.auth"] == [], "B4: offline stays off the main volume"
    details = [r.getMessage() for r in detail_capture]
    offline = [m for m in details if "网络不通" in m]
    assert len(offline) == 1, "N1: one detail summary, no duplicate offline-detail row"
    assert "AuthNetworkError" in offline[0]
    assert not any("登录预验证离线详情" in m for m in details)


async def test_successful_verify_logs_detail_only(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """Success is detail-only (ehr included); the main volume stays silent."""
    session = _session(tmp_path)
    _store_token(session)
    _wire(session, _FakeClient(account=AccountInfo(ehr="1234567")))

    with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
        assert await session.verify_live_credential() is not None

    assert [r for r in caplog.records if r.name == "aixcoding.auth"] == []
    details = [r.getMessage() for r in detail_capture]
    assert any("登录预验证请求" in m for m in details)
    assert any("登录预验证通过" in m and "ehr=1234567" in m for m in details)
