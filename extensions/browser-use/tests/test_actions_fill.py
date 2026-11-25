# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from icode_browser_use.actions import _shared, actionability, fill
from icode_browser_use.errors import CDPTimeout, StaleRef
from icode_browser_use.journal import Journal
from icode_browser_use.snapshot.fusion import FusedNode

FOCUS_VALUE = {
    "focused": True,
    "editable": True,
    "tag": "div",
    "type": "",
    "readOnly": False,
    "disabled": False,
}


class FakeClient:
    def __init__(self, state: dict | None = None) -> None:
        self.state = state
        self.update_on_insert = True
        self.calls: list[tuple[str, dict | None, str | None]] = []

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        params = params or {}
        self.calls.append((method, params, session_id))
        if method == "Input.insertText" and self.state is not None and self.update_on_insert:
            self.state["text"] = str(params.get("text", ""))
        elif method == "Input.dispatchKeyEvent" and self.state is not None:
            if params.get("key") == "Backspace":
                self.state["text"] = ""
        return {"result": {"value": None}}


class FakeSession:
    session_id = "main"
    target_id = "t"
    page_epoch = 7
    dialog_seq = 0
    cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0)

    def __init__(self) -> None:
        self.state = {"tag": "div", "editable": True, "focused": True, "text": ""}
        self.client = FakeClient(self.state)
        self.marked = 0
        self.successes = 0
        self.journal = Journal(None)
        self.snapshot = SimpleNamespace(
            nodes=[FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=self.page_epoch)]
        )

    def dialog_since(self, _seq):
        return None

    def resolve_target(self, _target):
        return SimpleNamespace(token="@e1#7", cdp_session_id=None)

    async def resolve_object_id(self, _entry):
        return "obj"

    def require_client(self) -> FakeClient:
        return self.client

    def mark_dirty(self) -> None:
        self.marked += 1

    def note_success(self, _target_id) -> None:
        self.successes += 1


def test_fill_submit_unchanged_is_not_claimed_as_success(monkeypatch) -> None:
    session = FakeSession()
    sessions = [session]
    page = {"href": "https://example.com", "mutations": 0, "toasts": []}

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(sessions[0].state)}
        return {}

    async def fake_eval_value(*_args, **_kwargs):
        return page

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", fake_sleep)
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "发布你的评论", "text": "hello", "clear": False, "submit": True, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "提交未确认" in text
    assert "已发送回车提交" not in text
    assert "已提交" not in text
    assert "status=uncertain" in text
    assert session.successes == 0


def test_a_page_probe_failure_keeps_the_cleared_field_verdict(monkeypatch) -> None:
    session = FakeSession()
    original = session.client.send

    async def clearing_send(method, params=None, session_id=None, timeout=None):
        result = await original(method, params, session_id, timeout)
        if method == "Input.dispatchKeyEvent" and (params or {}).get("key") == "Enter":
            session.state["text"] = ""
        return result

    session.client.send = clearing_send
    pages = {"n": 0}

    async def fake_eval_value(*_args, **_kwargs):
        pages["n"] += 1
        if pages["n"] > 1:
            raise CDPTimeout("Runtime.evaluate", 1.0)
        return {"href": "https://example.com", "mutations": 0, "toasts": []}

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(session.state)}
        return {}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "field", "text": "hello", "clear": True, "submit": True, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "已提交（输入框已清空）" in text
    assert "status=succeeded" in text


def test_fill_focus_failure_does_not_type(monkeypatch) -> None:
    session = FakeSession()

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": {"focused": False}}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    try:
        asyncio.run(
            fill.run(
                session,
                {"target": "field", "text": "hello", "clear": True, "submit": False, "keypress_fallback": False},
            )
        )
    except Exception as exc:  # noqa: BLE001 - the error type is asserted below
        assert "无法聚焦" in str(exc)
    else:
        raise AssertionError("expected focus failure")
    assert all(call[0] != "Input.insertText" for call in session.client.calls)


def test_fill_keypress_fallback_mismatch_is_uncertain(monkeypatch) -> None:
    session = FakeSession()
    session.state = {"tag": "div", "editable": True, "focused": True, "text": "wrong"}
    session.client.state = session.state
    session.client.update_on_insert = False

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(session.state)}
        return {}

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", fake_sleep)
    blocks = asyncio.run(
        fill.run(
            session,
            {
                "target": "field",
                "text": "hello",
                "clear": False,
                "submit": False,
                "keypress_fallback": True,
            },
        )
    )
    assert "读回与输入不一致" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]


def test_fill_readback_timeout_is_uncertain(monkeypatch) -> None:
    session = FakeSession()
    reads = 0

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        nonlocal reads
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            reads += 1
            if reads == 1:
                raise CDPTimeout("Runtime.callFunctionOn", 1.0)
            return {"type": "object", "value": dict(session.state)}
        return {}

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", fake_sleep)
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "field", "text": "hello", "clear": False, "submit": False, "keypress_fallback": False},
        )
    )
    assert "status=uncertain" in blocks[0]["text"]
    assert "readback_failed" in blocks[0]["text"]


def test_fill_enter_page_probe_timeout_does_not_press_enter(monkeypatch) -> None:
    session = FakeSession()

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(session.state)}
        return {}

    async def fake_eval_value(*_args, **_kwargs):
        raise CDPTimeout("Runtime.evaluate", 1.0)

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", fake_sleep)
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "field", "text": "hello", "clear": False, "submit": True, "keypress_fallback": False},
        )
    )
    assert "提交状态未知" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    assert all(
        not (call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter") for call in session.client.calls
    )


def test_fill_mismatch_does_not_retype(monkeypatch) -> None:
    session = FakeSession()
    session.state = {"tag": "div", "editable": True, "focused": True, "text": "bye"}
    session.client.state = session.state
    session.client.update_on_insert = False

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(sessions[0].state)}
        return {}

    async def fake_sleep(_seconds):
        return None

    sessions = [session]
    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", fake_sleep)
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "field", "text": "hello", "clear": False, "submit": False, "keypress_fallback": False},
        )
    )
    assert "读回与输入不一致" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    assert all(not (call[0] == "Input.dispatchKeyEvent" and call[1].get("text")) for call in session.client.calls)


def test_normalize_text_collapses_whitespace() -> None:
    assert fill._normalize_text("  hello\n\t世界  ") == "hello 世界"


def test_values_match_requires_exact_text_after_clear() -> None:
    assert fill._values_match("hello\n", "hello") is True
    assert fill._values_match("hello world", "hello") is False


def test_values_match_can_append_when_not_clearing() -> None:
    assert fill._values_match("prefix hello", "hello", clear=False) is True


def test_field_state_does_not_use_an_expando_value_for_contenteditable() -> None:
    assert "isContentEditable" in actionability.FIELD_STATE_FN
    assert "this.value !== undefined" not in actionability.FIELD_STATE_FN


def test_enter_key_carries_carriage_return_text() -> None:
    session = FakeSession()
    asyncio.run(fill._press_key(session, "Enter", "Enter", 13, text="\r"))
    key_down = next(call for call in session.client.calls if call[1].get("type") == "keyDown")
    key_up = next(call for call in session.client.calls if call[1].get("type") == "keyUp")
    assert key_down[1]["text"] == "\r"
    assert key_down[1]["nativeVirtualKeyCode"] == 13
    assert "text" not in key_up[1]


def _state(text: str = "", *, focused: bool = True) -> dict:
    return {"tag": "div", "editable": True, "focused": focused, "text": text}


def _page(*, href: str = "https://example.com", mutations: int = 0, toasts: list[str] | None = None) -> dict:
    return {"href": href, "mutations": mutations, "toasts": toasts or []}


def test_submit_verdict_cleared_field_is_reported_as_submitted() -> None:
    text, observed = fill._submit_verdict(_state("hello"), _state(""), _page(), _page())
    assert "已提交" in text
    assert observed is True


def test_submit_verdict_unchanged_is_not_claimed_as_submitted() -> None:
    text, observed = fill._submit_verdict(_state("hello"), _state("hello"), _page(), _page())
    assert "提交未确认" in text
    assert "已提交" not in text
    assert observed is False


def test_submit_verdict_surfaces_page_rejection() -> None:
    text, observed = fill._submit_verdict(
        _state("hello"), _state("hello"), _page(), _page(toasts=["评论失败：没有权限"])
    )
    assert "提交被拒" in text
    assert "没有权限" in text
    assert observed is False


def test_submit_verdict_treats_replaced_editor_as_unconfirmed() -> None:
    text, observed = fill._submit_verdict(_state("hello"), _state("hello"), _page(), _page(), replaced=True)
    assert "编辑器已被替换" in text
    assert observed is False


def test_submit_verdict_unknown_readback_is_not_submitted() -> None:
    text, observed = fill._submit_verdict(_state("hello"), {}, _page(), _page())
    assert "提交状态未知" in text
    assert "已提交" not in text
    assert observed is False


def test_submit_verdict_does_not_swallow_a_rejection_that_quotes_the_text() -> None:
    text, observed = fill._submit_verdict(_state("hello"), _state("hello"), _page(), _page(toasts=["hello 发送失败"]))
    assert "提交被拒" in text
    assert observed is False


def test_submit_verdict_page_unknown_is_not_reported_as_a_page_update() -> None:
    text, observed = fill._submit_verdict(_state("hello"), _state("hello"), _page(mutations=0), {})
    assert observed is False
    assert "页面状态未知" in text
    assert "页面有更新" not in text


def test_submit_verdict_does_not_treat_user_text_as_rejection() -> None:
    text, observed = fill._submit_verdict(
        _state("这个功能失败了"), _state("这个功能失败了"), _page(), _page(toasts=["这个功能失败了"])
    )
    assert "提交被拒" not in text
    assert observed is False


def test_submit_verdict_does_not_claim_empty_as_submitted() -> None:
    text, observed = fill._submit_verdict(_state(""), _state(""), _page(), _page())
    assert "已提交" not in text
    assert observed is False


def test_fill_rejects_non_text_target(monkeypatch) -> None:
    session = FakeSession()

    async def fake_call_value(*_args, **_kwargs):
        return {
            "type": "object",
            "value": {
                "focused": True,
                "editable": False,
                "tag": "button",
                "type": "submit",
                "readOnly": False,
                "disabled": False,
            },
        }

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    try:
        asyncio.run(
            fill.run(
                session,
                {"target": "评论", "text": "hello", "clear": True, "submit": True, "keypress_fallback": False},
            )
        )
    except Exception as exc:  # noqa: BLE001 - error type is asserted below
        assert "不是可编辑文本字段" in str(exc)
    else:
        raise AssertionError("expected non-text target rejection")
    assert all(call[0] != "Input.insertText" for call in session.client.calls)


def test_fill_records_prepared_before_sending_and_never_stores_the_typed_text(monkeypatch) -> None:
    session = FakeSession()
    phases: list[str] = []
    original_send = session.client.send

    async def observing_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.insertText":
            row = session.journal.lookup("fixed")
            phases.append(str(row["phase"]) if row else "missing")
        return await original_send(method, params, session_id, timeout)

    session.client.send = observing_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        return {"type": "object", "value": dict(session.state)}

    async def fake_eval_value(*_args, **_kwargs):
        return {"href": "https://example.com", "mutations": 0, "toasts": []}

    monkeypatch.setattr(fill, "new_action_id", lambda: "fixed")
    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))

    asyncio.run(
        fill.run(
            session,
            {
                "target": "field",
                "text": "SENTINEL-TOKEN",
                "clear": False,
                "submit": False,
                "keypress_fallback": False,
            },
        )
    )
    assert phases == ["prepared"]
    row = session.journal.lookup("fixed")
    assert row is not None
    assert row["phase"] == "result"
    assert "SENTINEL-TOKEN" not in str(row["evidence"])


def test_a_dialog_opened_by_the_fill_blocks_the_result(monkeypatch) -> None:
    from icode_browser_use.dialog import DialogInfo

    session = FakeSession()
    info = DialogInfo(seq=1, kind="alert", message="onchange fired")
    sent: list[bool] = []

    class DialogClient(FakeClient):
        async def send(self, method, params=None, session_id=None, timeout=None):
            result = await super().send(method, params, session_id=session_id, timeout=timeout)
            if method == "Input.insertText":
                sent.append(True)
            return result

    session.client = DialogClient(session.state)
    session.dialog_since = lambda _seq: info if sent else None

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": dict(FOCUS_VALUE)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "new_action_id", lambda: "fixed")
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "field", "text": "hello", "clear": False, "submit": False, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert 'dialog=alert message="onchange fired"' in text
    assert "status=blocked retryable=false" in text
    assert "succeeded" not in text
    assert sent == [True], "the fill was never sent"
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked"


def _stale_session() -> FakeSession:
    session = FakeSession()
    session.snapshot.nodes = [
        FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=session.page_epoch - 1)
    ]
    return session


def test_fill_free_text_on_stale_snapshot_refreshes_before_resolving(monkeypatch) -> None:
    session = _stale_session()
    order: list[str] = []

    async def fake_capture_and_register(_session):
        order.append("capture")
        _session.snapshot.nodes = [
            FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=_session.page_epoch)
        ]

    def fake_resolve_target(_target):
        order.append("resolve")
        assert not _shared.snapshot_is_stale(session), "resolve_target ran against a stale snapshot"
        return SimpleNamespace(token="@e1#7", cdp_session_id=None)

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        return {"type": "object", "value": dict(session.state)}

    session.resolve_target = fake_resolve_target
    monkeypatch.setattr(_shared, "capture_and_register", fake_capture_and_register)
    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "评论", "text": "hello", "clear": False, "submit": False, "keypress_fallback": False},
        )
    )
    assert order == ["capture", "resolve"]
    assert "status=succeeded" in blocks[0]["text"]
    assert session.successes == 1
    assert any(call[0] == "Input.insertText" for call in session.client.calls)


def test_fill_stale_ref_is_not_refreshed_and_surfaces_stale_ref(monkeypatch) -> None:
    session = _stale_session()
    captures: list[bool] = []

    async def fake_capture_and_register(_session):
        captures.append(True)

    def fake_resolve_target(target):
        epoch = int(str(target).rsplit("#", 1)[1])
        if epoch != session.page_epoch:
            raise StaleRef(f"引用 {target} 已过期（当前 epoch {session.page_epoch}）")
        return SimpleNamespace(token=target, cdp_session_id=None)

    session.resolve_target = fake_resolve_target
    monkeypatch.setattr(_shared, "capture_and_register", fake_capture_and_register)
    try:
        asyncio.run(
            fill.run(
                session,
                {"target": "@e1#6", "text": "hello", "clear": False, "submit": False, "keypress_fallback": False},
            )
        )
    except StaleRef as exc:
        assert "@e1#6" in str(exc)
    else:
        raise AssertionError("expected StaleRef for a stale @eN#epoch reference")
    assert captures == []
    assert session.client.calls == []
    assert session.successes == 0


def test_fill_stops_when_clear_shifts_focus_to_another_field(monkeypatch) -> None:
    """P1 #1: if clearing field 1 triggers an input event that moves focus to field 2, never type into field 2."""
    from icode_browser_use.errors import BadRequest

    session = FakeSession()
    session.state = {"tag": "input", "editable": False, "focused": True, "text": "old"}

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    "focused": True,
                    "editable": False,
                    "tag": "input",
                    "type": "text",
                    "readOnly": False,
                    "disabled": False,
                },
            }
        if "new Event('input'" in function:
            # Simulate page oninput handler shifting focus to field 2.
            session.state["text"] = ""
            session.state["focused"] = False
            return {"type": "object", "value": True}
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    try:
        asyncio.run(
            fill.run(
                session,
                {"target": "first", "text": "replacement", "clear": True, "submit": False, "keypress_fallback": False},
            )
        )
    except BadRequest as exc:
        assert "失去焦点" in str(exc)
    else:
        raise AssertionError("expected BadRequest when clear shifts focus")
    assert session.client.calls == []


def test_fill_stops_before_fallback_and_enter_when_insert_shifts_focus(monkeypatch) -> None:
    """P1 #1: if Input.insertText shifts focus away, neither fallback typing nor Enter runs on the other field."""
    session = FakeSession()
    session.state = {"tag": "input", "editable": False, "focused": True, "text": ""}
    session.client.update_on_insert = False

    original_send = session.client.send

    async def shifting_send(method, params=None, session_id=None, timeout=None):
        result = await original_send(method, params, session_id, timeout)
        if method == "Input.insertText":
            session.state["focused"] = False
        return result

    session.client.send = shifting_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    "focused": True,
                    "editable": False,
                    "tag": "input",
                    "type": "text",
                    "readOnly": False,
                    "disabled": False,
                },
            }
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "first", "text": "replacement", "clear": False, "submit": True, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "focus_lost" in text
    assert all(call[0] != "Input.dispatchKeyEvent" for call in session.client.calls)


def test_fill_rich_text_clear_verifies_selection_before_backspace(monkeypatch) -> None:
    """P1 #2: when in-editor selection fails, never send Backspace to nibble an existing draft."""
    from icode_browser_use.errors import BadRequest

    session = FakeSession()
    session.state = {"tag": "div", "editable": True, "focused": True, "text": "an existing draft"}

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "range.selectNodeContents" in function:
            return {"type": "object", "value": {"focused": True, "selected": False, "empty": False}}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(session.state)}
        return {"type": "object", "value": True}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    try:
        asyncio.run(
            fill.run(
                session,
                {"target": "editor", "text": "new draft", "clear": True, "submit": False, "keypress_fallback": False},
            )
        )
    except BadRequest as exc:
        assert "选中全部内容" in str(exc)
    else:
        raise AssertionError("expected BadRequest when selection cannot be verified")
    assert session.client.calls == []
    assert session.state["text"] == "an existing draft"


def test_fill_submit_navigation_succeeds_even_when_old_input_is_destroyed(monkeypatch) -> None:
    """Independent #7: GET form navigation destroys old input object_id; page href change still proves submission."""
    session = FakeSession()
    pages = [
        {"href": "http://example.com/search", "mutations": 0, "toasts": []},
        {"href": "http://example.com/done?q=testing", "mutations": 0, "toasts": []},
    ]
    entered = False
    original_send = session.client.send

    async def nav_send(method, params=None, session_id=None, timeout=None):
        nonlocal entered
        result = await original_send(method, params, session_id, timeout)
        if method == "Input.dispatchKeyEvent" and (params or {}).get("key") == "Enter":
            entered = True
        return result

    session.client.send = nav_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            if entered:
                raise StaleRef("Execution context was destroyed")
            return {"type": "object", "value": dict(session.state)}
        return {"type": "object", "value": True}

    async def fake_eval_value(*_args, **_kwargs):
        return pages.pop(0) if len(pages) > 1 else pages[0]

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "q", "text": "testing", "clear": False, "submit": True, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "已提交（页面已跳转）" in text
    assert "status=succeeded" in text
    assert session.successes == 1


def test_fill_clear_false_with_keypress_fallback_preserves_existing_text_when_insert_succeeds(monkeypatch) -> None:
    """P1 #1 (follow-up): clear=false + keypress_fallback=true must not wipe existing text when insertText succeeds."""
    session = FakeSession()
    session.state = {"tag": "input", "editable": False, "focused": True, "text": "draft "}
    original_send = session.client.send

    async def appending_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.insertText":
            session.client.calls.append((method, params or {}, session_id))
            session.state["text"] = session.state["text"] + str((params or {}).get("text", ""))
            return {"result": {"value": None}}
        return await original_send(method, params, session_id, timeout)

    session.client.send = appending_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    "focused": True,
                    "editable": False,
                    "tag": "input",
                    "type": "text",
                    "readOnly": False,
                    "disabled": False,
                    "text": "draft ",
                    "selectionStart": 6,
                    "selectionEnd": 6,
                },
            }
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "notes", "text": "replacement", "clear": False, "submit": False, "keypress_fallback": True},
        )
    )
    text = blocks[0]["text"]
    assert "status=succeeded" in text
    assert session.state["text"] == "draft replacement"
    assert all(call[0] != "Input.dispatchKeyEvent" for call in session.client.calls)


def test_fill_clear_false_append_on_full_maxlength_field_reports_mismatch_not_success(monkeypatch) -> None:
    """P1 #1 (follow-up): appending 'abc' to a full maxlength=3 input containing 'abc' must not falsely report succeeded."""
    session = FakeSession()
    session.state = {"tag": "input", "editable": False, "focused": True, "text": "abc"}
    session.client.update_on_insert = False

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    "focused": True,
                    "editable": False,
                    "tag": "input",
                    "type": "text",
                    "readOnly": False,
                    "disabled": False,
                    "text": "abc",
                    "selectionStart": 3,
                    "selectionEnd": 3,
                },
            }
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "code", "text": "abc", "clear": False, "submit": False, "keypress_fallback": True},
        )
    )
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "读回与输入不一致" in text
    assert session.state["text"] == "abc"
    # Backspace must never have been sent when clear=False.
    assert all(
        not (call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Backspace") for call in session.client.calls
    )
