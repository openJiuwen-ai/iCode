# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Routing-fix tests: loggers moved onto the aixcoding subtree (batch ①).

Design: aixcoding/docs/batches/logging-routing-fixes/design.md (signed off
2026-11-05). Three records previously sat on ``getLogger(__name__)`` loggers
outside the ``aixcoding`` subtree — two fully silenced by root's INFO
threshold, one leaking English text into the upstream debug log. Every case
asserts the target volume, the frozen Chinese message, and a spy handler on
the former ``__name__`` logger proving zero dispatch there (the defect's
direct regression anchor). Real logger-tree dispatch only; logging itself is
never mocked.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chrys.kernel.aixcoding.auth import crypto, login_token_header_value
from chrys.kernel.aixcoding.auth.crypto import MemoryBackend, get_backend
from chrys.kernel.aixcoding.diagnostics.logging_support import DETAIL_LEVEL

A1_MESSAGE = "读取登录凭证失败，token 请求头未附加。"
A2_WARNING = "系统凭据库不可用，登录凭证将仅保存在内存中，重启后需重新登录。"


@pytest.fixture(autouse=True)
def _aix_logger_isolation() -> Iterator[None]:
    """Pin the ``aixcoding`` logger tree for these tests.

    caplog's handler sits on the root logger, so public-channel records must
    propagate out of the ``aixcoding`` subtree to be captured; other test
    modules may have left ``propagate=False`` (or a file sink) behind.
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
    detail.propagate = False  # mirror the production preset
    detail.addHandler(capture)
    yield records
    detail.handlers[:] = saved[0]
    detail.propagate, detail.level = saved[1], saved[2]


@pytest.fixture
def subtree_spies() -> Iterator[dict[str, list[logging.LogRecord]]]:
    """Spy handlers on the former ``__name__`` loggers — zero dispatch there."""
    names = (
        "chrys.kernel.aixcoding.auth",  # A1's former logger
        "chrys.kernel.aixcoding.auth.crypto",  # A2's former logger
        "chrys.app.aixcoding.tui.login",  # A3's former logger
    )

    class _Spy(logging.Handler):
        def __init__(self, buf: list[logging.LogRecord]) -> None:
            super().__init__()
            self._buf = buf

        def emit(self, record: logging.LogRecord) -> None:
            self._buf.append(record)

    spies: dict[str, list[logging.LogRecord]] = {}
    handlers: list[tuple[logging.Logger, logging.Handler]] = []
    saved_levels: list[tuple[logging.Logger, int]] = []
    for name in names:
        logger = logging.getLogger(name)
        records: list[logging.LogRecord] = []
        spies[name] = records
        # Lower the spied logger's own level (peer-review blocker): records are
        # gated by isEnabledFor before any handler sees them — without this, a
        # regressed DEBUG record on the old logger would never be emitted and
        # the zero-dispatch assertion would pass vacuously (the very mechanism
        # that used to silence these records).
        saved_levels.append((logger, logger.level))
        logger.setLevel(DETAIL_LEVEL)
        spy = _Spy(records)
        spy.setLevel(logging.NOTSET)
        logger.addHandler(spy)
        handlers.append((logger, spy))
    yield spies
    for logger, spy in handlers:
        logger.removeHandler(spy)
    for logger, level in saved_levels:
        logger.setLevel(level)


class _Exploding:
    def __init__(self) -> None:
        raise OSError("headless session")


class _Garbage:
    degraded = False

    def protect(self, plaintext: bytes, label: str = crypto.LABEL) -> bytes:
        return b"corrupted"

    def unprotect(self, ciphertext: bytes, label: str = crypto.LABEL) -> bytes:
        return b"not-the-original"


def _with_factories(monkeypatch: pytest.MonkeyPatch, factories: list) -> None:
    monkeypatch.setattr(crypto, "_platform_factories", lambda: factories)


# ── A1: login_token_header_value ──────────────────────────────────────────


def test_a1_failure_lands_on_detail_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
    subtree_spies: dict[str, list[logging.LogRecord]],
) -> None:
    import chrys.kernel.aixcoding.auth as auth_module

    monkeypatch.setattr(auth_module, "_token_header_failures_logged", set())  # order-proof
    monkeypatch.setattr(
        "chrys.kernel.aixcoding.auth.get_login_session",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert login_token_header_value() == ""
    assert len(detail_capture) == 1
    record = detail_capture[0]
    assert record.name == "aixcoding.detail.auth"
    assert record.levelno == DETAIL_LEVEL
    assert record.getMessage() == A1_MESSAGE
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    # caplog may surface detail records in this pytest setup; the public-channel
    # assertion filters by logger name, the established auth_records precedent.
    assert [r for r in caplog.records if r.name == "aixcoding.auth"] == [], (
        "per-request failures must stay off the public channels"
    )
    assert subtree_spies["chrys.kernel.aixcoding.auth"] == []


def test_a1_persistent_failure_logs_once_per_exception_type(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """PR #95 review C1: a persistent failure must not flood the detail volume.

    Same-type recurrences stay silent (identical stack, no diagnostic loss);
    a new exception type is a new failure and gets its own record.
    """
    import chrys.kernel.aixcoding.auth as auth_module

    monkeypatch.setattr(auth_module, "_token_header_failures_logged", set())
    calls = iter([RuntimeError("boom"), RuntimeError("boom again"), OSError("different failure")])

    def _failing_session():
        raise next(calls)

    monkeypatch.setattr("chrys.kernel.aixcoding.auth.get_login_session", _failing_session)
    for _ in range(3):
        assert login_token_header_value() == ""
    assert len(detail_capture) == 2, "one record per exception type, not per request"
    assert detail_capture[0].exc_info[0] is RuntimeError
    assert detail_capture[1].exc_info[0] is OSError
    assert [r for r in caplog.records if r.name == "aixcoding.auth"] == []


def test_a1_anonymous_without_error_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """A healthy not-logged-in state returns an empty header with no record.

    Pins the message semantics: ``stored_token`` returning ``None`` is the
    normal anonymous flow (peer review m5) and must not enter the except path.
    """
    monkeypatch.setattr(
        "chrys.kernel.aixcoding.auth.get_login_session",
        lambda: SimpleNamespace(stored_token=None),
    )
    assert login_token_header_value() == ""
    assert detail_capture == []
    assert [r for r in caplog.records if r.name == "aixcoding.auth"] == []


# ── A2: crypto get_backend ────────────────────────────────────────────────


def test_a2_exploding_exhausts_candidates_full_double_write(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
    subtree_spies: dict[str, list[logging.LogRecord]],
) -> None:
    _with_factories(monkeypatch, [_Exploding])
    assert isinstance(get_backend(), MemoryBackend)

    detail_texts = [r.getMessage() for r in detail_capture]
    assert any("加密后端 _Exploding 探测失败" in text and "headless session" in text for text in detail_texts)
    assert A2_WARNING in detail_texts, "the public WARNING must be double-written to detail"
    # PR #95 review C7: pin crypto.py's local _DETAIL_LEVEL copy through an
    # actual levelno assertion (the existing pins cover only session.py's copy).
    warning_detail = [r for r in detail_capture if r.getMessage() == A2_WARNING]
    assert warning_detail and warning_detail[0].levelno == DETAIL_LEVEL

    public = [r for r in caplog.records if r.name == "aixcoding.auth"]
    assert len(public) == 1
    assert public[0].levelno == logging.WARNING
    assert public[0].getMessage() == A2_WARNING
    assert "_Exploding" not in public[0].getMessage(), "sentinel: backend name must stay off the public volume"
    assert "headless session" not in public[0].getMessage(), "sentinel: exception text likewise"
    assert subtree_spies["chrys.kernel.aixcoding.auth.crypto"] == []


def test_a2_garbage_roundtrip_mismatch_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """The non-raising mismatch path (peer review M1) gets its own detail record."""
    _with_factories(monkeypatch, [_Garbage])
    assert isinstance(get_backend(), MemoryBackend)

    detail_texts = [r.getMessage() for r in detail_capture]
    assert any("加密后端 _Garbage 往返校验不一致" in text for text in detail_texts)
    assert A2_WARNING in detail_texts
    public = [r for r in caplog.records if r.name == "aixcoding.auth"]
    assert [r.getMessage() for r in public] == [A2_WARNING]


def test_a2_healthy_candidate_short_circuits_without_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    """factory failure ≠ degradation: a later healthy candidate wins silently."""
    _with_factories(monkeypatch, [_Exploding, MemoryBackend])
    assert isinstance(get_backend(), MemoryBackend)  # selected via the probe return
    detail_texts = [r.getMessage() for r in detail_capture]
    assert any("加密后端 _Exploding 探测失败" in text for text in detail_texts)
    assert A2_WARNING not in detail_texts, "a healthy candidate's early return must not warn on either volume"
    public = [r for r in caplog.records if r.name == "aixcoding.auth"]
    assert public == [], "a healthy candidate's early return must not warn"


def test_a2_healthy_probe_is_silent(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    _with_factories(monkeypatch, [MemoryBackend])
    assert isinstance(get_backend(), MemoryBackend)
    assert detail_capture == []
    assert [r for r in caplog.records if r.name == "aixcoding.auth"] == []


# ── A3: login success callback failure ────────────────────────────────────


def test_a3_callback_failure_lands_on_detail_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
    subtree_spies: dict[str, list[logging.LogRecord]],
) -> None:
    from chrys.app.aixcoding.tui.login import LoginDialog

    code = SimpleNamespace(user_code="123-456", verification_uri="https://example", verification_uri_complete="")
    account = SimpleNamespace(ehr="1234567", name="测试用户")

    session = SimpleNamespace(
        request_device_code=lambda: _async_value(code),
        complete_login=lambda device_code, cancel_event: _async_value(account),
    )
    dismissed: list[object] = []
    dialog = SimpleNamespace(
        _session=session,
        _cancel_event=None,
        _on_login_success=Mock(side_effect=RuntimeError("callback boom")),
        _show_code=lambda device_code: None,
        _show_failure=lambda message: None,
        dismiss_requested=False,
        dismiss_when_topmost=dismissed.append,
    )

    asyncio.run(LoginDialog._run_login(dialog))  # type: ignore[arg-type]

    assert dismissed == [account], "the dialog still dismisses on callback failure"
    assert len(detail_capture) == 1
    record = detail_capture[0]
    assert record.name == "aixcoding.detail.auth"
    assert record.getMessage() == "登录成功回调失败，继续关闭登录对话框。"
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert [r for r in caplog.records if r.name == "aixcoding.auth"] == []
    assert subtree_spies["chrys.app.aixcoding.tui.login"] == []


def _async_value(value: object):
    return asyncio.sleep(0, result=value)
