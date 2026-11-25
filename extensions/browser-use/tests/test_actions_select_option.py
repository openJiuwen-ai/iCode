# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""Unit tests for ``browser_select_option`` (W08)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from icode_browser_use.actions import select_option
from icode_browser_use.errors import BrowserError, ConnectionClosed
from icode_browser_use.journal import Journal
from icode_browser_use.snapshot.fusion import FusedNode

SINGLE = {
    "ok": True,
    "tag": "select",
    "multiple": False,
    "disabled": False,
    "options": [
        {"value": "alpha", "label": "Alpha", "disabled": False, "selected": False},
        {"value": "beta", "label": "Beta", "disabled": False, "selected": True},
        {"value": "gamma", "label": "Gamma", "disabled": False, "selected": False},
    ],
}

MULTIPLE = {
    "ok": True,
    "tag": "select",
    "multiple": True,
    "disabled": False,
    "options": [
        {"value": "red", "label": "Red", "disabled": False, "selected": False},
        {"value": "green", "label": "Green", "disabled": False, "selected": False},
        {"value": "blue", "label": "Blue", "disabled": False, "selected": False},
        {"value": "grey", "label": "Grey", "disabled": True, "selected": False},
    ],
}

FACTS = {"tag": "select", "type": "select-one", "disabled": False, "value": "beta"}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: Exception | None = None

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, dict(params or {})))
        if self.fail is not None:
            raise self.fail
        return {"result": {"value": None}}


class FakeSession:
    session_id = "main"
    target_id = "t"
    page_epoch = 4
    dialog_seq = 0
    cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0)

    def __init__(self) -> None:
        self.client = FakeClient()
        self.successes = 0
        self.marked = 0
        self.journal = Journal(None)
        self.snapshot = SimpleNamespace(
            nodes=[FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=self.page_epoch)]
        )

    def dialog_since(self, _seq):
        return None

    def resolve_target(self, _target):
        return SimpleNamespace(token="@e2#4", cdp_session_id=None)

    async def resolve_object_id(self, _entry):
        return "obj"

    def require_client(self):
        return self.client

    def mark_dirty(self) -> None:
        self.marked += 1

    def note_success(self, _target_id) -> None:
        self.successes += 1


def install(
    monkeypatch,
    session: FakeSession,
    *,
    states: list[dict[str, Any] | Exception],
    facts: dict | None = None,
    applied=None,
):
    """Fake the two CDP calls: select state probes and the apply call.

    An item in *states* that is an exception is raised by the state probe.
    """
    pending = list(states)
    applied_values: list[list[str]] = []

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "wanted" in function:
            values = list(_kwargs["arguments"][0]["value"])
            applied_values.append(values)
            if callable(applied):
                applied(values)
            elif applied is not None:
                raise applied
            return {"type": "object", "value": {"selected": list(values)}}
        state = pending.pop(0) if len(pending) > 1 else pending[0]
        if isinstance(state, Exception):
            raise state
        return {"type": "object", "value": dict(state)}

    async def fake_target_facts(*_args, **_kwargs):
        return dict(FACTS if facts is None else facts)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(select_option, "call_value", fake_call_value)
    monkeypatch.setattr(select_option, "target_facts", fake_target_facts)
    monkeypatch.setattr(select_option, "_sleep", fake_sleep)
    return applied_values


def selected_after(state: dict, values: list[str]) -> dict:
    after = {**state, "options": [dict(option) for option in state["options"]]}
    for option in after["options"]:
        option["selected"] = option["value"] in values
    return after


def test_single_select_by_value_is_read_back(monkeypatch) -> None:
    session = FakeSession()
    after = selected_after(SINGLE, ["gamma"])
    applied = install(monkeypatch, session, states=[SINGLE, after])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["gamma"]}))
    assert applied == [["gamma"]]
    assert "selected=['gamma']" in blocks[0]["text"]
    assert "status=succeeded" in blocks[0]["text"]
    assert session.successes == 1


def test_label_matches_when_the_value_does_not(monkeypatch) -> None:
    session = FakeSession()
    after = selected_after(SINGLE, ["gamma"])
    applied = install(monkeypatch, session, states=[SINGLE, after])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["Gamma"]}))
    assert applied == [["gamma"]]
    assert "status=succeeded" in blocks[0]["text"]


def test_multiple_select_sets_every_requested_option(monkeypatch) -> None:
    session = FakeSession()
    after = selected_after(MULTIPLE, ["red", "blue"])
    applied = install(monkeypatch, session, states=[MULTIPLE, after])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["red", "blue"]}))
    assert applied == [["red", "blue"]]
    assert "status=succeeded" in blocks[0]["text"]


def test_single_select_with_two_values_is_blocked_without_applying(monkeypatch) -> None:
    session = FakeSession()
    applied = install(monkeypatch, session, states=[SINGLE])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["alpha", "beta"]}))
    text = blocks[0]["text"]
    assert applied == []
    assert "单选" in text
    assert "status=blocked" in text


def test_missing_option_lists_available_values(monkeypatch) -> None:
    session = FakeSession()
    applied = install(monkeypatch, session, states=[SINGLE])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["delta"]}))
    text = blocks[0]["text"]
    assert applied == []
    assert "delta" in text and "alpha=Alpha" in text
    assert "status=blocked" in text


def test_non_select_target_is_blocked(monkeypatch) -> None:
    session = FakeSession()
    applied = install(monkeypatch, session, states=[{"ok": False, "reason": "not_select", "tag": "div"}])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["alpha"]}))
    assert applied == []
    assert "不是原生 <select>" in blocks[0]["text"]
    assert "status=blocked" in blocks[0]["text"]


def test_disabled_select_is_blocked(monkeypatch) -> None:
    session = FakeSession()
    disabled = {**SINGLE, "disabled": True}
    applied = install(monkeypatch, session, states=[disabled])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["alpha"]}))
    assert applied == []
    assert "disabled" in blocks[0]["text"]
    assert "status=blocked" in blocks[0]["text"]


def test_readback_mismatch_is_uncertain_and_not_retryable(monkeypatch) -> None:
    session = FakeSession()
    applied = install(monkeypatch, session, states=[SINGLE, SINGLE])  # the page never changes
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["gamma"]}))
    text = blocks[0]["text"]
    assert applied == [["gamma"]]
    assert "status=uncertain retryable=false" in text
    assert "不要直接重复选择" in text
    assert session.successes == 0


def test_failed_readback_is_not_reported_as_success(monkeypatch) -> None:
    session = FakeSession()
    applied = install(monkeypatch, session, states=[SINGLE, BrowserError("CDP 错误 -32000: probe failed")])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["gamma"]}))
    text = blocks[0]["text"]
    assert applied == [["gamma"]]
    assert "status=uncertain" in text
    assert "页面未回读" in text
    assert "status=succeeded" not in text
    assert session.successes == 0


def test_an_enabled_sibling_with_the_same_value_is_selectable(monkeypatch) -> None:
    session = FakeSession()
    state = {
        "ok": True,
        "tag": "select",
        "multiple": False,
        "disabled": False,
        "options": [
            {"value": "dup", "label": "Dup (disabled)", "disabled": True, "selected": False},
            {"value": "dup", "label": "Dup", "disabled": False, "selected": False},
        ],
    }
    after = {
        **state,
        "options": [
            {"value": "dup", "label": "Dup (disabled)", "disabled": True, "selected": False},
            {"value": "dup", "label": "Dup", "disabled": False, "selected": True},
        ],
    }
    applied = install(monkeypatch, session, states=[state, after])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["dup"]}))
    assert applied == [["dup"]]
    assert "status=succeeded" in blocks[0]["text"]


def test_readback_polls_briefly_for_a_controlled_select(monkeypatch) -> None:
    session = FakeSession()
    after = selected_after(SINGLE, ["gamma"])
    # A React-style control applies the change on its next render, not synchronously.
    applied = install(monkeypatch, session, states=[SINGLE, SINGLE, after])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["gamma"]}))
    assert applied == [["gamma"]]
    assert "status=succeeded" in blocks[0]["text"]
    assert session.successes == 1


def test_journal_records_prepared_before_the_page_call(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(select_option, "new_action_id", lambda: "fixed")
    seen: list[str] = []
    after = selected_after(SINGLE, ["gamma"])
    real_state = select_option._select_state

    async def watching(sess, object_id, cdp_session_id):
        row = sess.journal.lookup("fixed")
        seen.append(str(row["phase"]) if row else "missing")
        return await real_state(sess, object_id, cdp_session_id)

    def check(_values):
        row = session.journal.lookup("fixed")
        seen.append(str(row["phase"]) if row else "missing")

    install(monkeypatch, session, states=[SINGLE, after], applied=check)
    monkeypatch.setattr(select_option, "_select_state", watching)
    asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["gamma"]}))
    # pre-flight (no action yet), apply (prepared), readback (dispatched)
    assert seen == ["missing", "prepared", "dispatched"]
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "result" and row["status"] == "succeeded"


def test_lost_connection_during_apply_stays_prepared(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(select_option, "new_action_id", lambda: "fixed")
    install(monkeypatch, session, states=[SINGLE], applied=ConnectionClosed("cdp gone"))
    with pytest.raises(ConnectionClosed):
        asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["gamma"]}))
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "prepared"


def test_disabled_option_cannot_be_selected(monkeypatch) -> None:
    session = FakeSession()
    applied = install(monkeypatch, session, states=[MULTIPLE])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["grey"]}))
    assert applied == []
    assert "status=blocked" in blocks[0]["text"]


def test_a_dialog_opened_by_the_change_handler_blocks_the_result(monkeypatch) -> None:
    from icode_browser_use.dialog import DialogInfo

    session = FakeSession()
    info = DialogInfo(seq=1, kind="alert", message="change handler")
    seen: list[bool] = []
    session.dialog_since = lambda _seq: info if seen else None

    def applied(_values):
        seen.append(True)

    monkeypatch.setattr(select_option, "new_action_id", lambda: "fixed")
    values = install(
        monkeypatch,
        session,
        states=[SINGLE, selected_after(SINGLE, ["gamma"])],
        applied=applied,
    )
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["gamma"]}))
    text = blocks[0]["text"]
    assert values == [["gamma"]], "the selection was never applied"
    assert 'dialog=alert message="change handler"' in text
    assert "status=blocked retryable=false" in text
    assert "succeeded" not in text
    assert session.successes == 0
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked"


def test_select_option_targets_exact_enabled_index_for_duplicate_values(monkeypatch) -> None:
    session = FakeSession()
    state = {
        "ok": True,
        "tag": "select",
        "multiple": True,
        "disabled": False,
        "options": [
            {"index": 0, "value": "dup", "label": "Disabled Group Item", "disabled": True, "selected": False},
            {"index": 1, "value": "dup", "label": "Enabled Beta", "disabled": False, "selected": False},
            {"index": 2, "value": "dup", "label": "Enabled Gamma", "disabled": False, "selected": False},
        ],
    }
    after_right = {
        **state,
        "options": [
            {"index": 0, "value": "dup", "label": "Disabled Group Item", "disabled": True, "selected": False},
            {"index": 1, "value": "dup", "label": "Enabled Beta", "disabled": False, "selected": False},
            {"index": 2, "value": "dup", "label": "Enabled Gamma", "disabled": False, "selected": True},
        ],
    }
    captured_args: list[list[dict[str, Any]]] = []
    states = [state, after_right]

    async def fake_call_value(_session, _object_id, function, **kwargs):
        if "wantedIdx" in function:
            captured_args.append(list(kwargs["arguments"]))
            return {"type": "object", "value": {"selected": ["dup"], "selectedIndices": [2]}}
        return {"type": "object", "value": dict(states.pop(0) if len(states) > 1 else states[0])}

    async def fake_target_facts(*_args, **_kwargs):
        return dict(FACTS)

    monkeypatch.setattr(select_option, "call_value", fake_call_value)
    monkeypatch.setattr(select_option, "target_facts", fake_target_facts)

    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["Enabled Gamma"]}))
    assert "status=succeeded" in blocks[0]["text"]
    assert captured_args == [[{"value": ["dup"]}, {"value": [2]}]]


def test_select_option_readback_rejects_disabled_selected_option(monkeypatch) -> None:
    session = FakeSession()
    state = {
        "ok": True,
        "tag": "select",
        "multiple": False,
        "disabled": False,
        "options": [
            {"index": 0, "value": "dup", "label": "Disabled", "disabled": True, "selected": False},
            {"index": 1, "value": "dup", "label": "Enabled", "disabled": False, "selected": False},
        ],
    }
    bad_after = {
        **state,
        "options": [
            {"index": 0, "value": "dup", "label": "Disabled", "disabled": True, "selected": True},
            {"index": 1, "value": "dup", "label": "Enabled", "disabled": False, "selected": False},
        ],
    }
    install(monkeypatch, session, states=[state, bad_after])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["dup"]}))
    assert "status=uncertain" in blocks[0]["text"]
    assert "status=succeeded" not in blocks[0]["text"]


def test_select_option_js_checks_optgroup_and_fieldset_disabled() -> None:
    assert "optgroup" in select_option._SELECT_STATE_FN
    assert "matches(':disabled')" in select_option._SELECT_STATE_FN
    assert "optgroup" in select_option._APPLY_FN
    assert "selectedIndex" in select_option._APPLY_FN


def test_select_option_rejects_succeeded_when_change_handler_replaces_option_value_at_same_index(monkeypatch) -> None:
    session = FakeSession()
    before = {
        "ok": True,
        "tag": "select",
        "multiple": False,
        "disabled": False,
        "options": [
            {"index": 0, "value": "a", "label": "A", "disabled": False, "selected": True},
            {"index": 1, "value": "b", "label": "B", "disabled": False, "selected": False},
        ],
    }
    # The page's change handler replaced index 1's option value 'b' with 'c'.
    after = {
        "ok": True,
        "tag": "select",
        "multiple": False,
        "disabled": False,
        "options": [
            {"index": 0, "value": "a", "label": "A", "disabled": False, "selected": False},
            {"index": 1, "value": "c", "label": "C", "disabled": False, "selected": True},
        ],
    }
    install(monkeypatch, session, states=[before, after])
    blocks = asyncio.run(select_option.run(session, {"target": "@e2#4", "values": ["b"]}))
    text = blocks[0]["text"]
    assert "selected=['c']" in text
    assert "status=uncertain retryable=false" in text
    assert "status=succeeded" not in text
    assert "不要直接重复选择" in text
    assert session.successes == 0
