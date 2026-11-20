# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Action-journal ownership: no global fallback, no cross-owner reads, durable rows (W04)."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
from types import SimpleNamespace

import pytest
from chrys.extensions import ToolContext, ToolError

import browser_use.extension as ext
from browser_use.actions.actionability import record_action
from browser_use.journal import Journal


class FakeOwnedSession:
    """Owned-session double with the real journal and action-lock surface."""

    def __init__(self, cfg, *, journal_owner: str = "", journal_session: str = "") -> None:
        self.cfg = cfg
        self.journal_owner = journal_owner
        self.journal_session = journal_session
        self.journal = Journal(None)
        self.state = "idle"
        self.started_once = False
        self.page_epoch = 9
        self.login_pending = False
        self.snapshot = SimpleNamespace(url="", title="")

    @contextlib.asynccontextmanager
    async def action_lock(self, _name: str):
        yield

    def note_action(self) -> None:
        return None

    async def ensure_started(self) -> None:
        raise AssertionError("action_status must not start the browser")

    async def aclose(self) -> None:
        self.state = "closed"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch):
    for name in tuple(os.environ):
        if name.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(name)
    importlib.reload(ext)
    ext._browsers.clear()
    yield
    ext._browsers.clear()


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(ext, "BrowserSession", FakeOwnedSession)


def ctx(tmp_path, *, agent: str = "A", session: str = "s1") -> ToolContext:
    return ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        agent_name=agent,
        session_id=session,
    )


def add_browser(context: ToolContext, *, browser_id: str, slot: int, agent: str | None = None) -> ext._Browser:
    session = FakeOwnedSession(
        SimpleNamespace(state_dir=context.data_dir / f"slot-{slot}"),
        journal_owner=ext._owner_key(context),
        journal_session=f"slot-{slot}",
    )
    browser = ext._Browser(
        browser_id,
        session,
        context.session_id,
        agent or context.agent_name,
        slot=slot,
    )
    ext._browsers[browser_id] = browser
    return browser


def test_status_query_without_any_browser_is_refused(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    context = ctx(tmp_path)

    async def scenario() -> None:
        with pytest.raises(ToolError) as info:
            await ext.browser_action_status(context, "abc123")
        assert info.value.code == "browser_id_required"
        assert "browser_id" in str(info.value)

    asyncio.run(scenario())


def test_status_query_routes_through_the_only_browser(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser = add_browser(context, browser_id="b1", slot=0)
        record_action(browser.session, "abc123", "browser_click", "succeeded", "effect=dom")
        text = await ext.browser_action_status(context, "abc123")
        assert '"status": "succeeded"' in text
        assert "browser_id" not in text

    asyncio.run(scenario())


def test_status_query_with_two_browsers_requires_a_choice(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    context = ctx(tmp_path)

    async def scenario() -> None:
        add_browser(context, browser_id="b1", slot=0)
        add_browser(context, browser_id="b2", slot=1)
        with pytest.raises(ToolError) as info:
            await ext.browser_action_status(context, "abc123")
        assert info.value.code == "browser_id_required"
        assert "browser_id" in str(info.value)

    asyncio.run(scenario())


def test_status_query_never_reads_another_browser_journal(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    context = ctx(tmp_path)

    async def scenario() -> None:
        first = add_browser(context, browser_id="b1", slot=0)
        add_browser(context, browser_id="b2", slot=1)
        record_action(first.session, "abc123", "browser_click", "succeeded", "effect=dom")
        found = await ext.browser_action_status(context, "abc123", "b1")
        assert '"status": "succeeded"' in found
        missed = await ext.browser_action_status(context, "abc123", "b2")
        assert "status=unknown" in missed

    asyncio.run(scenario())


def test_status_query_refuses_another_agents_browser(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)

    async def scenario() -> None:
        add_browser(ctx(tmp_path, agent="A"), browser_id="b1", slot=0, agent="A")
        with pytest.raises(ToolError) as info:
            await ext.browser_action_status(ctx(tmp_path, agent="B"), "abc123", "b1")
        assert info.value.code == "browser_owner_mismatch"
        assert "browser_open" in str(info.value)

    asyncio.run(scenario())


def test_a_known_action_id_is_unknown_after_the_browser_is_closed(tmp_path, monkeypatch) -> None:
    configure(monkeypatch, tmp_path)
    context = ctx(tmp_path)

    async def scenario() -> None:
        browser = add_browser(context, browser_id="b1", slot=0)
        record_action(browser.session, "abc123", "browser_click", "succeeded", "effect=dom")
        ext._browsers.pop("b1", None)
        with pytest.raises(ToolError) as info:
            await ext.browser_action_status(context, "abc123")
        assert info.value.code == "browser_id_required"
        assert "browser_id" in str(info.value)

    asyncio.run(scenario())
