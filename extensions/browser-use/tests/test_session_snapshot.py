# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import StaleRef
from icode_browser_use.session import BrowserSession
from icode_browser_use.snapshot.fusion import FusedNode


def node(*, name: str, backend: int, cdp_session_id: str | None = None) -> FusedNode:
    return FusedNode(
        backend_node_id=backend,
        frame_id="frame",
        target_id="target",
        role="button",
        name=name,
        tag="button",
        nth=0,
        bbox=(0.0, 0.0, 10.0, 10.0),
        in_viewport=True,
        visible=True,
        interactive=True,
        cdp_session_id=cdp_session_id,
    )


def make_session() -> BrowserSession:
    return BrowserSession(BrowserConfig(allowed_domains=("example.com",)))


def test_snapshot_reuses_refs_for_unchanged_nodes() -> None:
    session = make_session()
    first = node(name="A", backend=1)
    session.register_snapshot([first], url="https://example.com", title="Example")
    version = session.snapshot.version
    session.register_snapshot([node(name="A", backend=1)], url="https://example.com", title="Example")
    assert session.snapshot.version == version + 1
    assert session.resolve_target("@e1#" + str(session.page_epoch)).backend_node_id == 1


def test_snapshot_removed_node_ref_is_stale() -> None:
    session = make_session()
    session.register_snapshot([node(name="A", backend=1)], url="https://example.com", title="Example")
    token = "@e1#" + str(session.page_epoch)
    session.register_snapshot([], url="https://example.com", title="Example")
    with pytest.raises(StaleRef):
        session.resolve_target(token)


def test_a_removed_nodes_index_is_never_reused_within_the_epoch() -> None:
    """A token must not come to name a different node: a new node never inherits a vanished index."""
    session = make_session()
    session.register_snapshot([node(name="A", backend=1), node(name="B", backend=2)], url="https://e.com", title="")
    session.register_snapshot([node(name="A", backend=1)], url="https://e.com", title="")
    newcomer = node(name="C", backend=3)
    session.register_snapshot([node(name="A", backend=1), newcomer], url="https://e.com", title="")
    assert newcomer.ref == 3
    with pytest.raises(StaleRef):
        session.resolve_target("@e2#" + str(session.page_epoch))


def test_child_nodes_get_refs_with_cdp_session() -> None:
    session = make_session()
    parent = node(name="Parent", backend=1)
    child = node(name="Inside", backend=2, cdp_session_id="child-session")

    session.register_snapshot([parent, child], url="https://example.com", title="Example")

    assert parent.ref == 1
    assert child.ref == 2
    assert session._ref_map[2].backend_node_id == 2
    assert session._ref_map[2].cdp_session_id == "child-session"


def test_text_resolution_can_target_child_nodes() -> None:
    session = make_session()
    session.register_snapshot(
        [
            node(name="Parent", backend=1),
            node(name="Unique Child", backend=2, cdp_session_id="child-session"),
        ],
        url="https://example.com",
        title="Example",
    )
    entry = session.resolve_target("Unique Child")
    assert entry.cdp_session_id == "child-session"


def test_oopif_sessions_are_tracked_and_removed() -> None:
    import asyncio

    session = make_session()
    params = {"targetInfo": {"type": "iframe", "targetId": "target-1"}, "sessionId": "child-session"}

    asyncio.run(session._on_attached_to_target(params, None))
    assert session.oopif_sessions == ["child-session"]

    asyncio.run(session._on_detached_from_target({"sessionId": "child-session"}, None))
    assert session.oopif_sessions == []


def test_nested_oopif_parent_tracking_and_detach_cascade() -> None:
    import asyncio

    session = make_session()
    root = {"targetInfo": {"type": "iframe", "targetId": "t1"}, "sessionId": "child-1"}
    nested = {"targetInfo": {"type": "iframe", "targetId": "t2"}, "sessionId": "child-2"}

    asyncio.run(session._on_attached_to_target(root, "main"))
    asyncio.run(session._on_attached_to_target(nested, "child-1"))

    assert session.oopif_parent("child-1") is None
    assert session.oopif_parent("child-2") == "child-1"
    assert session.oopif_children("child-1") == ["child-2"]

    asyncio.run(session._on_detached_from_target({"sessionId": "child-1"}, None))
    assert session.oopif_sessions == []
    assert session.oopif_parent("child-2") is None
