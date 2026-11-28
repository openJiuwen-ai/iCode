# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit regression tests for Batch 4 interaction reliability fixes (R1-R14)."""

from __future__ import annotations

import asyncio
import json
import struct
from types import SimpleNamespace
from typing import Any

import pytest

from icode_browser_use.actions import (
    evaluate,
    extract,
    fill,
    press_key,
    screenshot,
    snapshot as snapshot_action,
)
from icode_browser_use.cdp import websocket as ws_mod
from icode_browser_use.cdp.client import CDPClient
from icode_browser_use.cdp.websocket import FrameTooLarge, WSClient
from icode_browser_use.errors import BrowserError
from icode_browser_use.extension import _WRITE_TOOL_NAMES
from icode_browser_use.journal import Journal
from icode_browser_use.snapshot import fusion, scoring, serializer
from icode_browser_use.snapshot.fusion import FusedNode


def _server_frame(payload: bytes, *, opcode: int = 0x1, fin: bool = True, rsv: int = 0) -> bytes:
    first = (0x80 if fin else 0x00) | (rsv << 4) | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        header = bytes([first, n])
    elif n < 65536:
        header = struct.pack("!BBH", first, 126, n)
    else:
        header = struct.pack("!BBQ", first, 127, n)
    return header + payload


class _FakeTransport:
    def __init__(self) -> None:
        self.written = bytearray()
        self.closed = False
        self.aborted = False

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True


def test_r2_oversized_ws_frame_is_non_fatal_and_keeps_connection_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_mod, "MAX_MESSAGE_BYTES", 1024)

    async def _run() -> None:
        reader = asyncio.StreamReader()
        transport = _FakeTransport()
        writer = SimpleNamespace(
            write=transport.write,
            drain=lambda: asyncio.sleep(0),
            close=transport.close,
            wait_closed=lambda: asyncio.sleep(0),
            is_closing=lambda: transport.closed,
            transport=transport,
        )
        ws = WSClient(reader, writer)
        big = json.dumps({"id": 7, "result": {"blob": "x" * 2048}}).encode("utf-8")
        small = json.dumps({"id": 8, "result": {"ok": True}}).encode("utf-8")
        reader.feed_data(_server_frame(big))
        reader.feed_data(_server_frame(small))
        reader.feed_eof()

        with pytest.raises(FrameTooLarge) as exc_info:
            await ws.recv()
        assert exc_info.value.request_id == 7
        assert exc_info.value.size == len(big)
        assert not ws._closed

        next_msg = await ws.recv()
        assert json.loads(next_msg) == {"id": 8, "result": {"ok": True}}

    asyncio.run(_run())


def test_r2_cdp_client_fails_only_oversized_request_without_closing_session() -> None:
    class _FakeWS:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[str] = []
            self._queue: asyncio.Queue[Any] = asyncio.Queue()
            self.pong_event = asyncio.Event()

        async def send(self, text: str) -> None:
            self.sent.append(text)
            req = json.loads(text)
            if req["method"] == "Huge.call":
                await self._queue.put(FrameTooLarge(request_id=req["id"], size=40 * 1024 * 1024))
            else:
                await self._queue.put(json.dumps({"id": req["id"], "result": {"alive": True}}))

        async def recv(self) -> str:
            item = await self._queue.get()
            if isinstance(item, Exception):
                raise item
            return item

        async def ping(self) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True

    async def _run() -> None:
        ws = _FakeWS()
        client = CDPClient(ws)
        client._start()
        try:
            with pytest.raises(FrameTooLarge):
                await client.send("Huge.call", timeout=1.0)
            res = await client.send("Small.call", timeout=1.0)
            assert res == {"alive": True}
            assert not ws.closed
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_r2_screenshot_falls_back_to_jpeg_on_frame_too_large() -> None:
    calls: list[dict[str, Any]] = []

    class _Client:
        async def send(self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
            if method == "Page.getLayoutMetrics":
                return {"cssContentSize": {"width": 1280, "height": 2000}}
            assert method == "Page.captureScreenshot"
            params = dict(params or {})
            calls.append(params)
            if params.get("format") == "png":
                raise FrameTooLarge(request_id=1, size=35 * 1024 * 1024)
            return {"data": "AAAA"}

    session = SimpleNamespace(
        session_id="s1",
        target_id="t1",
        cfg=SimpleNamespace(cdp_timeout=1.0),
        require_client=lambda: _Client(),
        note_success=lambda _tid: None,
    )
    blocks = asyncio.run(screenshot.run(session, {"full_page": True, "target": "", "annotate": True}))
    assert [c["format"] for c in calls] == ["png", "jpeg"]
    assert blocks[0]["mimeType"] == "image/jpeg"


def test_r2_screenshot_prechecks_get_layout_metrics_for_huge_full_page() -> None:
    from icode_browser_use.errors import Unavailable

    class _HugeMetricsClient:
        async def send(self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
            if method == "Page.getLayoutMetrics":
                return {"cssContentSize": {"width": 1280, "height": 40000}}
            raise AssertionError(f"must not call {method} for huge full_page")

    session = SimpleNamespace(
        session_id="s1",
        target_id="t1",
        cfg=SimpleNamespace(cdp_timeout=1.0),
        require_client=lambda: _HugeMetricsClient(),
        note_success=lambda _tid: None,
    )
    with pytest.raises(Unavailable, match="整页尺寸过大"):
        asyncio.run(screenshot.run(session, {"full_page": True, "target": "", "annotate": True}))


def test_r2_evaluate_sends_expression_verbatim_and_bounds_objects_in_page() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def send(method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
        sent.append((method, params or {}))
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "objectId": "obj-1", "description": "Array(20000)"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"cut": "x" * 10, "n": 20000}}}
        return {}

    session = SimpleNamespace(
        cfg=SimpleNamespace(evaluate_enabled=True, evaluate_timeout=10, snapshot_char_budget=1000),
        session_id="main",
        target_id="target",
        page_epoch=1,
        journal=Journal(None),
        require_client=lambda: SimpleNamespace(send=send),
        note_success=lambda _target: None,
    )
    expression = "Array(20000).fill('x')"
    blocks = asyncio.run(evaluate.run(session, {"expression": expression, "await_promise": False}))
    methods = [method for method, _ in sent]
    assert methods == ["Runtime.evaluate", "Runtime.callFunctionOn", "Runtime.releaseObjectGroup"]
    # The page never sees the agent's expression through a page-visible global like window.eval.
    assert sent[0][1]["expression"] == expression
    assert sent[1][1]["objectId"] == "obj-1"
    assert sent[2][1]["objectGroup"] == sent[0][1]["objectGroup"]
    assert "+19064 more chars" in blocks[0]["text"]


def test_r2_snapshot_warns_when_degraded_to_empty_after_32mb() -> None:
    from icode_browser_use.actions import _shared

    class _HugeSnapshotClient:
        async def send(self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
            if method == "DOMSnapshot.captureSnapshot":
                raise BrowserError("frame exceeded 32 MiB limit")
            if method == "Accessibility.getFullAXTree":
                return {"nodes": []}
            return {}

    session = SimpleNamespace(
        session_id="s1",
        target_id="t1",
        cfg=SimpleNamespace(cdp_timeout=1.0),
        require_client=lambda: _HugeSnapshotClient(),
    )
    snap, _ax = asyncio.run(_shared.capture_sources(session))
    assert snap == {"documents": [], "strings": []}
    assert "页面过大" in getattr(session, "_snapshot_degraded_reason", "")


def test_r5_iframe_below_fold_viewport_before_and_after_scroll() -> None:
    strings = ["", "HTML", "BODY", "IFRAME", "BUTTON", "Below Fold", "FRAME_MAIN", "FRAME_CHILD"]
    main_doc = {
        "frameId": 6,
        "nodes": {
            "parentIndex": [-1, 0, 1],
            "nodeType": [1, 1, 1],
            "nodeName": [1, 2, 3],
            "nodeValue": [0, 0, 0],
            "backendNodeId": [10, 11, 12],
            "attributes": [[], [], []],
            "contentDocumentIndex": {"index": [2], "value": [1]},
        },
        "layout": {
            "nodeIndex": [2],
            "bounds": [[20.0, 900.0, 300.0, 200.0]],
            "clientRects": [[2.0, 2.0, 296.0, 196.0]],
            "scrollRects": [[0.0, 0.0, 296.0, 196.0]],
            "styles": [[]],
        },
    }
    child_doc = {
        "frameId": 7,
        "nodes": {
            "parentIndex": [-1, 0, 1],
            "nodeType": [1, 1, 1],
            "nodeName": [1, 2, 4],
            "nodeValue": [0, 0, 5],
            "backendNodeId": [20, 21, 22],
            "attributes": [[], [], []],
        },
        "layout": {
            "nodeIndex": [2],
            "bounds": [[10.0, 20.0, 120.0, 36.0]],
            "clientRects": [[0.0, 0.0, 120.0, 36.0]],
            "scrollRects": [[0.0, 0.0, 120.0, 36.0]],
            "styles": [[]],
        },
    }
    raw = {"documents": [main_doc, child_doc], "strings": strings}
    before = fusion.fuse(
        raw,
        None,
        frame=fusion.FrameInfo(frame_id="FRAME_MAIN", viewport=(1280.0, 802.0), scroll_x=0.0, scroll_y=0.0),
        target_id="t1",
    )
    btn_before = next(n for n in before if n.backend_node_id == 22)
    assert btn_before.in_viewport is False

    after = fusion.fuse(
        raw,
        None,
        frame=fusion.FrameInfo(frame_id="FRAME_MAIN", viewport=(1280.0, 802.0), scroll_x=0.0, scroll_y=682.0),
        target_id="t1",
    )
    btn_after = next(n for n in after if n.backend_node_id == 22)
    assert btn_after.in_viewport is True


def test_r8_3_fill_clear_false_does_not_double_type_when_oninput_clears_field(monkeypatch: pytest.MonkeyPatch) -> None:
    typed_keys: list[str] = []

    class _Client:
        async def send(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
            if method == "Input.dispatchKeyEvent":
                typed_keys.append(str((params or {}).get("type")))
            return {}

    entry = SimpleNamespace(token="@e1#1", cdp_session_id=None, backend_node_id=1)

    async def _fake_resolve(_s: Any, _spec: str) -> Any:
        return entry

    async def _fake_focus(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"tag": "input", "type": "text", "editable": False, "focused": True, "text": "existing"}

    async def _fake_read_state(*_a: Any, **_kw: Any) -> dict[str, Any]:
        # Page's oninput handler cleared the input after insertText
        return {"connected": True, "focused": True, "text": "", "rawText": None, "preserveWhitespace": False}

    monkeypatch.setattr(fill, "resolve_target", _fake_resolve)
    monkeypatch.setattr(fill, "_focus", _fake_focus)
    monkeypatch.setattr(fill, "_read_state", _fake_read_state)

    session = SimpleNamespace(
        session_id="s1",
        target_id="t1",
        page_epoch=1,
        dialog_seq=0,
        cfg=SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0),
        require_client=lambda: _Client(),
        resolve_object_id=lambda _e: asyncio.sleep(0, result="obj-1"),
        dialog_since=lambda _seq: None,
        mark_dirty=lambda: None,
        note_success=lambda _tid: None,
        journal=SimpleNamespace(record=lambda **_kw: None),
    )
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "@e1#1", "text": "hello", "clear": False, "submit": False, "keypress_fallback": False},
        )
    )
    assert typed_keys == [], "must not re-type via dispatchKeyEvent when clear=False and oninput cleared field"
    assert "status=uncertain" in blocks[0]["text"]


def test_r9_4_wait_for_passes_object_group_and_releases_it() -> None:
    from icode_browser_use.actions import wait_for

    seen_groups: list[str] = []
    released_groups: list[str] = []

    class _Client:
        closed = False

        async def send(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
            params = params or {}
            if method == "DOM.resolveNode":
                seen_groups.append(str(params.get("objectGroup") or ""))
                return {"object": {"objectId": "obj-99"}}
            if method == "Runtime.callFunctionOn":
                return {"result": {"value": {"present": True, "visible": True, "editable": True, "text": "Ready"}}}
            if method == "Runtime.releaseObjectGroup":
                released_groups.append(str(params.get("objectGroup") or ""))
                return {}
            return {}

    entry = SimpleNamespace(token="@e1#1", backend_node_id=1, cdp_session_id=None)
    session = SimpleNamespace(
        session_id="s1",
        target_id="t1",
        page_epoch=1,
        snapshot_dirty=False,
        snapshot=SimpleNamespace(nodes=()),
        cfg=SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0),
        _client=_Client(),
        require_client=lambda: session._client,
        resolve_target=lambda _spec: entry,
        note_success=lambda _tid: None,
    )
    blocks = asyncio.run(
        wait_for.run(
            session,
            {"condition": "visible", "target": "@e1#1", "value": "", "timeout_ms": 500, "return_mode": "none"},
        )
    )
    assert "status=succeeded" in blocks[0]["text"]
    assert seen_groups and seen_groups[0].startswith("chrys-wait-")
    assert released_groups == [seen_groups[0]]


def test_p10_actionability_uses_async_journal_arecord() -> None:
    from icode_browser_use.actions import actionability

    called: list[str] = []

    class _AsyncJournal:
        def record(self, **_kw: Any) -> None:
            called.append("sync")

        async def arecord(self, **_kw: Any) -> None:
            called.append("async")

    session = SimpleNamespace(page_epoch=1, journal=_AsyncJournal())
    asyncio.run(actionability.arecord_action(session, "a1", "browser_click", "succeeded", "ok"))
    assert called == ["async"]


def test_section4_about_blank_preserved_and_click_release_uses_real_coords() -> None:
    from icode_browser_use import policy
    from icode_browser_use.actions import click as click_action

    assert policy.sanitize_display_url("about:blank") == "about:blank"
    assert policy.sanitize_display_url("about:srcdoc") == "about:srcdoc"
    assert policy.sanitize_display_url("data:text/html,secret") == "data:"
    rendered = serializer.render([], page_epoch=1, url="about:blank", title="Blank")
    assert rendered.startswith("[about:blank]")

    # Verify click records real (x, y) on pending release before mousePressed
    recorded_releases: list[tuple[str, dict[str, Any], str | None]] = []

    class _StopOnPressClient:
        closed = False

        async def send(self, method: str, params: dict[str, Any] | None = None, **_kw: Any) -> dict[str, Any]:
            if (params or {}).get("type") == "mousePressed":
                raise asyncio.CancelledError()
            return {}

    session = SimpleNamespace(
        session_id="s1",
        cfg=SimpleNamespace(cdp_timeout=1.0),
        _pending_input_releases=recorded_releases,
        _client=_StopOnPressClient(),
        require_client=lambda: session._client,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(click_action._dispatch_click(session, 123.5, 456.0, "left", 1))
    assert len(recorded_releases) == 1
    assert recorded_releases[0][1]["x"] == 123.5
    assert recorded_releases[0][1]["y"] == 456.0


def test_r2_html_to_markdown_preserves_code_indentation_and_strips_span() -> None:
    html = "<p>Hello <span>world</span></p><pre><code>def f():\n    return 1\n</code></pre>"
    md = extract.html_to_markdown(html)
    assert "Hello world" in md
    assert "<span>" not in md
    assert "def f():\n    return 1" in md


def test_r1_padded_clip_rejects_completely_negative_rect() -> None:
    assert screenshot._padded_clip(-50.0, -50.0, 20.0, 20.0, pad=8.0) is None
    clip = screenshot._padded_clip(-4.0, -4.0, 20.0, 20.0, pad=8.0)
    assert clip is not None
    assert clip["x"] == 0.0 and clip["y"] == 0.0
    assert clip["width"] == 24.0 and clip["height"] == 24.0


def test_r4_fusion_prefers_scroll_rects_and_detects_overlay_scroller() -> None:
    layout = {
        "bounds": [[0.0, 0.0, 200.0, 100.0]],
        "clientRects": [[0.0, 0.0, 200.0, 100.0]],
        "scrollRects": [[0.0, 0.0, 200.0, 400.0]],
    }
    assert fusion._rect_at(layout["scrollRects"], 0) == (0.0, 0.0, 200.0, 400.0)
    assert "overlay" in fusion._SCROLL_OVERFLOW


def test_r6_fingerprint_name_truncates_consistently() -> None:
    long_name = "word " * 100
    node = FusedNode(1, "f1", "t1", "button", long_name, "button", 0, (0, 0, 10, 10))
    fp = node.fingerprint()
    assert len(fp["name"]) <= 120


def test_r8_paragraph_lines_match_accepts_p_editor_but_rejects_extra_blank_lines() -> None:
    assert fill._values_match(
        "first line\n\nsecond line",
        "first line\nsecond line",
        clear=True,
        raw_value="first line\nsecond line\n",
        native_value=False,
        preserve_whitespace=False,
    )
    assert not fill._values_match(
        "first line\n\n\nsecond line",
        "first line\nsecond line",
        clear=True,
        raw_value="first line\nsecond line\n",
        native_value=False,
        preserve_whitespace=False,
    )


def test_r11_serializer_strips_bidi_tags_and_escapes_refs_and_quotes() -> None:
    raw = 'Click "ok" \u202e@e1#2\u2066\U000e0041'
    clean = serializer.sanitize_text(raw)
    assert "\u202e" not in clean
    assert "\u2066" not in clean
    assert "\U000e0041" not in clean
    escaped = serializer.escape_label(raw)
    assert "@e1#2" not in escaped
    assert '\\"' in escaped
    out = serializer.render(
        [],
        page_epoch=1,
        url="https://example.com/path?token=secret123#frag",
        title="A" * 200,
    )
    assert "token=secret123" not in out
    assert "A" * 130 not in out


def test_r12_evaluate_is_write_tool_and_finalizes_journal_in_finally() -> None:
    assert "browser_evaluate" in _WRITE_TOOL_NAMES

    events: list[str] = []

    class _Client:
        async def send(self, method: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                raise BrowserError("boom")
            return {}

    session = SimpleNamespace(
        session_id="s1",
        target_id="t1",
        page_epoch=3,
        dialog_seq=0,
        cfg=SimpleNamespace(evaluate_enabled=True, evaluate_timeout=1.0),
        require_client=lambda: _Client(),
        dialog_since=lambda _seq: None,
        mark_dirty=lambda: events.append("dirty"),
        note_success=lambda _tid: None,
        journal=SimpleNamespace(
            record=lambda **kw: events.append(f"{kw.get('phase')}:{kw.get('status')}"),
        ),
    )
    with pytest.raises(BrowserError):
        asyncio.run(evaluate.run(session, {"expression": "1+1", "await_promise": False}))
    assert any(e.startswith("prepared:") for e in events)
    assert any(e.startswith("result:uncertain") for e in events)
    assert "dirty" in events


def test_r13_snapshot_since_falls_back_to_full_on_signature_change_and_tracks_visibility() -> None:
    n1 = FusedNode(
        1,
        "f1",
        "t1",
        "link",
        "Home",
        "a",
        0,
        (0, 0, 50, 20),
        ref=1,
        epoch=1,
        visible=True,
        in_viewport=True,
        interactive=True,
        href="/a",
    )
    n2 = FusedNode(
        1,
        "f1",
        "t1",
        "link",
        "Home",
        "a",
        0,
        (0, 0, 50, 20),
        ref=1,
        epoch=1,
        visible=True,
        in_viewport=False,
        interactive=True,
        href="/b",
    )
    assert snapshot_action._node_delta_state(n1) != snapshot_action._node_delta_state(n2)


def test_r14_macos_commands_and_key_specs() -> None:
    assert press_key.parse_key("Ctrl++") == ("+", "Equal", 187, None, 2)
    assert press_key.parse_key("Shift+1") == ("!", "Digit1", 49, "!", 8)
    assert press_key.parse_key("!") == ("!", "Digit1", 49, "!", 0)
    assert press_key._MAC_COMMANDS[(0, "Backspace")] == ["deleteBackward"]
    assert press_key._MAC_COMMANDS[(4, "a")] == ["selectAll"]
    assert press_key._MAC_COMMANDS[(4, "c")] == ["copy"]


def test_r11_scoring_uses_text_hint_fallback() -> None:
    node = FusedNode(1, "f1", "t1", "button", "", "button", 0, (0, 0, 50, 20), interactive=True, text_hint="Save Draft")
    ranked = scoring.rank([node], "Save Draft")
    assert ranked and ranked[0].node is node
