# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import find
from icode_browser_use.errors import BadRequest
from icode_browser_use.snapshot.fusion import FusedNode


def _node(role: str, name: str, ref: int) -> FusedNode:
    return FusedNode(1, "f", "t", role, name, "button", 0, (0, 0, 10, 10), ref=ref, epoch=7)


def test_find_matches_name_and_role() -> None:
    session = SimpleNamespace(
        page_epoch=7,
        snapshot=SimpleNamespace(nodes=[_node("button", "Sign in", 1), _node("link", "Help", 2)]),
    )
    blocks = asyncio.run(find.run(session, {"query": "sign", "role": "", "limit": 20}))
    assert "@e1#7" in blocks[0]["text"]
    assert "Sign in" in blocks[0]["text"]
    assert "Help" not in blocks[0]["text"]


def test_find_role_filter_and_limit() -> None:
    nodes = [_node("button", "Go one", 1), _node("button", "Go two", 2), _node("link", "Go three", 3)]
    session = SimpleNamespace(page_epoch=7, snapshot=SimpleNamespace(nodes=nodes))
    blocks = asyncio.run(find.run(session, {"query": "go", "role": "button", "limit": 1}))
    text = blocks[0]["text"]
    assert "Go one" in text
    assert "Go two" not in text
    assert "truncated" in text


def test_find_refreshes_a_stale_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from icode_browser_use.actions import snapshot as snapshot_action

    stale = _node("button", "Old", 1)
    stale.epoch = 7
    fresh = _node("button", "New", 2)
    fresh.epoch = 8
    session = SimpleNamespace(
        page_epoch=8,
        snapshot=SimpleNamespace(nodes=[stale]),
    )

    async def fake_snapshot(_session, _arguments):
        _session.snapshot.nodes = [fresh]
        return []

    monkeypatch.setattr(snapshot_action, "run", fake_snapshot)
    blocks = asyncio.run(find.run(session, {"query": "new", "role": "", "limit": 20}))
    assert "@e2#8" in blocks[0]["text"]
    assert "New" in blocks[0]["text"]


@pytest.mark.parametrize(("dirty", "fresh"), [(True, False), (False, True)])
def test_find_refreshes_in_the_same_epoch(monkeypatch: pytest.MonkeyPatch, dirty: bool, fresh: bool) -> None:
    from icode_browser_use.actions import snapshot as snapshot_action

    session = SimpleNamespace(
        page_epoch=7,
        snapshot_dirty=dirty,
        snapshot=SimpleNamespace(nodes=[_node("button", "3456 Likes. Like", 1)]),
    )

    async def fake_snapshot(session, _arguments):
        session.snapshot.nodes = [_node("button", "3457 Likes. Liked", 1)]
        return []

    monkeypatch.setattr(snapshot_action, "run", fake_snapshot)
    text = asyncio.run(find.run(session, {"query": "Liked", "role": "button", "fresh": fresh}))[0]["text"]
    assert "3457 Likes. Liked" in text


def test_find_matches_href_of_an_icon_only_link() -> None:
    icon_link = FusedNode(1, "f", "t", "link", "/chat/c1", "a", 0, (0, 0, 10, 10), ref=3, epoch=7, href="/chat/c1")
    named = FusedNode(1, "f", "t", "link", "Trip planning", "a", 0, (0, 0, 10, 10), ref=4, epoch=7, href="/chat/c2")
    session = SimpleNamespace(
        page_epoch=7, snapshot=SimpleNamespace(nodes=[icon_link, named, _node("button", "Send", 5)])
    )

    text = asyncio.run(find.run(session, {"query": "c2", "role": "", "limit": 20}))[0]["text"]

    assert "@e4#7" in text
    assert "@e3#7" not in text
    assert "@e5#7" not in text


def test_find_requires_query() -> None:
    with pytest.raises(BadRequest):
        asyncio.run(find.run(SimpleNamespace(snapshot=SimpleNamespace(nodes=[])), {"query": ""}))


def test_matching_nodes_is_case_insensitive_and_filters_by_role_and_href() -> None:
    sign_in = _node("button", "Sign In", 1)
    help_link = FusedNode(2, "f", "t", "link", "Help", "a", 0, (0, 0, 10, 10), ref=2, epoch=7, href="/account/help")
    other = _node("link", "Pricing", 3)
    nodes = [sign_in, help_link, other]

    assert find.matching_nodes(nodes, "sign in") == [sign_in]
    assert find.matching_nodes(nodes, "SIGN") == [sign_in]
    assert find.matching_nodes(nodes, "sign", role="link") == []
    assert find.matching_nodes(nodes, "ACCOUNT") == [help_link]  # href match
    assert find.matching_nodes(nodes, "p", role="LINK") == [help_link, other]  # role is matched case-insensitively
    assert find.matching_nodes(nodes, "nothing") == []
    assert find.matching_nodes([], "sign") == []


def test_find_surfaces_actionability_flags_and_ranks_visible_editable_first() -> None:
    wrapper = FusedNode(
        1, "f", "t", "generic", "评论框", "div", 0, (0, 0, 0, 0), ref=1, epoch=7, visible=False, has_layout=False
    )
    editor = FusedNode(
        2,
        "f",
        "t",
        "generic",
        "",
        "div",
        1,
        (10, 20, 300, 40),
        ref=2,
        epoch=7,
        visible=True,
        in_viewport=True,
        interactive=True,
        focusable=True,
        editable=True,
        placeholder="写一条评论框内容",
        has_layout=True,
    )
    session = SimpleNamespace(page_epoch=7, snapshot=SimpleNamespace(nodes=[wrapper, editor]))
    text = asyncio.run(find.run(session, {"query": "评论框", "role": "", "limit": 20}))[0]["text"]
    lines = text.splitlines()
    assert "@e2#7" in lines[1]
    assert "[editable focusable in-view]" in lines[1]
    assert 'placeholder="写一条评论框内容"' in lines[1]
    assert "@e1#7" in lines[2]
    assert "[no-layout]" in lines[2]

    # role=textbox also matches contenteditable editors whose AX role stayed generic.
    tb = asyncio.run(find.run(session, {"query": "评论框", "role": "textbox", "limit": 20}))[0]["text"]
    assert "@e2#7" in tb
    assert "@e1#7" not in tb
