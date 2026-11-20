# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest

from browser_use.actions import screenshot
from browser_use.errors import BadRequest, BrowserError
from browser_use.snapshot.fusion import FusedNode

_PNG_1X1 = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
).decode("ascii")


class _ShotClient:
    def __init__(self, *, rect: dict | None = None, no_layout: bool = False) -> None:
        self.rect = rect
        self.no_layout = no_layout
        self.calls: list[tuple[str, dict, str | None]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params, session_id))
        if method == "DOM.scrollIntoViewIfNeeded":
            if self.no_layout:
                raise BrowserError('{"code":-32000,"message":"Node does not have a layout object"}')
            return {}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": self.rect}}
        if method == "Page.captureScreenshot":
            return {"data": _PNG_1X1}
        return {}


class _ShotSession:
    session_id = "main"
    target_id = "t1"
    page_epoch = 7
    cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0)

    def __init__(self, client: _ShotClient, *, cdp_session_id: str | None = None) -> None:
        self.client = client
        self._cdp_session_id = cdp_session_id
        self.successes = 0
        self.snapshot = SimpleNamespace(
            nodes=[FusedNode(1, "f", "t1", "button", "Follow", "button", 0, (0, 0, 10, 10), ref=1, epoch=7)]
        )

    def resolve_target(self, _target):
        return SimpleNamespace(token="@e1#7", cdp_session_id=self._cdp_session_id)

    async def resolve_object_id(self, _entry):
        return "obj-1"

    def require_client(self):
        return self.client

    def oopif_offset(self, cdp_session_id):
        return (100.0, 200.0) if cdp_session_id else (0.0, 0.0)

    def note_success(self, _target_id) -> None:
        self.successes += 1


def test_target_screenshot_scrolls_and_uses_page_coords_with_padding() -> None:
    client = _ShotClient(rect={"x": 40.0, "y": 50.0, "width": 120.0, "height": 30.0, "scrollX": 0.0, "scrollY": 500.0})
    session = _ShotSession(client)
    blocks = asyncio.run(screenshot.run(session, {"full_page": False, "target": "@e1#7", "annotate": False}))
    assert blocks[0]["type"] == "image"
    methods = [m for m, _, _ in client.calls]
    assert methods[0] == "DOM.scrollIntoViewIfNeeded"
    cap_params = next(p for m, p, _ in client.calls if m == "Page.captureScreenshot")
    assert cap_params["captureBeyondViewport"] is True
    clip = cap_params["clip"]
    # x=40-8=32, y=550-8=542, w=120+16=136, h=30+16=46
    assert clip == {"x": 32.0, "y": 542.0, "width": 136.0, "height": 46.0, "scale": 1}


def test_target_screenshot_applies_oopif_offset_and_child_session() -> None:
    client = _ShotClient(rect={"x": 10.0, "y": 20.0, "width": 80.0, "height": 20.0, "scrollX": 0.0, "scrollY": 0.0})
    session = _ShotSession(client, cdp_session_id="oopif-1")
    asyncio.run(screenshot.run(session, {"full_page": False, "target": "@e1#7", "annotate": False}))
    scroll_call = next((m, sid) for m, _, sid in client.calls if m == "DOM.scrollIntoViewIfNeeded")
    assert scroll_call == ("DOM.scrollIntoViewIfNeeded", "oopif-1")
    cap_params = next(p for m, p, _ in client.calls if m == "Page.captureScreenshot")
    assert cap_params["clip"]["x"] == 102.0
    assert cap_params["clip"]["y"] == 212.0


def test_target_screenshot_translates_no_layout_error_to_bad_request() -> None:
    client = _ShotClient(no_layout=True)
    session = _ShotSession(client)
    with pytest.raises(BadRequest) as exc_info:
        asyncio.run(screenshot.run(session, {"full_page": False, "target": "@e1#7", "annotate": False}))
    assert "无可见布局盒模型" in str(exc_info.value)
