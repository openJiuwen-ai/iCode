# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import actionability, click, extract, fill, hover, wait_for
from icode_browser_use.errors import BadRequest
from icode_browser_use.journal import Journal
from icode_browser_use.snapshot.fusion import FusedNode


class _ShadowClient:
    def __init__(self, *, cdp_hit_backend: int = 10) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.cdp_hit_backend = cdp_hit_backend

    async def send(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        self.calls.append((method, params, session_id))
        if method == "DOM.getNodeForLocation":
            return {"backendNodeId": self.cdp_hit_backend}
        if method == "Runtime.callFunctionOn" and params.get("functionDeclaration") == actionability.HIT_TEST_FN:
            return {
                "result": {
                    "value": {
                        "inside": False,
                        "clipper": False,
                        "shadowRetargeted": True,
                        "tag": "BILI-COMMENTS",
                        "id": "",
                    }
                }
            }
        return {"result": {"value": None}}


def _node(backend_id: int, *, role: str = "button", name: str = "确定", tag: str = "button", ancestors=()):
    return FusedNode(
        backend_id,
        "f1",
        "t1",
        role,
        name,
        tag,
        0,
        (10.0, 10.0, 80.0, 30.0),
        ref=backend_id,
        epoch=5,
        ancestor_ids=tuple(ancestors),
    )


class _ShadowSession:
    session_id = "main"
    target_id = "t1"
    page_epoch = 5
    dialog_seq = 0
    cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0)

    def __init__(self, *, cdp_hit_backend: int = 10, nodes: list[FusedNode] | None = None) -> None:
        self.client = _ShadowClient(cdp_hit_backend=cdp_hit_backend)
        self.journal = Journal(None)
        self.successes = 0
        self.marked = 0
        self.snapshot = SimpleNamespace(nodes=nodes or [_node(10)])

    def dialog_since(self, _seq):
        return None

    def resolve_target(self, _target):
        return SimpleNamespace(
            token="@e10#5",
            backend_node_id=10,
            frame_id="f1",
            cdp_session_id=None,
        )

    async def resolve_object_id(self, _entry):
        return "obj-10"

    def require_client(self):
        return self.client

    def oopif_offset(self, _session_id):
        return (0.0, 0.0)

    def mark_dirty(self) -> None:
        self.marked += 1

    def note_success(self, _target_id) -> None:
        self.successes += 1


def test_cdp_hit_test_accepts_exact_target_and_descendant_rejects_occluder() -> None:
    target_entry = SimpleNamespace(backend_node_id=10, frame_id="f1")
    child = _node(11, role="generic", name="icon", tag="span", ancestors=(1, 10))
    occluder = _node(99, role="generic", name="mask", tag="div", ancestors=(1,))

    s_exact = _ShadowSession(cdp_hit_backend=10, nodes=[_node(10), child, occluder])
    res_exact = asyncio.run(actionability.cdp_hit_test(s_exact, target_entry, 20.0, 20.0, None))
    assert res_exact is not None and res_exact["inside"] is True

    s_child = _ShadowSession(cdp_hit_backend=11, nodes=[_node(10), child, occluder])
    res_child = asyncio.run(actionability.cdp_hit_test(s_child, target_entry, 20.0, 20.0, None))
    assert res_child is not None and res_child["inside"] is True

    s_occ = _ShadowSession(cdp_hit_backend=99, nodes=[_node(10), child, occluder])
    res_occ = asyncio.run(actionability.cdp_hit_test(s_occ, target_entry, 20.0, 20.0, None))
    assert res_occ is not None and res_occ["inside"] is False and res_occ["tag"] == "DIV"


def test_click_resolves_shadow_retargeted_hit_and_observes_shadow_mutation(monkeypatch) -> None:
    session = _ShadowSession(cdp_hit_backend=10)
    facts_seq = iter(
        [
            {
                "tag": "button",
                "role": "button",
                "name": "展开",
                "type": "button",
                "inShadow": True,
                "rootChildCount": 5,
                "contextLength": 10,
                "disabled": False,
                "ariaDisabled": False,
                "classDisabled": False,
            },
            {
                "tag": "button",
                "role": "button",
                "name": "展开",
                "type": "button",
                "inShadow": True,
                "rootChildCount": 8,
                "contextLength": 24,
                "disabled": False,
                "ariaDisabled": False,
                "classDisabled": False,
            },
        ]
    )

    async def fake_facts(*_args, **_kwargs):
        return next(facts_seq)

    async def fake_page(*_args, **_kwargs):
        # Light-DOM mutations stay unchanged even though the shadow root mutated.
        return {"href": "https://example.com", "mutations": 0, "expanded": 0, "dialogs": 0, "active": "bili-comments"}

    async def fake_hit_point(*_args, **_kwargs):
        return 50.0, 25.0

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(click, "_target_facts", fake_facts)
    monkeypatch.setattr(click, "_page_state", fake_page)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_sleep", no_sleep)

    blocks = asyncio.run(click.run(session, {"target": "@e10#5", "button": "left", "click_count": 1}))
    text = blocks[0]["text"]
    assert "已点击 @e10#5" in text
    assert "effect=dom" in text
    assert "status=succeeded" in text
    methods = [m for m, _, _ in session.client.calls]
    assert "DOM.getNodeForLocation" in methods
    assert methods.count("Input.dispatchMouseEvent") == 3


def test_hover_resolves_shadow_retargeted_hit_via_cdp(monkeypatch) -> None:
    session = _ShadowSession(cdp_hit_backend=10)

    async def fake_hit_point(*_args, **_kwargs):
        return 50.0, 25.0

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(hover, "_hit_point", fake_hit_point)
    monkeypatch.setattr(hover, "_sleep", no_sleep)
    blocks = asyncio.run(hover.run(session, {"target": "@e10#5"}))
    assert "已悬停 @e10#5" in blocks[0]["text"]
    methods = [m for m, _, _ in session.client.calls]
    assert "DOM.getNodeForLocation" in methods
    assert "Input.dispatchMouseEvent" in methods


def test_fill_focus_failure_includes_shadow_and_layout_diagnostics(monkeypatch) -> None:
    session = _ShadowSession()

    async def fake_call_value(*_args, **_kwargs):
        return {
            "value": {
                "focused": False,
                "tag": "div",
                "editable": False,
                "hasLayout": False,
                "inShadow": True,
                "readOnly": False,
                "disabled": False,
            }
        }

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    with pytest.raises(BadRequest) as exc_info:
        asyncio.run(
            fill.run(
                session,
                {"target": "@e10#5", "text": "hi", "clear": True, "submit": False, "keypress_fallback": False},
            )
        )
    msg = str(exc_info.value)
    assert "无法聚焦目标元素" in msg
    assert "tag=div" in msg
    assert "no-layout" in msg
    assert "in-shadow" in msg


def test_extract_element_falls_back_to_snapshot_for_closed_shadow_host(monkeypatch) -> None:
    host = _node(10, role="bili-comment-box", name="", tag="bili-comment-box")
    child1 = _node(11, role="heading", name="评论 3", tag="h2", ancestors=(10,))
    child2 = _node(12, role="button", name="发布", tag="button", ancestors=(10,))
    session = _ShadowSession(nodes=[host, child1, child2])

    async def fake_call_value(*_args, **_kwargs):
        return {"value": ""}

    monkeypatch.setattr(extract, "call_value", fake_call_value)
    blocks = asyncio.run(extract.run(session, {"query": "@e10#5", "format": "text", "max_chars": 2000, "offset": 0}))
    text = blocks[0]["text"]
    assert "[shadow DOM text from snapshot]" in text
    assert "评论 3" in text
    assert "发布" in text


def test_shadow_probes_pierce_root_and_observe_shadow_root() -> None:
    assert "getRootNode" in actionability.HIT_TEST_FN
    assert "elementFromPoint" in actionability.HIT_TEST_FN
    assert "root.activeElement" in actionability.FOCUS_FN
    assert "root.activeElement" in actionability.FIELD_STATE_FN
    assert "this.shadowRoot" in wait_for._STABLE_NODE_FN
    assert "shadowRoot" in extract._DEEP_TEXT_JS
