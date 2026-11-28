# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from icode_browser_use.actions import evaluate
from icode_browser_use.journal import Journal


@pytest.mark.parametrize(
    ("result", "exception", "expected"),
    [
        (
            {"description": "Error: boom"},
            {"text": "Uncaught", "exception": {"description": "Error: boom"}},
            "Error: boom",
        ),
        ({}, {"text": "Error: boom", "exception": {"description": "Error: boom"}}, "Error: boom"),
        (
            {"description": "Error: boom"},
            {"exception": {"description": "Error: boom"}, "stackTrace": {"callFrames": [{}]}},
            "Error: boom\n(stack trace omitted)",
        ),
        ({"description": "Uncaught"}, {"exception": {"description": "Error: boom"}}, "Uncaught\nError: boom"),
        ({}, {}, "未知页面异常"),
    ],
)
def test_exception_descriptions_are_unique_but_distinct_details_remain(result, exception, expected):
    client = SimpleNamespace(send=AsyncMock(return_value={"result": result, "exceptionDetails": exception}))
    session = SimpleNamespace(
        cfg=SimpleNamespace(evaluate_enabled=True, evaluate_timeout=10, snapshot_char_budget=1000),
        session_id="main",
        target_id="target",
        page_epoch=3,
        journal=Journal(None),
        require_client=lambda: client,
        note_success=Mock(),
    )
    blocks = asyncio.run(evaluate.run(session, {"expression": "throw Error('boom')", "await_promise": False}))
    assert blocks[0]["text"] == "页面异常（未抛错，按结果文本呈现）：\n" + expected
    assert client.send.await_count == 1
    session.note_success.assert_called_once_with("target")


def test_evaluate_records_prepared_before_the_send_and_keeps_the_expression_out(monkeypatch) -> None:
    phases: list[str] = []
    journal = Journal(None)

    async def observing_send(*_args, **_kwargs):
        row = journal.lookup("fixed")
        phases.append(str(row["phase"]) if row else "missing")
        return {"result": {"value": 1}}

    session = SimpleNamespace(
        cfg=SimpleNamespace(evaluate_enabled=True, evaluate_timeout=10, snapshot_char_budget=1000),
        session_id="main",
        target_id="target",
        page_epoch=4,
        journal=journal,
        require_client=lambda: SimpleNamespace(send=observing_send),
        note_success=Mock(),
    )
    monkeypatch.setattr(evaluate, "new_action_id", lambda: "fixed")
    asyncio.run(
        evaluate.run(
            session,
            {"expression": "localStorage.token = 'SENTINEL-TOKEN'", "await_promise": False},
        )
    )
    assert phases == ["prepared"]
    row = journal.lookup("fixed")
    assert row is not None
    assert row["phase"] == "result"
    assert row["status"] == "succeeded"
    assert "SENTINEL-TOKEN" not in str(row["evidence"])


def test_evaluate_page_exception_is_recorded_as_uncertain(monkeypatch) -> None:
    journal = Journal(None)
    client = SimpleNamespace(
        send=AsyncMock(return_value={"result": {}, "exceptionDetails": {"exception": {"description": "boom"}}})
    )
    session = SimpleNamespace(
        cfg=SimpleNamespace(evaluate_enabled=True, evaluate_timeout=10, snapshot_char_budget=1000),
        session_id="main",
        target_id="target",
        page_epoch=4,
        journal=journal,
        require_client=lambda: client,
        note_success=Mock(),
    )
    monkeypatch.setattr(evaluate, "new_action_id", lambda: "fixed")
    asyncio.run(evaluate.run(session, {"expression": "throw Error('boom')", "await_promise": False}))
    row = journal.lookup("fixed")
    assert row is not None
    assert row["status"] == "uncertain"
    assert "page_exception" in str(row["evidence"])
