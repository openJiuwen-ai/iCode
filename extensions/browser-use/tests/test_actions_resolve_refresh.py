# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``_shared.resolve_target``: free text refreshes a stale snapshot, ``@eN#epoch`` never does."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from browser_use.actions import _shared
from browser_use.errors import StaleRef
from browser_use.snapshot.fusion import FusedNode


def _node(epoch: int) -> FusedNode:
    return FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=epoch)


def _stale_session() -> SimpleNamespace:
    session = SimpleNamespace(page_epoch=7, snapshot=SimpleNamespace(nodes=[_node(6)]), calls=[])

    def resolve_target(spec):
        session.calls.append(("resolve", spec))
        if spec.startswith("@") and not spec.endswith(f"#{session.page_epoch}"):
            raise StaleRef("引用已失效（页面已变化），请重新 snapshot")
        return SimpleNamespace(token="@e1#7", cdp_session_id=None)

    session.resolve_target = resolve_target
    return session


def _patch_capture(monkeypatch: pytest.MonkeyPatch, session: SimpleNamespace) -> None:
    async def fake_capture(_session, **kwargs):
        session.calls.append(("capture", kwargs))
        _session.snapshot.nodes = [_node(_session.page_epoch)]
        return _session.snapshot.nodes, None

    monkeypatch.setattr(_shared, "capture_and_register", fake_capture)


def test_free_text_refreshes_a_stale_snapshot_before_resolving(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _stale_session()
    _patch_capture(monkeypatch, session)

    entry = asyncio.run(_shared.resolve_target(session, "评论"))

    assert session.calls == [("capture", {}), ("resolve", "评论")]
    assert entry.token == "@e1#7"
    assert not _shared.snapshot_is_stale(session)


def test_free_text_on_a_fresh_snapshot_resolves_without_capturing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _stale_session()
    session.snapshot.nodes = [_node(7)]
    _patch_capture(monkeypatch, session)

    asyncio.run(_shared.resolve_target(session, "评论"))

    assert session.calls == [("resolve", "评论")]


def test_a_stale_ref_is_not_refreshed_and_still_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _stale_session()
    _patch_capture(monkeypatch, session)

    with pytest.raises(StaleRef):
        asyncio.run(_shared.resolve_target(session, "@e1#6"))

    assert session.calls == [("resolve", "@e1#6")]
    assert _shared.snapshot_is_stale(session)


def test_a_capture_failure_propagates_before_resolving(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _stale_session()

    async def failing_capture(_session, **kwargs):
        raise StaleRef("navigation in flight")

    monkeypatch.setattr(_shared, "capture_and_register", failing_capture)

    with pytest.raises(StaleRef):
        asyncio.run(_shared.resolve_target(session, "评论"))

    assert session.calls == []
