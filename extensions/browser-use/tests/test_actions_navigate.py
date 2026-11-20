# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from browser_use.actions import navigate
from browser_use.errors import BadRequest, Blocked, Unavailable


def make_session(base="https://example.com/dir/page?old=1"):
    client = SimpleNamespace(send=AsyncMock(return_value={"loaderId": "loader"}))
    return SimpleNamespace(
        cfg=SimpleNamespace(allowed_domains=("example.com",), allowed_origins=(), nav_load_timeout=1.0),
        snapshot=SimpleNamespace(url=base),
        session_id="main",
        target_id="target",
        require_client=Mock(return_value=client),
        wait_for_load=AsyncMock(),
        wait_for_navigation=AsyncMock(),
        nav_seq=0,
        note_success=Mock(),
    )


@pytest.fixture(autouse=True)
def snapshot_stubs(monkeypatch):
    monkeypatch.setattr(navigate, "_page_title", AsyncMock(return_value="Title"))
    monkeypatch.setattr(navigate, "capture_and_register", AsyncMock(return_value=([], None)))
    monkeypatch.setattr(navigate, "detect_login", lambda *_: None)
    monkeypatch.setattr(navigate, "render_snapshot", lambda *a, **kw: kw["url"])
    monkeypatch.setattr(navigate, "stats_line", lambda *_: "stats")
    monkeypatch.setattr(navigate, "login_hint", lambda *_: "")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/next", "https://example.com/next"),
        ("../next", "https://example.com/next"),
        ("child", "https://example.com/dir/child"),
        ("?q=2", "https://example.com/dir/page?q=2"),
        ("#section", "https://example.com/dir/page?old=1#section"),
        ("//sub.example.com/next", "https://sub.example.com/next"),
        ("  https://example.com/next  ", "https://example.com/next"),
    ],
)
def test_resolves_url_before_single_navigation(url, expected, snapshot_stubs):
    session = make_session()
    asyncio.run(navigate.run(session, {"url": url}))
    session.require_client().send.assert_awaited_once_with(
        "Page.navigate",
        {"url": expected},
        session_id="main",
        timeout=1.0,
    )
    session.wait_for_load.assert_awaited_once_with("loader", timeout=1.0)


@pytest.mark.parametrize(
    "base", ["", "about:blank", "file:///tmp/page", "https:///missing", "https://example.com:bad/page"]
)
def test_relative_url_requires_valid_http_base(base):
    session = make_session(base)
    with pytest.raises(BadRequest):
        asyncio.run(navigate.run(session, {"url": "/next"}))
    session.require_client.assert_not_called()


@pytest.mark.parametrize(
    ("base", "url"),
    [
        ("https://outside.test/page", "/next"),
        ("https://example.com/page", "//outside.test/next"),
        ("", "https://outside.test/next"),
    ],
)
def test_resolved_host_must_be_allowlisted(base, url):
    session = make_session(base)
    with pytest.raises(Blocked):
        asyncio.run(navigate.run(session, {"url": url}))
    session.require_client.assert_not_called()


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/plain,hi", "file:///tmp/page", "about:blank"])
def test_non_http_scheme_is_blocked(url):
    session = make_session()
    with pytest.raises(Blocked):
        asyncio.run(navigate.run(session, {"url": url}))
    session.require_client.assert_not_called()


@pytest.mark.parametrize(
    "url",
    ["", " ", "https:///missing", "https://example.com:bad", "https://[bad", "https://example.com\\@outside.test/"],
)
def test_invalid_url_is_rejected_before_navigation(url):
    session = make_session()
    with pytest.raises(BadRequest):
        asyncio.run(navigate.run(session, {"url": url}))
    session.require_client.assert_not_called()


def test_absolute_url_does_not_require_base(snapshot_stubs):
    session = make_session("")
    blocks = asyncio.run(navigate.run(session, {"url": "https://example.com/next"}))
    assert "https://example.com/next" in blocks[0]["text"]


@pytest.mark.parametrize("failure", ["send", "response", "load", "soft"])
def test_navigation_failures_are_never_retried(failure):
    session = make_session()
    client = session.require_client()
    if failure == "send":
        client.send.side_effect = TimeoutError("send timed out")
    elif failure == "response":
        client.send.return_value = {"errorText": "navigation failed"}
    elif failure == "soft":
        client.send.return_value = {}
        session.wait_for_navigation.side_effect = TimeoutError("soft navigation timed out")
    else:
        session.wait_for_load.side_effect = TimeoutError("load timed out")
    with pytest.raises((TimeoutError, Unavailable)):
        asyncio.run(navigate.run(session, {"url": "https://example.com/next"}))
    assert client.send.await_count == 1
