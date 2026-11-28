# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import time

from icode_browser_use.actions import back
from icode_browser_use.config import BrowserConfig
from icode_browser_use.session import BrowserSession


class _NavClient:
    """Answers a navigation command, then commits it after a delay (optionally another tab first)."""

    def __init__(self, session: BrowserSession, *, commits: list[tuple[float, str, str]]) -> None:
        self.session = session
        self.commits = commits  # (delay, cdp session, loader id), in order
        self.handlers: dict[str, list] = {}
        self.closed = False

    def on(self, method, handler):
        self.handlers.setdefault(method, []).append(handler)

    async def _emit(self, method, params, session_id):
        for handler in list(self.handlers.get(method, ())):
            result = handler(params, session_id)
            if result is not None:
                await result

    async def _commit(self):
        for delay, page_session, loader in self.commits:
            if delay:  # 0 = same reader batch: handled before any waiter resumes
                await asyncio.sleep(delay)
            frame_id = "main" if page_session == "page" else "other"
            if loader in {"beforeunload", "alert"}:  # beforeunload holds the navigation; alert comes from the new page
                await self._emit(
                    "Page.javascriptDialogOpening",
                    {"type": loader, "message": "", "url": "https://example.com/"},
                    page_session,
                )
            elif loader == "soft":  # the page's own pushState/replaceState on the main frame
                await self._emit(
                    "Page.navigatedWithinDocument", {"frameId": frame_id, "url": "https://example.com/#s"}, page_session
                )
            elif loader.startswith("load:"):
                await self._emit("Page.lifecycleEvent", {"name": "load", "loaderId": loader[5:]}, page_session)
            elif loader.startswith("commit:"):
                frame = {"id": frame_id, "loaderId": loader[7:], "url": "https://example.com/"}
                await self._emit("Page.frameNavigated", {"frame": frame}, page_session)
            else:
                frame = {"id": frame_id, "loaderId": loader, "url": "https://example.com/"}
                await self._emit("Page.frameNavigated", {"frame": frame}, page_session)
                await self._emit("Page.lifecycleEvent", {"name": "load", "loaderId": loader}, page_session)

    async def send(self, method, params=None, *, session_id=None, timeout=None):
        if method in {"Page.reload", "Page.navigateToHistoryEntry"}:
            asyncio.get_running_loop().create_task(self._commit())
        return {}


def _session(commits: list[tuple[float, str, str]]) -> tuple[BrowserSession, _NavClient]:
    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",), nav_load_timeout=3.0))
    session._session_id, session._main_frame_id = "page", "main"
    client = _NavClient(session, commits=commits)
    session._client = client
    session._register_handlers()
    return session, client


def _follow(session: BrowserSession, monkeypatch, method: str = "Page.reload") -> float:
    async def fake_capture(_session, **_kwargs):
        return [], None

    async def fake_title(_session):
        return ""

    monkeypatch.setattr(back, "capture_and_register", fake_capture)
    monkeypatch.setattr(back, "_page_title", fake_title)
    monkeypatch.setattr(back, "detect_login", lambda *_args: None)
    started = time.monotonic()
    asyncio.run(back.follow_navigation(session, method, {}, cross_document=method == "Page.reload"))
    return time.monotonic() - started


def test_a_slow_commit_is_followed_to_its_load_not_the_full_timeout(monkeypatch) -> None:
    # The commit lands after the old 2 s loader grace: the wait must still end at the load.
    session, _client = _session([(2.3, "page", "L2")])
    assert _follow(session, monkeypatch) < 2.9


def test_a_background_tab_navigation_is_not_taken_for_ours(monkeypatch) -> None:
    # Another owned tab commits first; taking its loader would wait out the full timeout.
    session, _client = _session([(0.05, "background-tab", "LB"), (0.2, "page", "L2")])
    assert _follow(session, monkeypatch) < 1.0


def test_following_navigations_registers_no_per_call_listener(monkeypatch) -> None:
    session, client = _session([(0.01, "page", "L2")])
    before = {method: len(handlers) for method, handlers in client.handlers.items()}
    for _ in range(3):
        _follow(session, monkeypatch)
    assert {method: len(handlers) for method, handlers in client.handlers.items()} == before


def test_a_same_document_navigation_right_after_the_commit_does_not_skip_its_load(monkeypatch) -> None:
    # SPA routers often replaceState right after commit; the load of the commit must still be awaited.
    session, _client = _session([(0.05, "page", "commit:L2"), (0.0, "page", "soft"), (0.5, "page", "load:L2")])
    assert _follow(session, monkeypatch, "Page.navigateToHistoryEntry") >= 0.5


def test_a_reload_is_not_satisfied_by_the_old_page_pushing_state(monkeypatch) -> None:
    session, _client = _session([(0.05, "page", "soft"), (0.3, "page", "L2")])
    assert _follow(session, monkeypatch) >= 0.3


def test_history_step_is_not_satisfied_by_the_old_page_pushing_state(monkeypatch) -> None:
    # The old page replaceStates while the history step's own commit is still on the network.
    session, _client = _session([(0.05, "page", "soft"), (0.3, "page", "L2")])
    started = time.monotonic()

    async def fake_capture(_session, **_kwargs):
        return [], None

    async def fake_title(_session):
        return ""

    monkeypatch.setattr(back, "capture_and_register", fake_capture)
    monkeypatch.setattr(back, "_page_title", fake_title)
    monkeypatch.setattr(back, "detect_login", lambda *_args: None)
    asyncio.run(back.follow_navigation(session, "Page.navigateToHistoryEntry", {}, expected_url="https://example.com/"))
    assert time.monotonic() - started >= 0.3


def test_a_same_document_history_step_is_done_at_its_own_url(monkeypatch) -> None:
    session, _client = _session([(0.05, "page", "soft")])  # navigatedWithinDocument to https://example.com/#s
    started = time.monotonic()

    async def fake_capture(_session, **_kwargs):
        return [], None

    async def fake_title(_session):
        return ""

    monkeypatch.setattr(back, "capture_and_register", fake_capture)
    monkeypatch.setattr(back, "_page_title", fake_title)
    monkeypatch.setattr(back, "detect_login", lambda *_args: None)
    asyncio.run(
        back.follow_navigation(session, "Page.navigateToHistoryEntry", {}, expected_url="https://example.com/#s")
    )
    assert time.monotonic() - started < 1.0


def test_a_beforeunload_dialog_is_reported_not_waited_out(monkeypatch) -> None:
    session, _client = _session([(0.05, "page", "beforeunload")])
    captured = []

    async def fake_capture(_session, **_kwargs):
        captured.append(True)
        return [], None

    monkeypatch.setattr(back, "capture_and_register", fake_capture)
    started = time.monotonic()
    text = asyncio.run(back.follow_navigation(session, "Page.reload", {}, cross_document=True))[0]["text"]
    assert time.monotonic() - started < 1.5, "the action lock must be released so browser_dialog can answer"
    assert "dialog=beforeunload" in text and "status=blocked" in text and "browser_dialog" in text
    assert captured == [], "the old page must not be returned as the reloaded one"


def test_a_commit_survives_a_burst_of_same_document_navigations(monkeypatch) -> None:
    session, client = _session([])

    async def scenario():
        after = session.nav_seq
        frame = {"id": "main", "loaderId": "L3", "url": "https://example.com/"}
        await client._emit("Page.frameNavigated", {"frame": frame}, "page")
        for index in range(40):
            await client._emit(
                "Page.navigatedWithinDocument", {"frameId": "main", "url": f"https://example.com/#{index}"}, "page"
            )
        return await session.wait_for_navigation(after=after, timeout=0.2, cross_document=True)

    assert asyncio.run(scenario()) == "L3"


def _follow_text(session, monkeypatch, method="Page.navigateToHistoryEntry", **kwargs):
    async def fake_capture(_session, **_kwargs):
        return [], None

    async def fake_title(_session):
        return ""

    monkeypatch.setattr(back, "capture_and_register", fake_capture)
    monkeypatch.setattr(back, "_page_title", fake_title)
    monkeypatch.setattr(back, "detect_login", lambda *_args: None)
    monkeypatch.setattr(back, "render_snapshot", lambda *_args, **_kwargs: "[snapshot]")
    monkeypatch.setattr(back, "stats_line", lambda *_args: "stats")
    started = time.monotonic()
    text = asyncio.run(back.follow_navigation(session, method, {}, **kwargs))[0]["text"]
    return text, time.monotonic() - started


def test_no_navigation_is_said_not_passed_off_as_done(monkeypatch) -> None:
    session, _client = _session([])  # nav_load_timeout=3.0; the page never navigates
    text, _elapsed = _follow_text(session, monkeypatch)
    assert "未观察到导航" in text


def test_commit_and_load_share_one_deadline(monkeypatch) -> None:
    session, _client = _session([(2.0, "page", "commit:L2")])  # committed, never loads
    _text, elapsed = _follow_text(session, monkeypatch)
    assert elapsed < 3.6, f"{elapsed:.1f}s: the load wait got a second full timeout"


def test_a_dialog_from_the_loading_page_is_reported_as_after_the_commit(monkeypatch) -> None:
    # The new document commits, then its onload alert()s before the load event can fire.
    session, _client = _session([(0.05, "page", "commit:L2"), (0.05, "page", "alert")])
    started = time.monotonic()
    text = asyncio.run(back.follow_navigation(session, "Page.reload", {}, cross_document=True))[0]["text"]
    assert time.monotonic() - started < 1.5, "the load wait must not hold the lock behind the dialog"
    assert "dialog=alert" in text and "status=blocked" in text
    assert "导航已提交" in text and "尚未发生" not in text


def test_a_history_step_that_only_moves_an_iframe_finishes_on_its_commit(monkeypatch) -> None:
    # Joint session history: the entry has the main frame's own URL, only a child frame navigates.
    session, client = _session([])
    session._snapshot.url = "https://example.com/"

    async def child_commit():
        await asyncio.sleep(0.05)
        frame = {"id": "child", "parentId": "main", "loaderId": "LC", "url": "https://example.com/inner"}
        await client._emit("Page.frameNavigated", {"frame": frame}, "page")

    async def scenario():
        asyncio.get_running_loop().create_task(child_commit())
        after = session.nav_seq
        return await session.wait_for_navigation(after=after, timeout=2.0, url="https://example.com/")

    started = time.monotonic()
    assert asyncio.run(scenario()) == ""
    assert time.monotonic() - started < 1.0


def test_a_child_frame_never_satisfies_a_step_to_another_url_or_a_plain_navigate(monkeypatch) -> None:
    session, client = _session([])
    session._snapshot.url = "https://example.com/"

    async def scenario(url):
        after = session.nav_seq
        frame = {"id": "child", "parentId": "main", "loaderId": "LC", "url": "https://ads.example/x"}
        await client._emit("Page.frameNavigated", {"frame": frame}, "page")
        return await session.wait_for_navigation(after=after, timeout=0.2, url=url)

    async def both():
        return await scenario("https://example.com/other"), await scenario(None)

    assert asyncio.run(both()) == (None, None)


def test_the_old_page_pushing_state_does_not_make_a_beforeunload_look_committed(monkeypatch) -> None:
    session, _client = _session([(0.02, "page", "soft"), (0.03, "page", "beforeunload")])
    text = asyncio.run(
        back.follow_navigation(session, "Page.navigateToHistoryEntry", {}, expected_url="https://example.com/b")
    )[0]["text"]
    assert "dialog=beforeunload" in text and "导航尚未发生" in text
