# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import action_status
from icode_browser_use.actions.actionability import mark_dispatched, record_action
from icode_browser_use.errors import BadRequest
from icode_browser_use.journal import Journal


def session(epoch: int = 7):
    return SimpleNamespace(page_epoch=epoch, journal=Journal(None))


def test_action_status_returns_recorded_result() -> None:
    live = session()
    record_action(live, "abc123", "browser_click", "succeeded", "effect=dom")
    blocks = asyncio.run(action_status.run(live, {"action_id": "abc123"}))
    text = blocks[0]["text"]
    assert '"status": "succeeded"' in text
    assert '"phase": "result"' in text
    assert '"document_epoch": 7' in text
    assert '"retryable": false' in text


def test_action_status_unknown() -> None:
    blocks = asyncio.run(action_status.run(session(epoch=1), {"action_id": "missing"}))
    assert "status=unknown" in blocks[0]["text"]


def test_action_status_downgrades_an_unfinished_dispatch() -> None:
    live = session()
    mark_dispatched(live, "half", "browser_click", "dispatched click on @e1#7")
    text = asyncio.run(action_status.run(live, {"action_id": "half"}))[0]["text"]
    assert '"status": "uncertain"' in text
    assert '"phase": "dispatched"' in text
    assert "may or may not have been sent" in text


def test_action_status_requires_a_browser() -> None:
    with pytest.raises(BadRequest):
        asyncio.run(action_status.run(None, {"action_id": "abc"}))
