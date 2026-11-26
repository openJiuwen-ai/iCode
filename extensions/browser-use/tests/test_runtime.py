# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import contextlib

import pytest

import icode_browser_use.runtime as runtime
from icode_browser_use.errors import BadRequest, CDPTimeout, PageHung
from icode_browser_use.types import ToolDef


class FakeSession:
    def __init__(self, *, recycle: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.target_id = "t1"
        self._recycle = recycle

    @contextlib.asynccontextmanager
    async def action_lock(self, name: str):
        self.calls.append(("lock_enter", name))
        try:
            yield
        finally:
            self.calls.append(("lock_exit", name))

    def note_action(self) -> None:
        self.calls.append(("note_action",))

    async def ensure_started(self) -> None:
        self.calls.append(("ensure_started",))

    def note_timeout_and_should_recycle(self, target: str) -> bool:
        self.calls.append(("recycle_check", target))
        return self._recycle

    async def recycle_page(self) -> None:
        self.calls.append(("recycle_page",))


def _tool() -> ToolDef:
    return ToolDef(
        name="browser_navigate",
        description="",
        params={"url": {"type": "string", "required": True}},
        schema={},
    )


def test_call_tool_orders_lock_budget_start_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(runtime, "validate", lambda params, arguments: {"url": arguments["url"]})

    async def dispatch(_session, name, arguments):
        _session.calls.append(("dispatch", name, arguments))
        return [{"type": "text", "text": "ok"}]

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    blocks = asyncio.run(runtime.call_tool(session, _tool(), {"url": "https://example.com"}))

    assert blocks == [{"type": "text", "text": "ok"}]
    assert session.calls == [
        ("lock_enter", "browser_navigate"),
        ("note_action",),
        ("ensure_started",),
        ("dispatch", "browser_navigate", {"url": "https://example.com"}),
        ("lock_exit", "browser_navigate"),
    ]


def test_call_tool_recycles_after_timeout_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(recycle=True)
    monkeypatch.setattr(runtime, "validate", lambda params, arguments: arguments)

    async def dispatch(_session, name, arguments):
        raise CDPTimeout("Page.navigate", 20.0, target_id="t9")

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    with pytest.raises(PageHung):
        asyncio.run(runtime.call_tool(session, _tool(), {"url": "https://example.com"}))

    assert ("recycle_check", "t9") in session.calls
    assert ("recycle_page",) in session.calls


def test_call_tool_keeps_timeout_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession(recycle=False)
    monkeypatch.setattr(runtime, "validate", lambda params, arguments: arguments)

    async def dispatch(_session, name, arguments):
        raise CDPTimeout("Page.navigate", 20.0)

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    with pytest.raises(CDPTimeout):
        asyncio.run(runtime.call_tool(session, _tool(), {"url": "https://example.com"}))
    assert ("recycle_page",) not in session.calls


def test_call_tool_really_validates_arguments() -> None:
    """Mutation guard: replacing validate() must not be able to hide a missing arg."""
    with pytest.raises(BadRequest):
        asyncio.run(runtime.call_tool(FakeSession(), _tool(), {}))


def test_download_listing_skips_start_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    tool = ToolDef(name="browser_downloads", description="", params={}, schema={})

    async def dispatch(_session, name, arguments):
        _session.calls.append(("dispatch", name, arguments))
        return []

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    asyncio.run(runtime.call_tool(session, tool, {}))
    assert session.calls == [
        ("lock_enter", "browser_downloads"),
        ("dispatch", "browser_downloads", {}),
        ("lock_exit", "browser_downloads"),
    ]


def test_download_cancel_uses_budget_without_starting_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    tool = ToolDef(
        name="browser_download_cancel",
        description="",
        params={"guid": {"type": "string", "required": True}},
        schema={},
    )

    async def dispatch(_session, name, arguments):
        _session.calls.append(("dispatch", name, arguments))
        return []

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    asyncio.run(runtime.call_tool(session, tool, {"guid": "g-1"}))
    assert session.calls == [
        ("lock_enter", "browser_download_cancel"),
        ("note_action",),
        ("dispatch", "browser_download_cancel", {"guid": "g-1"}),
        ("lock_exit", "browser_download_cancel"),
    ]


def test_reading_logs_never_starts_the_browser_or_spends_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    tool = ToolDef(name="browser_logs", description="", params={}, schema={})

    async def dispatch(_session, name, arguments):
        _session.calls.append(("dispatch", name, arguments))
        return []

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    asyncio.run(runtime.call_tool(session, tool, {}))
    assert session.calls == [
        ("lock_enter", "browser_logs"),
        ("dispatch", "browser_logs", {}),
        ("lock_exit", "browser_logs"),
    ]


def test_closing_a_tab_never_starts_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()
    tool = ToolDef(
        name="browser_tab_close", description="", params={"tab_id": {"type": "string", "required": True}}, schema={}
    )

    async def dispatch(_session, name, arguments):
        _session.calls.append(("dispatch", name, arguments))
        return []

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    asyncio.run(runtime.call_tool(session, tool, {"tab_id": "t-old"}))
    assert ("ensure_started",) not in session.calls
    assert ("note_action",) in session.calls


def test_runtime_routes_observe_tools_to_observe_pool_and_appends_budget_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PoolSession(FakeSession):
        def note_observe_action(self) -> None:
            self.calls.append(("note_observe_action",))

        def action_budget_warning(self, *, observe: bool = False) -> str:
            return f"action_budget_warning: pool={'observe' if observe else 'action'} remaining_window=1"

    session = PoolSession()
    tool = ToolDef(name="browser_extract", description="", params={}, schema={})

    async def dispatch(_session, name, arguments):
        _session.calls.append(("dispatch", name, arguments))
        return [{"type": "text", "text": "page body"}]

    monkeypatch.setattr(runtime, "dispatch", dispatch)
    blocks = asyncio.run(runtime.call_tool(session, tool, {}))
    assert ("note_observe_action",) in session.calls
    assert ("note_action",) not in session.calls
    assert "page body" in blocks[0]["text"]
    assert "action_budget_warning: pool=observe" in blocks[0]["text"]
