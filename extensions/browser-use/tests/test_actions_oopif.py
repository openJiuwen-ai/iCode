# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from icode_browser_use.actions._shared import (
    call_value,
    ensure_oopif_domains,
    eval_value,
    resolve_oopif_offset,
)
from icode_browser_use.config import BrowserConfig
from icode_browser_use.session import BrowserSession


class FakeClient:
    closed = False

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
    session.register_oopif("child", "target-child")
    session.set_oopif_offset("child", (10.0, 20.0))
    assert session.oopif_offset("child") == (10.0, 20.0)
    assert session.oopif_offset("missing") == (0.0, 0.0)


def test_late_oopif_cache_updates_do_not_reregister_detached_session() -> None:
    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    session.register_oopif("child-1", "target-if-1")
    session._forget_oopif("child-1")
    assert session.oopif_sessions == []

    session.mark_oopif_enabled("child-1")
    session.set_oopif_frame_id("child-1", "frame-1")
    session.set_oopif_offset("child-1", (12.0, 34.0))

    assert session.oopif_sessions == []
    assert session.is_oopif_enabled("child-1") is False
    assert session.oopif_frame_id("child-1") == ""
    assert session.oopif_offset("child-1") == (0.0, 0.0)


def test_ensure_oopif_domains_caches_per_session_and_invalidates_on_detach_or_disconnect() -> None:
    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    client = FakeClient()
    session._client = client
    session.register_oopif("child-1", "target-if-1")

    async def scenario() -> None:
        await ensure_oopif_domains(session, "child-1")
        assert len(client.calls) == 4
        # Second call for the same attached OOPIF sends zero enable round-trips.
        await ensure_oopif_domains(session, "child-1")
        assert len(client.calls) == 4

        # Detaching invalidates the cache so a re-attached session re-initializes.
        session._forget_oopif("child-1")
        session.register_oopif("child-1", "target-if-1")
        await ensure_oopif_domains(session, "child-1")
        assert len(client.calls) == 8

        # Disconnecting also clears cached OOPIF domain state.
        session.mark_disconnected()
        session._client = client
        session._state = "ready"
        await ensure_oopif_domains(session, "child-1")
        assert len(client.calls) == 12

    asyncio.run(scenario())


def test_resolve_oopif_offset_sums_live_nested_chain() -> None:
    class ChainClient:
        closed = False

        async def send(self, method: str, params=None, session_id=None, timeout=None):
            params = dict(params or {})
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"id": f"frame-{session_id}"}}}
            if method == "DOM.getFrameOwner":
                fid = params["frameId"]
                return {"backendNodeId": 101 if fid == "frame-outer" else 202}
            if method == "DOM.resolveNode":
                bid = params["backendNodeId"]
                return {"object": {"objectId": f"obj-{bid}"}}
            if method == "Runtime.callFunctionOn":
                oid = params["objectId"]
                return {"result": {"value": {"x": 30.0, "y": 140.0} if oid == "obj-101" else {"x": 15.0, "y": 25.0}}}
            return {}

    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    session._client = ChainClient()
    session._session_id = "main"
    session.register_oopif("outer", "t-outer")
    session.register_oopif("inner", "t-inner", parent_session_id="outer")
    session.set_oopif_offset("inner", (999.0, 999.0))  # stale pre-scroll cache

    offset = asyncio.run(resolve_oopif_offset(session, "inner"))
    assert offset == (45.0, 165.0)
    assert session.oopif_offset("outer") == (30.0, 140.0)
    assert session.oopif_offset("inner") == (45.0, 165.0)


def test_scrolled_oopif_click_and_hover_use_post_scroll_live_offset() -> None:
    from icode_browser_use.actions import click, hover
    from icode_browser_use.journal import Journal

    class ScrolledOopifClient:
        closed = False

        def __init__(self) -> None:
            self.scrolled = False
            self.mouse_points: list[tuple[str, float, float]] = []
            self.hit_args: list[tuple[float, float]] = []

        async def send(self, method: str, params=None, session_id=None, timeout=None):
            params = dict(params or {})
            if method == "DOM.scrollIntoViewIfNeeded":
                self.scrolled = True
                return {}
            if method == "Page.getFrameTree" and session_id == "oopif-1":
                return {"frameTree": {"frame": {"id": "frame-oopif-1"}}}
            if method == "DOM.getFrameOwner" and session_id == "main":
                return {"backendNodeId": 500}
            if method == "DOM.resolveNode" and session_id == "main":
                return {"object": {"objectId": "owner-iframe"}}
            if method == "DOM.getContentQuads" and session_id == "oopif-1":
                return {"quads": [[10.0, 20.0, 90.0, 20.0, 90.0, 60.0, 10.0, 60.0]]}
            if method == "Runtime.callFunctionOn":
                if params.get("objectId") == "owner-iframe":
                    return {"result": {"value": {"x": 30.0, "y": 200.0 if self.scrolled else 1200.0}}}
                fn = str(params.get("functionDeclaration") or "")
                if "elementFromPoint" in fn:
                    args = params.get("arguments") or []
                    self.hit_args.append((float(args[0]["value"]), float(args[1]["value"])))
                    return {"result": {"value": {"inside": True, "clipper": False, "shadowRetargeted": False}}}
                return {"result": {"value": {"connected": True, "tag": "button", "name": "Go", "disabled": False}}}
            if method == "Runtime.evaluate":
                return {"result": {"value": {"href": "https://example.com", "mutations": 1}}}
            if method == "Input.dispatchMouseEvent":
                self.mouse_points.append((str(params["type"]), float(params["x"]), float(params["y"])))
                return {}
            return {}

    async def scenario() -> None:
        client = ScrolledOopifClient()
        session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
        session._client = client
        session._session_id = "main"
        session._target_id = "t1"
        session._journal = Journal(None)
        session.register_oopif("oopif-1", "target-if")
        session.set_oopif_offset("oopif-1", (30.0, 1200.0))  # stale pre-scroll offset
        entry = SimpleNamespace(
            token="@e1#1",
            cdp_session_id="oopif-1",
            backend_node_id=9,
            bbox=(40.0, 1220.0, 80.0, 40.0),
        )
        session.resolve_target = lambda _spec: entry  # type: ignore[method-assign]

        async def resolve_obj(_entry):
            return "btn-obj"

        session.resolve_object_id = resolve_obj  # type: ignore[method-assign]
        session._snapshot = SimpleNamespace(nodes=[SimpleNamespace(epoch=session.page_epoch)])

        await hover.run(session, {"target": "@e1#1"})
        # Button centre in iframe is (50, 40); live iframe offset is (30, 200) -> (80, 240) (NOT 1240!).
        assert client.hit_args[-1] == (50.0, 40.0)
        assert ("mouseMoved", 80.0, 240.0) in client.mouse_points

        client.scrolled = False
        client.mouse_points.clear()
        client.hit_args.clear()
        session.set_oopif_offset("oopif-1", (30.0, 1200.0))
        await click.run(session, {"target": "@e1#1", "button": "left", "click_count": 1, "dispatch": "mouse"})
        assert client.hit_args[-1] == (50.0, 40.0)
        assert ("mousePressed", 80.0, 240.0) in client.mouse_points
        assert ("mouseReleased", 80.0, 240.0) in client.mouse_points

    asyncio.run(scenario())
