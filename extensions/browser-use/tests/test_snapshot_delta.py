# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio

from icode_browser_use.actions import snapshot
from icode_browser_use.config import BrowserConfig
from icode_browser_use.session import BrowserSession
from icode_browser_use.snapshot.fusion import FusedNode


def _node(
    name: str,
    backend: int,
    *,
    in_viewport: bool = True,
    parent: int = 0,
    ancestors: tuple[int, ...] = (),
    tag: str = "button",
    role: str = "button",
    interactive: bool = True,
) -> FusedNode:
    return FusedNode(
        backend_node_id=backend,
        frame_id="frame",
        target_id="target",
        role=role,
        name=name,
        tag=tag,
        nth=0,
        bbox=(0, 0, 10, 10),
        in_viewport=in_viewport,
        visible=True,
        interactive=interactive,
        parent_backend_id=parent,
        ancestor_ids=ancestors,
    )


def _session(nodes: list[FusedNode]) -> BrowserSession:
    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    session.register_snapshot(nodes, url="https://example.com", title="Example")
    return session


def test_snapshot_since_returns_only_changed_nodes(monkeypatch) -> None:
    session = _session([_node("Old", 1)])
    new_nodes = [_node("Old", 1), _node("New", 2)]

    async def fake_capture(_session, **_kwargs):
        _session.register_snapshot(new_nodes, url="https://example.com", title="Example")
        return new_nodes, None

    async def fake_count(_session):
        return 0

    async def fake_title(_session):
        return "Example"

    monkeypatch.setattr(snapshot, "capture_and_register", fake_capture)
    monkeypatch.setattr(snapshot, "_read_mutation_count", fake_count)
    monkeypatch.setattr(snapshot, "_page_title", fake_title)
    monkeypatch.setattr(snapshot, "detect_login", lambda *_args: None)
    blocks = asyncio.run(snapshot.run(session, {"scope": "viewport", "interactive_only": False, "since": 1}))
    text = blocks[0]["text"]
    assert "delta: +1 -0 since version 1" in text
    assert "New" in text
    assert "Old" not in text


def test_snapshot_since_unknown_returns_full(monkeypatch) -> None:
    session = _session([_node("Old", 1)])

    async def fake_capture(_session, **_kwargs):
        _session.register_snapshot([_node("Old", 1)], url="https://example.com", title="Example")
        return list(_session.snapshot.nodes), None

    async def fake_count(_session):
        return 0

    async def fake_title(_session):
        return "Example"

    monkeypatch.setattr(snapshot, "capture_and_register", fake_capture)
    monkeypatch.setattr(snapshot, "_read_mutation_count", fake_count)
    monkeypatch.setattr(snapshot, "_page_title", fake_title)
    monkeypatch.setattr(snapshot, "detect_login", lambda *_args: None)
    blocks = asyncio.run(snapshot.run(session, {"scope": "viewport", "interactive_only": False, "since": 99}))
    text = blocks[0]["text"]
    assert "Old" in text
    assert "delta:" not in text


def test_snapshot_target_serializes_only_its_subtree(monkeypatch) -> None:
    session = _session([_node("Old", 1)])
    nodes = [
        _node("Outside", 1),
        _node("Form", 10),
        _node("Name", 11, parent=10, ancestors=(10,)),
        _node("City", 12, parent=10, ancestors=(10,)),
        _node("", 13, parent=10, ancestors=(10,), tag="div", role="generic", interactive=False),
        _node("Elsewhere", 2),
    ]

    async def fake_capture(_session, **_kwargs):
        _session.register_snapshot(nodes, url="https://example.com", title="Example")
        return list(_session.snapshot.nodes), None

    async def fake_count(_session):
        return 0

    async def fake_title(_session):
        return "Example"

    monkeypatch.setattr(snapshot, "capture_and_register", fake_capture)
    monkeypatch.setattr(snapshot, "_read_mutation_count", fake_count)
    monkeypatch.setattr(snapshot, "_page_title", fake_title)
    monkeypatch.setattr(snapshot, "detect_login", lambda *_args: None)
    blocks = asyncio.run(
        snapshot.run(session, {"scope": "viewport", "interactive_only": False, "target": "Form", "since": 0})
    )
    text = blocks[0]["text"]
    assert "subtree @" in text
    assert "Form" in text and "Name" in text and "City" in text
    assert "Outside" not in text and "Elsewhere" not in text
    assert "interactive 3 / offscreen 0 / structural hidden 1" in text


def test_snapshot_delta_does_not_count_invisible_structural_changes(monkeypatch) -> None:
    structural = _node("", 20, tag="div", role="generic", interactive=False)
    session = _session([structural])

    async def fake_capture(_session, **_kwargs):
        _session.register_snapshot([structural], url="https://example.com", title="Example")
        return [structural], None

    async def fake_count(_session):
        return 1

    async def fake_title(_session):
        return "Example"

    monkeypatch.setattr(snapshot, "capture_and_register", fake_capture)
    monkeypatch.setattr(snapshot, "_read_mutation_count", fake_count)
    monkeypatch.setattr(snapshot, "_page_title", fake_title)
    monkeypatch.setattr(snapshot, "detect_login", lambda *_args: None)
    blocks = asyncio.run(snapshot.run(session, {"scope": "viewport", "interactive_only": False, "since": 1}))
    text = blocks[0]["text"]
    assert "+2 -0" not in text and "+1 -0" not in text
    assert 'div ""' not in text
    assert "structural hidden 1" in text


def test_snapshot_target_that_is_a_frame_says_so(monkeypatch) -> None:
    session = _session([_node("Frame", 10)])
    nodes = [_node("Frame", 10, tag="iframe", role="iframe"), _node("Inside", 11, parent=10, ancestors=(10,))]

    async def fake_capture(_session, **_kwargs):
        _session.register_snapshot(nodes, url="https://example.com", title="Example")
        return list(_session.snapshot.nodes), None

    async def fake_count(_session):
        return 0

    async def fake_title(_session):
        return "Example"

    monkeypatch.setattr(snapshot, "capture_and_register", fake_capture)
    monkeypatch.setattr(snapshot, "_read_mutation_count", fake_count)
    monkeypatch.setattr(snapshot, "_page_title", fake_title)
    monkeypatch.setattr(snapshot, "detect_login", lambda *_args: None)
    blocks = asyncio.run(
        snapshot.run(session, {"scope": "viewport", "interactive_only": False, "target": "Frame", "since": 0})
    )
    text = blocks[0]["text"]
    assert "target is a frame" in text and "use scope=full" in text


def test_scope_full_does_not_mark_cached_nodes_in_viewport(monkeypatch) -> None:
    session = _session([])
    nodes = [_node("Below", 1, in_viewport=False)]

    async def fake_capture(_session, **_kwargs):
        _session.register_snapshot(nodes, url="https://example.com", title="Example")
        return list(_session.snapshot.nodes), None

    async def fake_count(_session):
        return 0

    async def fake_title(_session):
        return "Example"

    monkeypatch.setattr(snapshot, "capture_and_register", fake_capture)
    monkeypatch.setattr(snapshot, "_read_mutation_count", fake_count)
    monkeypatch.setattr(snapshot, "_page_title", fake_title)
    monkeypatch.setattr(snapshot, "detect_login", lambda *_args: None)
    blocks = asyncio.run(snapshot.run(session, {"scope": "full", "interactive_only": False, "since": 0}))
    assert "Below" in blocks[0]["text"]
    # Later free-text ranking reads the cached nodes: their viewport facts must be real.
    assert all(not node.in_viewport for node in session.snapshot.nodes)
