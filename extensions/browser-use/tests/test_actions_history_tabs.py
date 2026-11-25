# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from icode_browser_use.actions import back, forward, reload, tab_close
from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import BadRequest, CDPTimeout, TargetNotFound, Unavailable
from icode_browser_use.session import BrowserSession


def _history_session(current: int, count: int = 3) -> SimpleNamespace:
    client = SimpleNamespace(
        send=AsyncMock(
            return_value={
                "currentIndex": current,
                "entries": [{"id": 100 + index, "url": f"https://example.com/{index}"} for index in range(count)],
            }
        )
    )
    return SimpleNamespace(
        require_client=lambda: client,
        session_id="page",
        cfg=SimpleNamespace(cdp_timeout=1, nav_load_timeout=1),
    )


@pytest.mark.parametrize(
    ("module", "current", "expected"),
    [(back, 1, 100), (back, 2, 101), (forward, 0, 101), (forward, 1, 102)],
)
def test_history_steps_to_the_neighbouring_entry(monkeypatch, module, current, expected) -> None:
    calls = []

    async def fake_follow(_session, method, params, **kwargs):
        calls.append((method, params, kwargs))
        return [{"type": "text", "text": "snapshot"}]

    monkeypatch.setattr(back, "follow_navigation", fake_follow)
    blocks = asyncio.run(module.run(_history_session(current), {}))
    assert blocks[0]["text"] == "snapshot"
    assert calls == [
        (
            "Page.navigateToHistoryEntry",
            {"entryId": expected},
            {"expected_url": f"https://example.com/{expected - 100}"},
        )
    ]


@pytest.mark.parametrize(("module", "current", "notice"), [(back, 0, "历史起点"), (forward, 2, "历史末尾")])
def test_history_edges_are_a_notice_not_a_navigation(monkeypatch, module, current, notice) -> None:
    follow = AsyncMock()
    monkeypatch.setattr(back, "follow_navigation", follow)
    blocks = asyncio.run(module.run(_history_session(current), {}))
    assert notice in blocks[0]["text"]
    follow.assert_not_called()


def test_reload_passes_ignore_cache(monkeypatch) -> None:
    follow = AsyncMock(return_value=[{"type": "text", "text": "snapshot"}])
    monkeypatch.setattr(reload, "follow_navigation", follow)
    asyncio.run(reload.run(SimpleNamespace(), {"ignore_cache": True}))
    follow.assert_awaited_once()
    assert follow.await_args.args[1:] == ("Page.reload", {"ignoreCache": True})
    assert follow.await_args.kwargs == {"cross_document": True}, "a reload is only satisfied by a commit"


def _tab_session(close_error: Exception | None = None) -> BrowserSession:
    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    session._state = "ready"
    session._record_page("root", "s-root", opener_id=None, url="https://example.com/")
    session._record_page("popup", "s-popup", opener_id="root", url="https://example.com/p")
    session._target_id, session._session_id = "popup", "s-popup"
    session.closed_targets = []

    async def send(method, params=None, *, session_id=None, timeout=None):
        if method == "Target.closeTarget":
            if close_error is not None:
                raise close_error
            session.closed_targets.append(params["targetId"])
            return {"success": True}
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": target} for target in session._pages]}
        return {}

    session._client = SimpleNamespace(send=send, closed=False)

    async def activate(page):
        session._target_id, session._session_id = page.target_id, page.session_id

    session._activate_page = activate
    return session


def test_closing_the_current_tab_falls_back_to_its_opener() -> None:
    session = _tab_session()
    text = asyncio.run(tab_close.run(session, {"tab_id": "popup"}))[0]["text"]
    assert session.target_id == "root"
    assert [info.target_id for info in session.pages()] == ["root"]
    assert session.closed_targets == ["popup"]
    assert "已关闭标签页 popup" in text and "旧 ref 全部失效" in text
    assert "tab_id=root" in text


def test_closing_a_background_tab_keeps_the_current_one() -> None:
    session = _tab_session()
    session._target_id, session._session_id = "root", "s-root"
    text = asyncio.run(tab_close.run(session, {"tab_id": "popup"}))[0]["text"]
    assert session.target_id == "root"
    assert "旧 ref" not in text
    assert session.closed_targets == ["popup"]


def test_last_or_unknown_tab_is_refused_without_closing() -> None:
    session = _tab_session()
    with pytest.raises(TargetNotFound):
        asyncio.run(session.close_page("someone-else"))
    asyncio.run(session.close_page("popup"))
    with pytest.raises(BadRequest):
        asyncio.run(session.close_page("root"))
    assert session.closed_targets == ["popup"]


def test_a_failed_close_is_reported_and_the_tab_stays_tracked() -> None:
    session = _tab_session(close_error=CDPTimeout("Target.closeTarget", 1))
    with pytest.raises(Unavailable):
        asyncio.run(tab_close.run(session, {"tab_id": "popup"}))
    assert [info.target_id for info in session.pages()] == ["root", "popup"]
    assert session.target_id == "popup"


def test_close_timeouts_never_recycle_the_current_page(monkeypatch) -> None:
    from icode_browser_use import runtime
    from icode_browser_use.actions import build_tool_defs

    session = _tab_session(close_error=CDPTimeout("Target.closeTarget", 1))
    session._target_id, session._session_id = "root", "s-root"
    recycled = []

    async def recycle_page():
        recycled.append(session.target_id)

    async def started():
        return None

    session.recycle_page = recycle_page
    session.ensure_started = started
    tool = next(tool for tool in build_tool_defs() if tool.name == "browser_tab_close")
    for _ in range(3):
        with pytest.raises(Unavailable):
            asyncio.run(runtime.call_tool(session, tool, {"tab_id": "popup"}))
    assert recycled == []
    assert [info.target_id for info in session.pages()] == ["root", "popup"]
