# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import drag
from icode_browser_use.errors import BadRequest
from icode_browser_use.journal import Journal


class _DragClient:
    def __init__(self, *, trigger_html5: bool = False) -> None:
        self.trigger_html5 = trigger_html5
        self.calls: list[tuple[str, dict]] = []
        self._handlers: dict[str, list] = {}
        self.closed = False

    def on(self, method: str, handler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    def off(self, method: str, handler) -> None:
        if method in self._handlers and handler in self._handlers[method]:
            self._handlers[method].remove(handler)

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if (
            self.trigger_html5
            and method == "Input.dispatchMouseEvent"
            and params.get("type") == "mouseMoved"
            and params.get("buttons") == 1
        ):
            for handler in list(self._handlers.get("Input.dragIntercepted", ())):
                await handler(
                    {"data": {"items": [{"mimeType": "text/plain", "data": "card-1"}], "dragOperationsMask": 1}},
                    session_id,
                )
        return {}


class _DragSession:
    session_id = "main"
    target_id = "t1"
    page_epoch = 5
    dialog_seq = 0
    cfg = SimpleNamespace(cdp_timeout=5.0, evaluate_timeout=5.0)

    def __init__(self, *, trigger_html5: bool = False) -> None:
        self.client = _DragClient(trigger_html5=trigger_html5)
        self.journal = Journal(None)
        self.snapshot = SimpleNamespace(nodes=[SimpleNamespace(epoch=self.page_epoch)])
        self.snapshot_dirty = False
        self.marked = 0
        self.successes = 0
        self._entries = {
            "Task Alpha": SimpleNamespace(
                token="@e1#5", backend_node_id=1, frame_id="f1", cdp_session_id=None, bbox=(10, 10, 100, 30)
            ),
            "Done Column": SimpleNamespace(
                token="@e2#5", backend_node_id=2, frame_id="f1", cdp_session_id=None, bbox=(220, 10, 140, 100)
            ),
        }

    def require_client(self):
        return self.client

    def resolve_target(self, spec: str):
        return self._entries[spec]

    async def resolve_object_id(self, entry):
        return f"oid-{entry.backend_node_id}"

    def oopif_offset(self, _sid):
        return (0.0, 0.0)

    def dialog_since(self, _seq):
        return None

    def mark_dirty(self) -> None:
        self.marked += 1

    def note_success(self, _tid) -> None:
        self.successes += 1


def test_drag_pointer_mode_dispatches_stepped_mouse_sequence_and_records_journal(monkeypatch) -> None:
    session = _DragSession(trigger_html5=False)
    mutations = 0

    async def fake_facts(*_args, **_kwargs):
        return {"connected": True, "tag": "div", "disabled": False, "ariaDisabled": False}

    async def fake_locate(_s, oid, *_args, **_kwargs):
        return (60.0, 25.0, "centre", {"inside": True}) if oid == "oid-1" else (290.0, 60.0, "centre", {"inside": True})

    async def fake_hit_point(_s, oid, *_args, **_kwargs):
        return (60.0, 25.0) if oid == "oid-1" else (290.0, 60.0)

    async def fake_page(*_args, **_kwargs):
        return {"href": "https://example.com", "mutations": mutations, "expanded": 0, "dialogs": 0, "active": "div"}

    orig_send = session.client.send

    async def send_with_mut(method, params=None, session_id=None, timeout=None):
        nonlocal mutations
        res = await orig_send(method, params=params, session_id=session_id, timeout=timeout)
        if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mouseReleased":
            mutations = 1
        return res

    session.client.send = send_with_mut  # type: ignore[method-assign]
    monkeypatch.setattr(drag, "_target_facts", fake_facts)
    monkeypatch.setattr(drag, "_locate_hit", fake_locate)
    monkeypatch.setattr(drag, "_hit_point", fake_hit_point)
    monkeypatch.setattr(drag, "_page_state", fake_page)
    monkeypatch.setattr(drag, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(drag.run(session, {"source": "Task Alpha", "target": "Done Column", "steps": 4}))
    text = blocks[0]["text"]
    assert "mode=pointer" in text
    assert "effect=dom" in text
    assert "status=succeeded" in text
    assert session.successes == 1
    mouse_types = [p["type"] for m, p in session.client.calls if m == "Input.dispatchMouseEvent"]
    assert mouse_types[0] == "mouseMoved"
    assert mouse_types[1] == "mousePressed"
    assert mouse_types[-1] == "mouseReleased"
    assert mouse_types.count("mouseMoved") == 5  # initial move + 4 steps
    # Intercept disabled cleanly in finally and handler unregistered
    assert ("Input.setInterceptDrags", {"enabled": False}) in session.client.calls
    assert not session.client._handlers.get("Input.dragIntercepted")
    rows = session.journal.rows_for("browser_drag", 5)
    assert any(r["status"] == "succeeded" for r in rows)


def test_drag_html5_mode_hands_off_to_dispatch_drag_events(monkeypatch) -> None:
    session = _DragSession(trigger_html5=True)
    mutations = 0

    async def fake_facts(*_args, **_kwargs):
        return {"connected": True, "tag": "div", "disabled": False, "ariaDisabled": False}

    async def fake_locate(*_args, **_kwargs):
        return 60.0, 25.0, "centre", {"inside": True}

    async def fake_hit_point(_s, oid, *_args, **_kwargs):
        return (60.0, 25.0) if oid == "oid-1" else (290.0, 60.0)

    async def fake_page(*_args, **_kwargs):
        return {"href": "https://example.com", "mutations": mutations, "expanded": 0, "dialogs": 0, "active": "div"}

    orig_send = session.client.send

    async def send_with_drop(method, params=None, session_id=None, timeout=None):
        nonlocal mutations
        res = await orig_send(method, params=params, session_id=session_id, timeout=timeout)
        if method == "Input.dispatchDragEvent" and (params or {}).get("type") == "drop":
            mutations = 2
        return res

    session.client.send = send_with_drop  # type: ignore[method-assign]
    monkeypatch.setattr(drag, "_target_facts", fake_facts)
    monkeypatch.setattr(drag, "_locate_hit", fake_locate)
    monkeypatch.setattr(drag, "_hit_point", fake_hit_point)
    monkeypatch.setattr(drag, "_page_state", fake_page)
    monkeypatch.setattr(drag, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(drag.run(session, {"source": "Task Alpha", "target": "Done Column", "steps": 6}))
    text = blocks[0]["text"]
    assert "mode=html5" in text
    assert "status=succeeded" in text
    drag_types = [p["type"] for m, p in session.client.calls if m == "Input.dispatchDragEvent"]
    assert drag_types == ["dragEnter", "dragOver", "drop"]


def test_drag_refuses_same_element_and_blocks_disabled_source(monkeypatch) -> None:
    session = _DragSession()
    with pytest.raises(BadRequest, match="同一个元素"):
        asyncio.run(drag.run(session, {"source": "Task Alpha", "target": "Task Alpha", "steps": 4}))

    async def fake_disabled(*_args, **_kwargs):
        return {"connected": True, "tag": "div", "disabled": True, "ariaDisabled": False}

    monkeypatch.setattr(drag, "_target_facts", fake_disabled)
    blocks = asyncio.run(drag.run(session, {"source": "Task Alpha", "target": "Done Column", "steps": 4}))
    assert "dispatch=not_sent" in blocks[0]["text"]
    assert "status=blocked" in blocks[0]["text"]
    assert not session.client.calls
