# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions._shared import detect_login
from icode_browser_use.config import BrowserConfig, LOGIN_WAIT_MAX
from icode_browser_use.errors import Busy
from icode_browser_use.session import BrowserSession


def session():
    result = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    result._session_id = "page"
    result._main_frame_id = "main"
    result.snapshot.url = "https://example.com/"
    return result


async def commit(s, loader="current", url="https://example.com/", sid="page", frame="main"):
    await s._on_frame_navigated({"frame": {"id": frame, "loaderId": loader, "url": url}}, sid)


async def response(s, status, loader="current", sid="page", frame="main"):
    await s._on_response(
        {
            "type": "Document",
            "frameId": frame,
            "loaderId": loader,
            "response": {"status": status, "url": "https://example.com/"},
        },
        sid,
    )


@pytest.mark.parametrize("status,expected", [(403, False), (401, True), (200, False)])
def test_status_login(status, expected):
    async def run():
        s = session()
        await commit(s)
        await response(s, status)
        assert (detect_login(s, []) is not None) is expected

    asyncio.run(run())


def test_password_evidence_survives_403():
    async def run():
        s = session()
        await commit(s)
        await response(s, 403)
        password = SimpleNamespace(tag="input", input_type="password", visible=True, interactive=True)
        assert detect_login(s, [password]) is not None

    asyncio.run(run())


@pytest.mark.parametrize("response_first", [False, True])
def test_main_response_correlates_with_commit(response_first):
    async def run():
        s = session()
        await commit(s, "old")
        if response_first:
            await response(s, 401)
            assert s.last_main_status is None
            await commit(s)
        else:
            await commit(s)
            await response(s, 401)
        assert s.last_main_status == 401
        await response(s, 200, frame="child")
        await response(s, 200, sid="oopif")
        await response(s, 200, loader="old")
        assert s.last_main_status == 401
        await commit(s, "next")
        assert s.last_main_status is None
        await response(s, 401)
        assert s.last_main_status is None

    asyncio.run(run())


def test_error_page_does_not_trigger_or_release_login():
    async def run():
        s = session()
        await commit(s)
        await response(s, 401)
        s.snapshot.url = "chrome-error://chromewebdata/"
        assert detect_login(s, []) is None
        s.note_login_pending()
        assert detect_login(s, []) is None
        assert s.login_pending

    asyncio.run(run())


def test_foreign_navigation_does_not_replace_main_page():
    async def run():
        s = session()
        await commit(s)
        await response(s, 401)
        await commit(s, "child-loader", "https://other.test/", "oopif", "child")
        await s._on_navigated_within_document({"frameId": "child", "url": "https://other.test/#x"}, "page")
        assert s.snapshot.url == "https://example.com/"
        assert s.last_main_status == 401

    asyncio.run(run())


@pytest.mark.parametrize("action", ["browser_navigate", "browser_back"])
def test_explicit_exit_allowed_and_failure_keeps_protection(action):
    async def run():
        s = session()
        s.note_login_pending()
        with pytest.raises(ValueError):
            async with s.action_lock(action):
                s.clear_login_pending()
                raise ValueError("navigation failed")
        assert s.login_pending
        async with s.action_lock(action):
            s.clear_login_pending()
        assert not s.login_pending

    asyncio.run(run())


def test_queued_write_rechecks_pending_after_lock():
    async def run():
        s = session()
        entered = False

        async def write():
            nonlocal entered
            async with s.action_lock("browser_click"):
                entered = True

        async with s._action_lock:
            task = asyncio.create_task(write())
            await asyncio.sleep(0)
            s.note_login_pending()
        with pytest.raises(Busy):
            await task
        assert not entered

    asyncio.run(run())


def test_timeout_explains_exit_without_authorizing_writes():
    async def run():
        s = session()
        s.note_login_pending()
        s._login_pending_since -= LOGIN_WAIT_MAX + 1
        error = s.login_required(site="example.com", reason="password")
        assert "timed_out" in error.details
        for action in ("browser_navigate", "browser_back", "browser_forward", "browser_close"):
            assert action in error.details
        assert s.login_pending
        async with s.action_lock("browser_snapshot"):
            pass
        async with s.action_lock("browser_close"):
            pass
        with pytest.raises(Busy):
            async with s.action_lock("browser_type"):
                pass

    asyncio.run(run())


@pytest.mark.parametrize(
    ("action", "allowed"),
    [
        ("browser_logs", True),
        ("browser_forward", True),
        ("browser_tab_close", False),
        ("browser_reload", False),
    ],
)
def test_new_tools_respect_the_login_wait(action, allowed):
    async def run():
        s = session()
        s.note_login_pending()
        if allowed:
            async with s.action_lock(action):
                pass
        else:
            with pytest.raises(Busy):
                async with s.action_lock(action):
                    pass

    asyncio.run(run())


@pytest.mark.parametrize(
    ("action", "allowed"),
    [
        ("browser_logs", True),
        ("browser_forward", False),
        ("browser_reload", False),
        ("browser_tab_close", False),
    ],
)
def test_new_tools_respect_an_open_dialog(action, allowed):
    from icode_browser_use.dialog import DialogInfo
    from icode_browser_use.errors import DialogPending

    async def run():
        s = session()
        s._dialog = DialogInfo(seq=1, kind="alert", message="hi")
        if allowed:
            async with s.action_lock(action):
                pass
        else:
            with pytest.raises(DialogPending):
                async with s.action_lock(action):
                    pass

    asyncio.run(run())
