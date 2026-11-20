# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""Unit tests for ``browser_press_key`` (W08)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from browser_use.actions import press_key
from browser_use.errors import BadRequest, CDPTimeout, ConnectionClosed
from browser_use.journal import Journal
from browser_use.snapshot.fusion import FusedNode

FACTS = {
    "tag": "input",
    "type": "text",
    "name": "combo",
    "inForm": True,
    "readOnly": False,
    "checked": False,
    "value": "seed",
    "contextLength": 10,
    "disabled": False,
    "ariaDisabled": False,
}


def page(
    *,
    active: str = "input",
    active_id: str = "keys",
    active_value: str = "seed",
    active_text: str = "",
    mutations: int = 0,
    href: str = "http://fixture/",
) -> dict:
    return {
        "href": href,
        "mutations": mutations,
        "expanded": 0,
        "dialogs": 0,
        "active": active,
        "activeId": active_id,
        "activeValue": active_value,
        "activeChecked": False,
        "activeText": active_text,
        "toasts": [],
    }


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.fail: Exception | None = None

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, dict(params or {}), session_id))
        if self.fail is not None:
            raise self.fail
        return {"result": {"value": None}}

    def keys(self) -> list[dict]:
        return [params for method, params, _ in self.calls if method == "Input.dispatchKeyEvent"]


class FakeSession:
    session_id = "main"
    target_id = "t"
    page_epoch = 3
    dialog_seq = 0
    cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0)

    def __init__(self, cdp_session_id: str | None = None) -> None:
        self.client = FakeClient()
        self.successes = 0
        self.marked = 0
        self.journal = Journal(None)
        self._cdp_session_id = cdp_session_id
        self.snapshot = SimpleNamespace(
            nodes=[FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=self.page_epoch)]
        )

    def dialog_since(self, _seq):
        return None

    def resolve_target(self, _target):
        return SimpleNamespace(token="@e1#3", cdp_session_id=self._cdp_session_id)

    async def resolve_object_id(self, _entry):
        return "obj"

    def require_client(self):
        return self.client

    def mark_dirty(self) -> None:
        self.marked += 1

    def note_success(self, _target_id) -> None:
        self.successes += 1


def install(monkeypatch, session: FakeSession, *, pages: list[dict], facts: list[dict], focus: dict | None = None):
    focus = focus if focus is not None else {"focused": True}

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(focus)}
        return {"type": "object", "value": dict(facts.pop(0) if facts else FACTS)}

    async def fake_eval_value(*_args, **_kwargs):
        return dict(pages.pop(0) if len(pages) > 1 else pages[0])

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(press_key, "call_value", fake_call_value)
    monkeypatch.setattr(press_key, "eval_value", fake_eval_value)
    monkeypatch.setattr(press_key, "_sleep", fake_sleep)


def test_parse_key_plain_and_named() -> None:
    assert press_key.parse_key("Enter") == ("Enter", "Enter", 13, "\r", 0)
    assert press_key.parse_key("tab") == ("Tab", "Tab", 9, None, 0)
    assert press_key.parse_key("ArrowDown") == ("ArrowDown", "ArrowDown", 40, None, 0)
    assert press_key.parse_key("a") == ("a", "KeyA", 65, "a", 0)
    assert press_key.parse_key("A") == ("A", "KeyA", 65, "A", 0)
    assert press_key.parse_key("7") == ("7", "Digit7", 55, "7", 0)


def test_parse_key_combinations() -> None:
    assert press_key.parse_key("Ctrl+A") == ("a", "KeyA", 65, None, 2)
    assert press_key.parse_key("Shift+Tab") == ("Tab", "Tab", 9, None, 8)
    assert press_key.parse_key("Shift+a") == ("A", "KeyA", 65, "A", 8)
    assert press_key.parse_key("Ctrl+Shift+A") == ("A", "KeyA", 65, None, 10)
    assert press_key.parse_key("Meta+Enter") == ("Enter", "Enter", 13, None, 4)
    assert press_key.parse_key("/") == ("/", "Slash", 191, "/", 0)
    assert press_key.parse_key("Shift+-") == ("-", "Minus", 189, "-", 8)
    assert press_key.parse_key("+") == ("+", "+", None, "+", 0)


@pytest.mark.parametrize("spec", ["", "   ", "Hyper+A", "Ctrl+", "NotAKey", "é"])
def test_parse_key_rejects_unknown_input(spec: str) -> None:
    with pytest.raises(BadRequest):
        press_key.parse_key(spec)


def test_plain_character_inserts_through_a_single_char_event(monkeypatch) -> None:
    session = FakeSession()
    install(monkeypatch, session, pages=[page()], facts=[FACTS])
    asyncio.run(press_key.run(session, {"key": "a", "target": ""}))
    keys = session.client.keys()
    assert [event["type"] for event in keys] == ["keyDown", "char", "keyUp"]
    assert keys[1]["text"] == "a"
    assert "text" not in keys[0]


def test_enter_carries_the_return_character(monkeypatch) -> None:
    session = FakeSession()
    install(monkeypatch, session, pages=[page(), page(active="body", href="http://fixture/submitted")], facts=[FACTS])
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": ""}))
    keys = session.client.keys()
    assert [event["type"] for event in keys] == ["keyDown", "keyUp"]
    assert keys[0]["text"] == "\r"
    assert "effect=navigation" in blocks[0]["text"]
    assert "status=succeeded" in blocks[0]["text"]


def test_control_combination_sets_modifiers_without_text(monkeypatch) -> None:
    session = FakeSession()
    install(monkeypatch, session, pages=[page()], facts=[FACTS])
    asyncio.run(press_key.run(session, {"key": "Ctrl+A", "target": "combo"}))
    keys = session.client.keys()
    assert all(event["modifiers"] == 2 for event in keys)
    assert all("text" not in event for event in keys)


def test_target_is_focused_before_pressing_and_dispatches_into_its_frame(monkeypatch) -> None:
    session = FakeSession(cdp_session_id="child")
    install(monkeypatch, session, pages=[page()], facts=[FACTS])
    asyncio.run(press_key.run(session, {"key": "Tab", "target": "@e1#3"}))
    key_sessions = [session_id for method, _, session_id in session.client.calls if method == "Input.dispatchKeyEvent"]
    assert key_sessions == ["child", "child"]


def test_unfocusable_target_sends_no_keys_and_is_recorded_blocked(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(press_key, "new_action_id", lambda: "fixed")
    install(monkeypatch, session, pages=[page()], facts=[FACTS], focus={"focused": False})
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "status=blocked" in text
    assert "unfocusable" in text
    assert session.client.keys() == []
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked"


def test_disabled_target_is_blocked_and_recorded(monkeypatch) -> None:
    session = FakeSession()
    disabled = {**FACTS, "disabled": True}
    install(monkeypatch, session, pages=[page()], facts=[disabled])
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    assert "status=blocked" in blocks[0]["text"]
    assert session.client.keys() == []
    assert session.successes == 0


def test_page_change_is_succeeded_and_journalled(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(press_key, "new_action_id", lambda: "fixed")
    changed = {**FACTS, "value": "seed!"}
    install(monkeypatch, session, pages=[page(), page(active="body", mutations=1)], facts=[FACTS, changed])
    blocks = asyncio.run(press_key.run(session, {"key": "Tab", "target": "@e1#3"}))
    assert "effect=dom" in blocks[0]["text"]
    assert "status=succeeded" in blocks[0]["text"]
    assert session.successes == 1
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "result" and row["status"] == "succeeded"


def test_enter_without_a_page_change_is_not_claimed_as_submitted(monkeypatch) -> None:
    session = FakeSession()
    install(monkeypatch, session, pages=[page()], facts=[FACTS, FACTS])
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "effect=none" in text
    assert "status=uncertain" in text
    assert "不要重复按 Enter" in text
    assert len(session.client.keys()) == 2  # exactly one keyDown/keyUp pair: no resend
    assert session.successes == 0


def test_enter_that_edited_the_field_is_still_not_a_confirmed_submit(monkeypatch) -> None:
    session = FakeSession()
    changed = {**FACTS, "value": "seed!"}
    install(monkeypatch, session, pages=[page()], facts=[FACTS, FACTS, changed])
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "effect=dom" in text
    assert "status=uncertain" in text
    assert "提交未确认" in text
    assert session.successes == 0
    assert len(session.client.keys()) == 2


def test_unrelated_page_mutations_are_not_claimed_as_the_key_effect(monkeypatch) -> None:
    session = FakeSession()
    install(monkeypatch, session, pages=[page(), page(mutations=7)], facts=[FACTS, FACTS])
    blocks = asyncio.run(press_key.run(session, {"key": "Tab", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "effect=dom_elsewhere" in text
    assert "status=uncertain" in text
    assert session.successes == 0


def test_focused_field_edit_is_attributed_without_a_target(monkeypatch) -> None:
    session = FakeSession()
    pages = [page(), page(active_value="seed!")]
    install(monkeypatch, session, pages=pages, facts=[FACTS])
    blocks = asyncio.run(press_key.run(session, {"key": "a", "target": ""}))
    assert "effect=dom" in blocks[0]["text"]
    assert "status=succeeded" in blocks[0]["text"]


def test_enter_in_a_textarea_is_a_newline_not_a_submit(monkeypatch) -> None:
    session = FakeSession()
    facts = {**EDITABLE_FACTS, "tag": "textarea", "value": "seed"}
    changed = {**facts, "value": "seed\n", "contextLength": 11}
    install(
        monkeypatch,
        session,
        pages=[page(active="textarea")],
        facts=[facts, facts, changed],
        focus={**EDITABLE_FOCUS, "tag": "textarea"},
    )

    async def never(*_args, **_kwargs):
        raise AssertionError("a textarea Enter must not be judged as a submit")

    monkeypatch.setattr(press_key, "field_state", never)
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "effect=dom" in text
    assert "status=succeeded" in text
    assert "提交" not in text
    assert "textarea" in text and "只插入换行" in text
    assert len(session.client.keys()) == 2


def test_enter_in_a_text_input_is_judged_as_a_submit(monkeypatch) -> None:
    session = FakeSession()
    focus = {"focused": True, "tag": "input", "type": "text", "editable": False}
    install_submit(monkeypatch, pages=[page(active="input")], fields=[field("hello"), field("")], focus=focus)
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "已提交（输入框已清空）" in text
    assert "status=succeeded" in text
    assert "提交未确认" not in text


def test_press_key_records_dispatched_after_the_send(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(press_key, "new_action_id", lambda: "fixed")
    seen: list[str] = []

    async def watch(_seconds):
        row = session.journal.lookup("fixed")
        seen.append(str(row["phase"]) if row else "missing")

    install(monkeypatch, session, pages=[page(), page(active_value="seed!")], facts=[FACTS, FACTS, FACTS])
    monkeypatch.setattr(press_key, "_sleep", watch)
    asyncio.run(press_key.run(session, {"key": "Tab", "target": "@e1#3"}))
    assert seen and seen[0] == "dispatched"
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "result"


def test_lost_connection_stays_prepared(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(press_key, "new_action_id", lambda: "fixed")
    install(monkeypatch, session, pages=[page()], facts=[FACTS])

    async def fail(*_args, **_kwargs):
        raise ConnectionClosed("cdp gone")

    monkeypatch.setattr(press_key, "_send_key", fail)
    with pytest.raises(ConnectionClosed):
        asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "prepared"


def test_a_dialog_opened_by_the_key_blocks_the_result(monkeypatch) -> None:
    from browser_use.dialog import DialogInfo

    session = FakeSession()
    info = DialogInfo(seq=1, kind="confirm", message="Submit?")
    session.dialog_since = lambda _seq: info
    monkeypatch.setattr(press_key, "new_action_id", lambda: "fixed")
    install(monkeypatch, session, pages=[page()], facts=[FACTS, FACTS])
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert 'dialog=confirm message="Submit?"' in text
    assert "status=blocked retryable=false" in text
    assert "succeeded" not in text
    assert session.successes == 0
    assert session.client.keys(), "the key was never sent"
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked" and row["phase"] == "result"


# --- plain Enter in a text field is judged as a submit ---------------------------------------

EDITABLE_FOCUS = {"focused": True, "tag": "div", "type": "", "editable": True}
EDITABLE_FACTS = {**FACTS, "tag": "div", "type": "", "value": "", "inForm": False}


def field(text: str) -> dict:
    return {"tag": "div", "editable": True, "focused": True, "text": text}


def install_submit(monkeypatch, *, pages: list[dict], fields: list, focus: dict | None = None) -> None:
    """Like ``install`` but answers the field-state probe from ``fields`` (an Exception entry is raised)."""
    focus = focus if focus is not None else EDITABLE_FOCUS

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(focus)}
        if "let text=null" in function:
            item = fields.pop(0) if len(fields) > 1 else fields[0]
            if isinstance(item, Exception):
                raise item
            return {"type": "object", "value": dict(item)}
        return {"type": "object", "value": dict(EDITABLE_FACTS)}

    async def fake_eval_value(*_args, **_kwargs):
        return dict(pages.pop(0) if len(pages) > 1 else pages[0])

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(press_key, "call_value", fake_call_value)
    monkeypatch.setattr(press_key, "eval_value", fake_eval_value)
    monkeypatch.setattr(press_key, "_sleep", fake_sleep)


def test_enter_that_empties_a_contenteditable_is_a_confirmed_submit(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(press_key, "new_action_id", lambda: "fixed")
    install_submit(monkeypatch, pages=[page(active="div")], fields=[field("hello"), field("")])
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "status=succeeded" in text
    assert "已提交（输入框已清空）" in text
    assert "effect=dom" in text
    assert session.successes == 1
    assert len(session.client.keys()) == 2
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "result" and row["status"] == "succeeded"


def test_enter_that_leaves_the_field_and_page_unchanged_is_unconfirmed(monkeypatch) -> None:
    session = FakeSession()
    install_submit(monkeypatch, pages=[page(active="div")], fields=[field("hello")])
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "提交未确认" in text
    assert "succeeded" not in text
    assert "不要重复按 Enter" in text
    assert len(session.client.keys()) == 2  # exactly one keyDown/keyUp pair: no resend
    assert session.successes == 0


def test_shift_enter_in_a_text_field_is_not_a_submit_attempt(monkeypatch) -> None:
    session = FakeSession()
    changed = {**EDITABLE_FACTS, "contextLength": 11}
    facts = [EDITABLE_FACTS, EDITABLE_FACTS, changed]
    install(monkeypatch, session, pages=[page(active="div")], facts=facts, focus=EDITABLE_FOCUS)

    async def never(*_args, **_kwargs):
        raise AssertionError("Shift+Enter must not read the field state")

    monkeypatch.setattr(press_key, "field_state", never)
    blocks = asyncio.run(press_key.run(session, {"key": "Shift+Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "已按键 Shift+Enter 到 @e1#3" in text
    assert "effect=dom" in text
    assert "status=succeeded" in text
    assert "提交" not in text
    assert all(event["modifiers"] == 8 for event in session.client.keys())


def test_enter_whose_page_probe_times_out_keeps_the_cleared_field_verdict(monkeypatch) -> None:
    session = FakeSession()
    pages = {"n": 0}
    fields = [field("hello"), field("")]

    async def fake_eval_value(*_args, **_kwargs):
        pages["n"] += 1
        if pages["n"] > 1:
            raise CDPTimeout("Runtime.evaluate", 1.0)
        return page(active="div")

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(EDITABLE_FOCUS)}
        if "let text=null" in function:
            return {"type": "object", "value": dict(fields.pop(0) if len(fields) > 1 else fields[0])}
        return {"type": "object", "value": dict(EDITABLE_FACTS)}

    monkeypatch.setattr(press_key, "call_value", fake_call_value)
    monkeypatch.setattr(press_key, "eval_value", fake_eval_value)
    monkeypatch.setattr(press_key, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "已提交（输入框已清空）" in text
    assert "status=succeeded" in text


def test_enter_whose_field_readback_times_out_is_uncertain(monkeypatch) -> None:
    session = FakeSession()
    install_submit(
        monkeypatch,
        pages=[page(active="div")],
        fields=[field("hello"), CDPTimeout("Runtime.callFunctionOn", 1.0)],
    )
    blocks = asyncio.run(press_key.run(session, {"key": "Enter", "target": "@e1#3"}))
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "字段回读失败" in text
    assert "effect=unknown" in text
    assert "succeeded" not in text
    assert len(session.client.keys()) == 2
    assert session.successes == 0
