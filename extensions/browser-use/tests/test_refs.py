# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio

import pytest

from browser_use.config import BrowserConfig
from browser_use.errors import BadRequest, StaleRef
from browser_use.session import BrowserSession, RefEntry


def make_session() -> BrowserSession:
    return BrowserSession(BrowserConfig(allowed_domains=("example.com",)))


def make_entry(*, index: int = 1, epoch: int = 7) -> RefEntry:
    return RefEntry(
        index=index,
        epoch=epoch,
        fingerprint={},
        backend_node_id=10,
        frame_id="frame",
        target_id="target",
        bbox=(0.0, 0.0, 10.0, 10.0),
        text_hint="Submit",
    )


def test_ref_token_spelling() -> None:
    assert make_entry().token == "@e1#7"


def test_resolves_matching_epoch() -> None:
    session = make_session()
    session._page_epoch = 7
    session._ref_map = {1: make_entry()}
    assert session.resolve_target("@e1#7").backend_node_id == 10


def test_rejects_stale_epoch() -> None:
    session = make_session()
    session._page_epoch = 8
    session._ref_map = {1: make_entry(epoch=7)}
    with pytest.raises(StaleRef):
        session.resolve_target("@e1#7")


def test_rejects_unknown_index() -> None:
    session = make_session()
    session._page_epoch = 7
    with pytest.raises(StaleRef):
        session.resolve_target("@e2#7")


def test_a_bare_ref_names_the_exact_current_token() -> None:
    """Models drop the #epoch; the refusal must hand back the token to copy, not just the format."""
    session = make_session()
    session._page_epoch = 7
    session._ref_map = {1: make_entry()}
    with pytest.raises(BadRequest) as exc_info:
        session.resolve_target("@e1")
    assert "@e1#7" in str(exc_info.value)
    assert "缺少世代" in str(exc_info.value)


def test_a_bare_ref_that_is_not_in_the_snapshot_asks_for_a_new_one() -> None:
    session = make_session()
    session._page_epoch = 7
    with pytest.raises(BadRequest) as exc_info:
        session.resolve_target("@e9")
    assert "不在当前快照" in str(exc_info.value)


def test_rejects_malformed_ref() -> None:
    session = make_session()
    with pytest.raises(BadRequest) as exc_info:
        session.resolve_target("@e")
    assert "@e序号#世代" in str(exc_info.value)


class LiveClient:
    """Just enough CDP: ``DOM.resolveNode`` plus the live-check call."""

    closed = False

    def __init__(self, value: dict, ax_nodes: list[dict] | None = None) -> None:
        self.value = value
        self.ax_nodes = ax_nodes or []

    async def send(self, method, params=None, session_id=None, timeout=None):
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "Accessibility.getPartialAXTree":
            assert params == {"backendNodeId": 10, "fetchRelatives": False}
            return {"nodes": self.ax_nodes}
        return {"result": {"value": dict(self.value)}}


def live_session(value: dict, fingerprint: dict) -> tuple[BrowserSession, RefEntry]:
    session = make_session()
    session._client = LiveClient(value)
    session._session_id = "main"
    entry = make_entry()
    entry.fingerprint = fingerprint
    return session, entry


def test_a_select_keeps_its_ref_when_its_options_text_differs_from_its_name() -> None:
    session, entry = live_session(
        {"ok": True, "tag": "select", "name": "Alpha Beta Gamma"},
        {"tag": "select", "name": "Choice"},
    )
    assert asyncio.run(session.resolve_object_id(entry)) == "obj"


def test_a_same_tag_element_with_different_text_is_stale() -> None:
    session, entry = live_session(
        {"ok": True, "tag": "button", "name": "Delete"},
        {"tag": "button", "name": "Save"},
    )
    with pytest.raises(StaleRef):
        asyncio.run(session.resolve_object_id(entry))


@pytest.mark.parametrize("actual", ["519 Likes. Like", "519 Likes. Unlike"])
def test_like_ref_tracks_the_action_when_the_count_changes(actual: str) -> None:
    session, entry = live_session(
        {"ok": True, "tag": "button", "name": actual},
        {"tag": "button", "role": "button", "name": "518 Likes. Like", "name_source": "ax"},
    )
    if actual.endswith(". Like"):
        assert asyncio.run(session.resolve_object_id(entry)) == "obj"
    else:
        with pytest.raises(StaleRef):
            asyncio.run(session.resolve_object_id(entry))


@pytest.mark.parametrize("current_name", ["Author Post title", "Author Other post"])
def test_an_ax_named_article_is_checked_against_its_live_ax_name(current_name: str) -> None:
    session = make_session()
    session._session_id = "main"
    session._client = LiveClient(
        {"ok": True, "tag": "article", "name": "Author Post title Other article contents"},
        [{"name": {"value": current_name}, "backendDOMNodeId": 10}],
    )
    entry = make_entry()
    entry.fingerprint = {"tag": "article", "name": "Author Post title", "name_source": "ax"}
    if current_name == "Author Post title":
        assert asyncio.run(session.resolve_object_id(entry)) == "obj"
    else:
        with pytest.raises(StaleRef):
            asyncio.run(session.resolve_object_id(entry))


def test_a_replaced_tag_is_stale() -> None:
    session, entry = live_session({"ok": True, "tag": "div", "name": ""}, {"tag": "button", "name": ""})
    with pytest.raises(StaleRef):
        asyncio.run(session.resolve_object_id(entry))
