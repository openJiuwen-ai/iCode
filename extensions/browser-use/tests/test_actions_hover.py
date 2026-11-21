# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from icode_browser_use.actions import hover
from icode_browser_use.journal import Journal
from icode_browser_use.snapshot.fusion import FusedNode


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, params or {}, session_id))
        return {"result": {}}


class FakeSession:
    session_id = "main"
    target_id = "t"
    page_epoch = 7
    cfg = SimpleNamespace(cdp_timeout=1.0)

    def __init__(self) -> None:
        self.client = FakeClient()
        self.marked = 0
        self.successes = 0
        self.journal = Journal(None)
        self.snapshot = SimpleNamespace(
            nodes=[FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=self.page_epoch)]
        )

    def resolve_target(self, _target):
        return SimpleNamespace(token="@e1#7", cdp_session_id=None)

    async def resolve_object_id(self, _entry):
        return "obj"

    def require_client(self):
        return self.client

    def oopif_offset(self, _session_id):
        return (0.0, 0.0)

    def mark_dirty(self) -> None:
        self.marked += 1

    def note_success(self, _target_id) -> None:
        self.successes += 1


async def _no_sleep(_seconds):
    return None


def test_hover_moves_the_mouse_without_clicking(monkeypatch) -> None:
    session = FakeSession()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 20.0

    async def fake_hit_test(*_args, **_kwargs):
        return {"inside": True}

    monkeypatch.setattr(hover, "_hit_point", fake_hit_point)
    monkeypatch.setattr(hover, "hit_test", fake_hit_test)
    monkeypatch.setattr(hover, "_sleep", _no_sleep)
    blocks = asyncio.run(hover.run(session, {"target": "评论"}))
    assert session.client.calls == [
        (
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": 10.0, "y": 20.0, "button": "none", "buttons": 0},
            "main",
        )
    ]
    assert "已悬停" in blocks[0]["text"]
    assert session.marked == 1
    assert session.successes == 1


def test_hover_refuses_when_hit_test_is_inconclusive(monkeypatch) -> None:
    session = FakeSession()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 20.0

    async def fake_hit_test(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hover, "_hit_point", fake_hit_point)
    monkeypatch.setattr(hover, "hit_test", fake_hit_test)
    blocks = asyncio.run(hover.run(session, {"target": "评论"}))
    assert session.client.calls == []
    assert "status=uncertain" in blocks[0]["text"]


def test_hover_is_registered_as_a_tool() -> None:
    from icode_browser_use.actions import build_tool_defs

    names = [tool.name for tool in build_tool_defs(evaluate_enabled=False)]
    assert "browser_hover" in names
