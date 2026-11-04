# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LoginDialog tests: success flow, cancel stops without storing, failure UI."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

import pytest
from aixcoding.mock.auth.server import MockAuthConfig, create_server
from aixcoding.tests.tui.conftest import wait_until
from textual.app import App
from textual.widgets import Static

from chrys.app.aixcoding.tui import LoginDialog
from chrys.kernel.aixcoding.auth import AccountInfo, Environment, LoginSession
from chrys.kernel.aixcoding.auth.crypto import MemoryBackend
from chrys.kernel.aixcoding.diagnostics.logging_support import DETAIL_LEVEL


@pytest.fixture
def detail_capture() -> Iterator[list[logging.LogRecord]]:
    """Capture the file-only auth detail subtree, which caplog cannot see."""
    detail = logging.getLogger("aixcoding.detail")
    saved = (detail.handlers[:], detail.propagate, detail.level)
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = _Capture()
    capture.setLevel(logging.NOTSET)
    logging.addLevelName(DETAIL_LEVEL, "DETAIL")
    detail.setLevel(DETAIL_LEVEL)
    detail.propagate = False
    detail.addHandler(capture)
    yield records
    detail.handlers[:] = saved[0]
    detail.propagate, detail.level = saved[1], saved[2]


class _DialogApp(App):
    pass


class MockServer:
    """Loopback mock on an ephemeral port, usable as a context manager."""

    def __init__(self, **config: object) -> None:
        self.server = create_server(MockAuthConfig(**config), host="127.0.0.1", port=0)  # type: ignore[arg-type]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"

    def __enter__(self) -> MockServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def make_session(tmp_path, base_url: str) -> LoginSession:
    return LoginSession(
        environment=Environment.LOCAL,
        endpoints=(base_url, base_url),
        config_dir=tmp_path,
        backend=MemoryBackend(),
    )


async def test_login_dialog_success_flow(tmp_path) -> None:
    with MockServer(mode="auto", interval=0) as mock:
        session = make_session(tmp_path, mock.base_url)
        opened: list[str] = []
        app = _DialogApp()
        async with app.run_test() as pilot:
            results: list[object] = []
            dialog = LoginDialog(session=session, open_browser=opened.append)
            app.push_screen(dialog, results.append)

            done = await wait_until(lambda: bool(results), pilot=pilot, timeout=10)
            assert done, "dialog should dismiss with the account after the mock grants"

            account = results[0]
            assert isinstance(account, AccountInfo)
            assert account.display_name == "测试员"  # the mock's fixture user (PR #56)
            # The browser was pointed at the pre-filled verification URL.
            assert opened and "user_code=" in opened[0]
            # The credential is stored for the next silent check.
            assert session.stored_token is not None
            assert session.stored_token.startswith("mock-token-")


async def test_login_dialog_cancel_stops_without_storing(tmp_path) -> None:
    # interval=1: a real (short) poll sleep, so the wait loop does not spin.
    with MockServer(mode="manual", interval=1) as mock:  # never confirms on its own
        session = make_session(tmp_path, mock.base_url)
        app = _DialogApp()
        async with app.run_test() as pilot:
            results: list[object] = []
            dialog = LoginDialog(session=session, open_browser=lambda url: None)
            app.push_screen(dialog, results.append)
            await pilot.pause()  # let the screen compose before querying widgets

            # Wait until the user code is on screen, then cancel. Content (not
            # display) is the signal: visibility is class-driven via the tcss.
            assert await wait_until(lambda: bool(dialog.children), pilot=pilot, timeout=10), (
                "dialog compose children should mount"
            )
            code_view = dialog.query_one("#login-code", Static)
            shown = await wait_until(lambda: bool(str(code_view.content).strip()), pilot=pilot, timeout=10)
            assert shown, "user code should appear once the device code arrives"

            await pilot.press("escape")
            assert await wait_until(lambda: results == [None], pilot=pilot, timeout=5), (
                "cancelling should dismiss the dialog with None"
            )
            assert session.stored_token is None


async def test_login_dialog_shows_error_when_unreachable(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    session = make_session(tmp_path, "http://127.0.0.1:1/api/v1")  # nothing listens here
    app = _DialogApp()
    async with app.run_test() as pilot:
        results: list[object] = []
        dialog = LoginDialog(session=session, open_browser=lambda url: None)
        app.push_screen(dialog, results.append)
        await pilot.pause()  # let the screen compose before querying widgets

        status = dialog.query_one("#login-status", Static)

        def failed() -> bool:
            return "无法获取登录码" in str(status.content)

        assert await wait_until(failed, pilot=pilot, timeout=10), (
            "unreachable server should surface as an error message"
        )
        shown = str(status.content)
        assert shown == "无法获取登录码，请稍后重试。"
        assert all(term not in shown for term in ("127.0.0.1", "AuthNetworkError", "All connection"))
        failure = next(r for r in detail_capture if r.getMessage().startswith("获取登录码失败："))
        assert failure.exc_info is None
        assert all(term in failure.getMessage() for term in ("AuthNetworkError", "All connection"))
        main = [r for r in caplog.records if r.name == "aixcoding.auth"]
        assert len(main) == 1
        assert main[0].levelno == logging.ERROR
        assert main[0].getMessage() == "登录失败：无法获取登录码，请稍后重试。"
        assert "AuthNetworkError" not in main[0].getMessage()

        await pilot.press("escape")
        await pilot.pause()
        assert results == [None]


async def test_login_dialog_poll_denial_keeps_details_off_the_ui(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
    detail_capture: list[logging.LogRecord],
) -> None:
    with MockServer(mode="manual", interval=0) as mock:
        session = make_session(tmp_path, mock.base_url)
        app = _DialogApp()
        async with app.run_test() as pilot:
            results: list[object] = []
            dialog = LoginDialog(session=session, open_browser=lambda url: None)
            app.push_screen(dialog, results.append)
            await pilot.pause()
            code_view = dialog.query_one("#login-code", Static)
            assert await wait_until(lambda: bool(str(code_view.content).strip()), pilot=pilot, timeout=10)

            mock.server.mock_state.confirm(str(code_view.content), "deny")
            status = dialog.query_one("#login-status", Static)
            assert await wait_until(lambda: str(status.content) == "登录未完成，请稍后重试。", pilot=pilot, timeout=10)
            shown = str(status.content)
            assert all(term not in shown for term in ("access_denied", "DevicePollDenied", "device authorization"))
            failure = next(r for r in detail_capture if r.getMessage().startswith("授权轮询失败："))
            assert failure.exc_info is None
            assert all(term in failure.getMessage() for term in ("DevicePollDenied", "access_denied"))
            assert not [r for r in caplog.records if r.name == "chrys.app.aixcoding.tui.login"]
            main = [r for r in caplog.records if r.name == "aixcoding.auth"]
            assert len(main) == 1
            assert main[0].levelno == logging.ERROR
            assert main[0].getMessage() == "登录失败：登录未完成，请稍后重试；env=local"
            assert "DevicePollDenied" not in main[0].getMessage()

            await pilot.press("escape")
            await pilot.pause()
            assert results == [None]
