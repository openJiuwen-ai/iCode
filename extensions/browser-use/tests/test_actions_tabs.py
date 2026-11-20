# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for ``browser_tabs`` and ``browser_tab_switch`` (W09 tabs step)."""

from __future__ import annotations

import asyncio

import pytest

from icode_browser_use.actions import snapshot, tab_switch, tabs
from icode_browser_use.errors import TargetNotFound
from icode_browser_use.session import PageInfo
from icode_browser_use.types import text_block


class FakeSession:
    def __init__(self, pages: list[PageInfo], *, error: BaseException | None = None) -> None:
        self._pages = pages
        self._error = error
        self.switched: list[str] = []

    async def list_pages(self) -> list[PageInfo]:
        return list(self._pages)

    async def switch_page(self, tab_id: str) -> PageInfo:
        self.switched.append(tab_id)
        if self._error is not None:
            raise self._error
        return next(info for info in self._pages if info.target_id == tab_id)


PAGES = [
    PageInfo(index=1, target_id="t-root", url="https://example.com/", title="Root", opener_id=None, current=True),
    PageInfo(
        index=2,
        target_id="t-popup",
        url="https://provider.example/auth",
        title="Sign in",
        opener_id="t-root",
        current=False,
    ),
]


def test_tabs_lists_every_owned_page_with_current_and_opener() -> None:
    session = FakeSession(PAGES)
    text = asyncio.run(tabs.run(session, {}))[0]["text"]
    assert "标签页 2 个（当前 #1）" in text
    assert "#1 * tab_id=t-root opener=- url=https://example.com/ title=Root" in text
    assert "#2 - tab_id=t-popup opener=t-root url=https://provider.example/auth title=Sign in" in text


def test_tabs_without_pages_points_at_starting_the_browser() -> None:
    text = asyncio.run(tabs.run(FakeSession([]), {}))[0]["text"]
    assert "标签页：0" in text
    assert "browser_navigate" in text


def test_tabs_hides_query_strings_in_urls() -> None:
    pages = [
        PageInfo(
            index=1,
            target_id="t-root",
            url="https://example.com/callback?code=secret&state=xyz#frag",
            title="Root",
            opener_id=None,
            current=True,
        )
    ]
    text = asyncio.run(tabs.run(FakeSession(pages), {}))[0]["text"]
    assert "https://example.com/callback" in text
    assert "code=secret" not in text and "state=xyz" not in text and "#frag" not in text


def test_tab_switch_switches_then_returns_a_snapshot(monkeypatch) -> None:
    session = FakeSession(PAGES)
    monkeypatch.setattr(snapshot, "run", lambda _session, _arguments: _snapshot())

    text = asyncio.run(tab_switch.run(session, {"tab_id": "t-popup"}))[0]["text"]

    assert session.switched == ["t-popup"]
    assert text.startswith("已切换到 #2 https://provider.example/auth")
    assert "SNAPSHOT" in text


def test_tab_switch_reports_an_unknown_tab() -> None:
    session = FakeSession(PAGES, error=TargetNotFound("未知标签页：t-missing（可用：t-root；请先 browser_tabs）"))
    with pytest.raises(TargetNotFound):
        asyncio.run(tab_switch.run(session, {"tab_id": "t-missing"}))
    assert session.switched == ["t-missing"]


async def _snapshot() -> list[dict]:
    return [text_block("SNAPSHOT")]


def test_tabs_displays_background_and_foreground_markers() -> None:
    pages = [
        PageInfo(
            index=1,
            target_id="t-bg",
            url="https://www.bilibili.com/video/BV1",
            title="Video",
            opener_id=None,
            current=True,
            active=False,
        ),
        PageInfo(
            index=2,
            target_id="t-fg",
            url="https://github.com/u",
            title="GH",
            opener_id="t-bg",
            current=False,
            active=True,
        ),
    ]
    text = asyncio.run(tabs.run(FakeSession(pages), {}))[0]["text"]
    assert "tab_id=t-bg" in text and "[background]" in text
    assert "tab_id=t-fg" in text and "[foreground]" in text


def test_screenshot_timeout_on_background_tab_explains_throttling() -> None:
    from types import SimpleNamespace

    from icode_browser_use.actions import screenshot
    from icode_browser_use.errors import CDPTimeout, Unavailable

    class BgClient:
        async def send(self, method, params=None, session_id=None, timeout=None):
            raise CDPTimeout(method, 1.0)

    class BgSession:
        session_id = "s1"
        target_id = "t1"
        cfg = SimpleNamespace(cdp_timeout=1.0)

        def require_client(self):
            return BgClient()

        async def current_page_foreground(self):
            return False

    with pytest.raises(Unavailable) as exc_info:
        asyncio.run(screenshot.run(BgSession(), {"full_page": False, "target": "", "annotate": False}))
    assert "background_tab_throttled" in str(exc_info.value)
    assert "后台" in str(exc_info.value)
