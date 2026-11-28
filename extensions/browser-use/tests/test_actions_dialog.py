# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import dialog as dialog_action
from icode_browser_use.dialog import DialogInfo
from icode_browser_use.errors import BadRequest
from icode_browser_use.journal import Journal


class FakeSession:
    page_epoch = 5

    def __init__(
        self,
        pending: DialogInfo | None = None,
        *,
        error: Exception | None = None,
        closed: str = "accepted",
        reason: str = "handled by browser_dialog",
        sent: bool = True,
    ) -> None:
        self.journal = Journal(None)
        self.pending = pending
        self.last: DialogInfo | None = None
        self.error = error
        self.closed = closed
        self.reason = reason
        self.sent = sent
        self.calls: list[tuple[bool, str]] = []

    def pending_dialog(self) -> DialogInfo | None:
        return self.pending

    def last_dialog(self) -> DialogInfo | None:
        return self.last or self.pending

    async def handle_dialog(self, *, accept: bool, text: str = "") -> tuple[DialogInfo, bool]:
        if self.error is not None:
            raise self.error
        self.calls.append((accept, text))
        info = self.pending
        assert info is not None
        info.closed = self.closed
        info.reason = self.reason
        self.pending = None
        self.last = info
        return info, self.sent


def fixed_id(monkeypatch) -> None:
    monkeypatch.setattr(dialog_action, "new_action_id", lambda: "fixed")


def test_accepting_the_open_dialog_is_recorded(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(DialogInfo(seq=1, kind="confirm", message="Delete?"))
    blocks = asyncio.run(dialog_action.run(session, {"action": "accept", "text": ""}))
    text = blocks[0]["text"]
    assert session.calls == [(True, "")]
    assert "已接受 confirm 对话框" in text
    assert 'dialog=confirm message="Delete?"' in text
    assert "result=accepted reason=handled by browser_dialog" in text
    assert "status=succeeded" in text
    assert "browser_snapshot" in text
    row = session.journal.lookup("fixed")
    assert row is not None
    assert (row["tool"], row["phase"], row["status"]) == ("browser_dialog", "result", "succeeded")


def test_prompt_answer_is_forwarded_and_counted(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(DialogInfo(seq=2, kind="prompt", message="Name?"))
    blocks = asyncio.run(dialog_action.run(session, {"action": "accept", "text": "Ada"}))
    text = blocks[0]["text"]
    assert session.calls == [(True, "Ada")]
    assert "prompt_text_chars=3" in text


def test_no_dialog_reports_the_last_closing_reason(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession()
    session.last = DialogInfo(
        seq=4,
        kind="alert",
        message="stuck",
        closed="dismissed",
        reason="auto-dismissed after 10s (headless)",
    )
    blocks = asyncio.run(dialog_action.run(session, {"action": "dismiss"}))
    text = blocks[0]["text"]
    assert "当前没有打开的 JavaScript 对话框" in text
    assert "status=blocked retryable=false" in text
    assert "auto-dismissed after 10s (headless)" in text
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked"
    assert session.calls == []


def test_text_for_a_confirm_dialog_is_refused_and_the_dialog_stays(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(
        DialogInfo(seq=3, kind="confirm", message="Sure?"),
        error=BadRequest("text 只用于 prompt 对话框（当前为 confirm）"),
    )
    blocks = asyncio.run(dialog_action.run(session, {"action": "accept", "text": "oops"}))
    text = blocks[0]["text"]
    assert "对话框仍在等待处理" in text
    assert "status=blocked retryable=true" in text
    assert session.pending is not None


def test_a_dialog_that_is_already_gone_is_not_claimed_as_handled(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(
        DialogInfo(seq=5, kind="alert", message="gone"),
        closed="gone",
        reason="browser_dialog reached no dialog (closed by the page)",
        sent=False,
    )
    blocks = asyncio.run(dialog_action.run(session, {"action": "accept"}))
    text = blocks[0]["text"]
    assert "status=uncertain retryable=false" in text
    assert "result=gone" in text
    assert "prompt_text_chars" not in text
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "uncertain"


def test_an_unsent_answer_is_not_reported_as_sent(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(
        DialogInfo(seq=7, kind="prompt", message="Name?"),
        reason="handled by browser_dialog",
        sent=False,
    )
    blocks = asyncio.run(dialog_action.run(session, {"action": "accept", "text": "Ada"}))
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "prompt_text_chars" not in text


def test_a_closed_by_the_page_record_is_not_called_our_success(monkeypatch) -> None:
    fixed_id(monkeypatch)
    session = FakeSession(
        DialogInfo(seq=8, kind="alert", message="x"),
        reason="closed in the page",
        sent=True,
    )
    blocks = asyncio.run(dialog_action.run(session, {"action": "accept"}))
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "result=accepted reason=closed in the page" in text


@pytest.mark.parametrize("action", ["accept", "dismiss"])
def test_both_actions_reach_handle_dialog(monkeypatch, action) -> None:
    fixed_id(monkeypatch)
    session = SimpleNamespace(
        page_epoch=5,
        journal=Journal(None),
        pending_dialog=lambda: DialogInfo(seq=6, kind="alert", message="hi"),
        last_dialog=lambda: None,
    )
    seen: list[tuple[bool, str]] = []

    async def handle_dialog(*, accept: bool, text: str = "") -> tuple[DialogInfo, bool]:
        seen.append((accept, text))
        return (
            DialogInfo(seq=6, kind="alert", message="hi", closed="accepted", reason="handled by browser_dialog"),
            True,
        )

    session.handle_dialog = handle_dialog
    asyncio.run(dialog_action.run(session, {"action": action}))
    assert seen == [(action == "accept", "")]
