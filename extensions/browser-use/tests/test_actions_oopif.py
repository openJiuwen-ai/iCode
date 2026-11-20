# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from icode_browser_use.actions._shared import call_value, eval_value
from icode_browser_use.config import BrowserConfig
from icode_browser_use.session import BrowserSession


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.calls.append((method, session_id))
        return {"result": {"value": "ok"}}


class FakeSession:
    session_id = "main"
    cfg = SimpleNamespace(evaluate_timeout=1.0)

    def __init__(self) -> None:
        self.client = FakeClient()

    def require_client(self) -> FakeClient:
        return self.client


def test_eval_value_routes_to_the_child_session() -> None:
    session = FakeSession()
    asyncio.run(eval_value(session, "1", cdp_session_id="child"))
    assert session.client.calls == [("Runtime.evaluate", "child")]


def test_call_value_routes_to_the_child_session() -> None:
    session = FakeSession()
    asyncio.run(call_value(session, "obj", "function(){}", cdp_session_id="child"))
    assert session.client.calls == [("Runtime.callFunctionOn", "child")]


def test_oopif_offset_roundtrip() -> None:
    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    session.set_oopif_offset("child", (10.0, 20.0))
    assert session.oopif_offset("child") == (10.0, 20.0)
    assert session.oopif_offset("missing") == (0.0, 0.0)
