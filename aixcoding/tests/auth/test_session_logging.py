# (asserts are pytest idiom; fake creds carry secrets; frozen Chinese log
#  messages keep full-width punctuation by design, same as session.py)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session logging tests: 12 state-transition records under ``aixcoding.auth``.

Design: aixcoding/docs/batches/auth-logging/design.md (signed off 2026-10-29);
detail channel split: aixcoding/docs/batches/detail-logging/design.md +
ADR-0017 (signed off 2026-11-02) — the three success records (#1/#5/#8) now
carry ``ehr`` only on the file-only ``aixcoding.detail`` subtree. Every case
asserts the frozen message text, the level, and the two red lines: no secret
ever appears in a record, and no record carries a traceback
(``exc_info is None``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator

import httpx
import pytest
from test_auth_session import MockServer, make_session

from chrys.kernel.aixcoding.auth import Environment, LoginSession, StoredCredential
from chrys.kernel.aixcoding.auth.crypto import MemoryBackend
from chrys.kernel.aixcoding.auth.delegation import DelegatedCredential
from chrys.kernel.aixcoding.auth.errors import DevicePollCancelled, DevicePollDenied
from chrys.kernel.aixcoding.diagnostics.logging_support import DETAIL_LEVEL

DEAD_URL = "http://127.0.0.1:1/api/v1"
EHR = "1234567"


@pytest.fixture(autouse=True)
def _aix_logger_isolation() -> Iterator[None]:
    """Pin the ``aixcoding`` logger tree for these tests (review F5).

    caplog's handler sits on the root logger, so records must propagate out
    of the ``aixcoding`` subtree to be captured; other test modules may have
    left ``propagate=False`` (or a file sink) behind. Save/restore all of it.
    """
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
    """Capture records from the file-only detail subtree (ADR-0017).

    caplog cannot see them: its handler sits on the root logger while the
    detail subtree never propagates. Pin the subtree's level the way
    ``setup_logging`` would and hook a capture handler at the subtree root.
    """
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
    detail.propagate = False  # mirror the production preset (review S1)
    detail.addHandler(capture)
    yield records
    detail.handlers[:] = saved[0]
    detail.propagate, detail.level = saved[1], saved[2]


def auth_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "aixcoding.auth"]


def assert_clean(records: list[logging.LogRecord], *secrets: str) -> None:
    """The two red lines: no traceback, no secret in any rendered message."""
    for record in records:
        assert record.exc_info is None
        message = record.getMessage()
        for secret in secrets:
            assert secret not in message


def delegated_session(tmp_path, base_url: str, token: str, ehr: str = "1234567") -> LoginSession:
    return LoginSession(
        environment=Environment.LOCAL,
        endpoints=(base_url, base_url),
        config_dir=tmp_path,
        backend=MemoryBackend(),
        delegated=DelegatedCredential(token=token, ehr=ehr),
    )


async def login_once(session: LoginSession) -> str:
    code = await session.request_device_code()
    await session.complete_login(code)
    credential = session.store.load(Environment.LOCAL)
    assert credential is not None
    return credential.token


async def test_01_silent_ok_delegated(tmp_path, caplog, detail_capture) -> None:
    with MockServer(mode="auto", interval=0) as mock:
        token = await login_once(make_session(tmp_path, mock.base_url))
        session = delegated_session(tmp_path, mock.base_url, token)
        detail_capture.clear()  # the preparation step's own detail record
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
            assert await session.check_silent() is not None
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "INFO"
        assert records[0].getMessage() == "已登录（桌面端登录）；env=local"
        assert_clean(records, token, EHR)  # ehr never on the panel-visible channel
        assert len(detail_capture) == 1
        assert detail_capture[0].name == "aixcoding.detail.auth"
        assert detail_capture[0].exc_info is None  # traceback red line (review N1)
        assert detail_capture[0].levelname == "DETAIL"
        assert detail_capture[0].getMessage() == f"已登录（桌面端登录）；ehr={EHR} env=local"


async def test_02_delegated_rejected(tmp_path, caplog) -> None:
    with MockServer(mode="auto") as mock:
        session = delegated_session(tmp_path, mock.base_url, "secret-bogus-delegated")
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
            assert await session.check_silent() is None
        assert session.delegated_credential is None  # rejection stops the shadow
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "ERROR"
        assert records[0].getMessage() == "桌面端传入的登录已失效，改用本机登录；env=local exc=AuthServerError"
        assert_clean(records, "secret-bogus-delegated")


async def test_03_delegated_network_fail(tmp_path, caplog) -> None:
    session = delegated_session(tmp_path, DEAD_URL, "secret-d-token")
    with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
        assert await session.check_silent() is None
    assert session.delegated_credential is not None  # network failure keeps the delegation
    # Platform assumption: a loopback dead port refuses immediately (no firewall
    # on 127.0.0.1), so the httpx cause is ConnectError, not ConnectTimeout.
    records = auth_records(caplog)
    assert len(records) == 1
    assert records[0].levelname == "ERROR"
    assert records[0].getMessage() == (
        "登录检查未完成（网络不通），登录状态保留；env=local exc=AuthNetworkError cause=ConnectError"
    )
    assert_clean(records, "secret-d-token")


async def test_04_no_credential(tmp_path, caplog) -> None:
    with MockServer(mode="auto") as mock:
        session = make_session(tmp_path, mock.base_url)
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
            assert await session.check_silent() is None
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "INFO"
        assert records[0].getMessage() == "未登录（首次使用或登录已过期）；env=local"


async def test_05_silent_ok_stored(tmp_path, caplog, detail_capture) -> None:
    with MockServer(mode="auto", interval=0) as mock:
        session = make_session(tmp_path, mock.base_url)
        token = await login_once(session)
        caplog.clear()
        detail_capture.clear()  # the preparation step's own detail record
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
            assert await session.check_silent() is not None
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "INFO"
        assert records[0].getMessage() == "已登录（本机保存的登录）；env=local"
        assert_clean(records, token, EHR)
        assert len(detail_capture) == 1
        assert detail_capture[0].name == "aixcoding.detail.auth"
        assert detail_capture[0].exc_info is None  # traceback red line (review N1)
        assert detail_capture[0].levelname == "DETAIL"
        assert detail_capture[0].getMessage() == f"已登录（本机保存的登录）；ehr={EHR} env=local"


async def test_06_stored_rejected(tmp_path, caplog) -> None:
    with MockServer(mode="auto") as mock:
        session = make_session(tmp_path, mock.base_url)
        session.store.store(
            Environment.LOCAL,
            StoredCredential.issued_now(environment_id="local", user_id="8769092", token="secret-bogus-stored"),
        )
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
            assert await session.check_silent() is None
        assert session.store.load(Environment.LOCAL) is None  # dead credential cleared
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "ERROR"
        assert records[0].getMessage() == "本机保存的登录已失效，已自动清除，请重新登录；env=local exc=AuthServerError"
        assert_clean(records, "secret-bogus-stored")


async def test_07_stored_network_fail(tmp_path, caplog) -> None:
    session = make_session(tmp_path, DEAD_URL)
    session.store.store(
        Environment.LOCAL,
        StoredCredential.issued_now(environment_id="local", user_id="8769092", token="secret-keep-token"),
    )
    with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
        assert await session.check_silent() is None
    assert session.stored_token == "secret-keep-token"  # offline is not a logout
    # Platform assumption: loopback dead port refuses immediately (see test_03).
    records = auth_records(caplog)
    assert len(records) == 1
    assert records[0].levelname == "ERROR"
    assert records[0].getMessage() == (
        "登录检查未完成（网络不通），登录信息保留；env=local exc=AuthNetworkError cause=ConnectError"
    )
    assert_clean(records, "secret-keep-token")


async def test_08_login_success(tmp_path, caplog, detail_capture) -> None:
    with MockServer(mode="auto", interval=0) as mock:
        session = make_session(tmp_path, mock.base_url)
        code = await session.request_device_code()
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
            account = await session.complete_login(code)
        assert account.ehr == EHR
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "INFO"
        assert records[0].getMessage() == "登录成功；env=local"
        assert_clean(records, EHR)
        assert len(detail_capture) == 1
        assert detail_capture[0].name == "aixcoding.detail.auth"
        assert detail_capture[0].exc_info is None  # traceback red line (review N1)
        assert detail_capture[0].levelname == "DETAIL"
        assert detail_capture[0].getMessage() == f"登录成功；ehr={EHR} env=local"


async def test_11_login_failed_denied(tmp_path, caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/device/code"):
            return httpx.Response(
                200,
                json={
                    "result": {
                        "device_code": "dc-1",
                        "user_code": "ABCD-EFGH",
                        "verification_uri": "http://mock.test/verify",
                        "verification_uri_complete": "http://mock.test/verify?c=ABCD-EFGH",
                        "interval": 1,
                        "expires_in": 600,
                    }
                },
            )
        return httpx.Response(200, json={"result": {"error": "access_denied"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        session = LoginSession(
            environment=Environment.LOCAL,
            endpoints=("http://mock.test/api/v1", "http://mock.test/api/v1"),
            config_dir=tmp_path,
            backend=MemoryBackend(),
            http=http,
        )
        code = await session.request_device_code()
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"), pytest.raises(DevicePollDenied):
            await session.complete_login(code)  # re-raise semantics: the error escapes
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "ERROR"
        assert records[0].getMessage() == "登录失败；env=local exc=DevicePollDenied"
        assert_clean(records)
    finally:
        await http.aclose()


async def test_12_login_cancelled(tmp_path, caplog) -> None:
    with MockServer(mode="manual") as mock:
        session = make_session(tmp_path, mock.base_url)
        code = await session.request_device_code()
        cancel = asyncio.Event()
        cancel.set()
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"), pytest.raises(DevicePollCancelled):
            await session.complete_login(code, cancel_event=cancel)
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "INFO"
        assert records[0].getMessage() == "取消登录；env=local"


def test_09_logout_skipped_delegation(tmp_path, caplog) -> None:
    # No server needed: a delegated logout never touches the network, so any
    # token string works (and no stray "login success" record can appear).
    session = delegated_session(tmp_path, "http://unused.test/api/v1", "any-delegated-token")
    # Seed THIS session's own store: a buggy logout that ignores the active
    # delegation would clear it and turn the assertion below red.
    session.store.store(
        Environment.LOCAL,
        StoredCredential.issued_now(environment_id="local", user_id="8769092", token="secret-seed-token"),
    )
    with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
        session.logout()
    assert session.store.load(Environment.LOCAL) is not None  # delegation logout destroys nothing
    records = auth_records(caplog)
    assert len(records) == 1
    assert records[0].levelname == "INFO"
    assert records[0].getMessage() == "跳过登出（当前使用桌面端登录，由桌面端管理）；env=local"
    assert_clean(records, "any-delegated-token", "secret-seed-token")


async def test_10_logout(tmp_path, caplog) -> None:
    with MockServer(mode="auto", interval=0) as mock:
        session = make_session(tmp_path, mock.base_url)
        await login_once(session)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="aixcoding.auth"):
            session.logout()
        assert session.store.load(Environment.LOCAL) is None
        records = auth_records(caplog)
        assert len(records) == 1
        assert records[0].levelname == "INFO"
        assert records[0].getMessage() == "已登出；env=local"
