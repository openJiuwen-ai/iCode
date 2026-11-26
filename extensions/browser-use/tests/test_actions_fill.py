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


def test_fill_clear_false_replaces_existing_contenteditable_selection(monkeypatch) -> None:
    """Issue #1: FOCUS_FN must keep an existing in-editor selection so clear=false replaces 'bcd' -> 'aXef'."""
    assert "hasSelection" in actionability.FOCUS_FN
    assert "if (selection && !hasSelection)" in actionability.FOCUS_FN

    session = FakeSession()
    session.state = {"tag": "div", "editable": True, "focused": True, "text": "abcdef"}
    original_send = session.client.send

    async def replacing_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.insertText":
            session.client.calls.append((method, params or {}, session_id))
            session.state["text"] = "a" + str((params or {}).get("text", "")) + "ef"
            return {"result": {"value": None}}
        return await original_send(method, params, session_id, timeout)

    session.client.send = replacing_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    **FOCUS_VALUE,
                    "text": "abcdef",
                    "selectionText": "bcd",
                },
            }
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "editor", "text": "X", "clear": False, "submit": False, "keypress_fallback": False},
        )
    )
    assert "status=succeeded" in blocks[0]["text"]
    assert session.state["text"] == "aXef"


def test_fill_emoji_utf16_selection_offset_matches_and_submits(monkeypatch) -> None:
    """Issue #2: UTF-16 selection offset after emoji ('😀AB' at offset 2) matches '😀!AB' and sends Enter."""
    session = FakeSession()
    session.state = {"tag": "input", "editable": False, "focused": True, "text": "😀AB"}
    pages = [
        {"href": "http://example.com/form", "mutations": 0, "toasts": []},
        {"href": "http://example.com/submitted", "mutations": 0, "toasts": []},
    ]
    original_send = session.client.send

    async def emoji_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.insertText":
            session.client.calls.append((method, params or {}, session_id))
            session.state["text"] = "😀" + str((params or {}).get("text", "")) + "AB"
            return {"result": {"value": None}}
        return await original_send(method, params, session_id, timeout)

    session.client.send = emoji_send

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
                    "text": "😀AB",
                    "selectionStart": 2,
                    "selectionEnd": 2,
                },
            }
        return {"type": "object", "value": dict(session.state)}

    async def fake_eval_value(*_args, **_kwargs):
        return pages.pop(0) if len(pages) > 1 else pages[0]

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "field", "text": "!", "clear": False, "submit": True, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "status=succeeded" in text
    assert "已提交（页面已跳转）" in text
    assert any(call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter" for call in session.client.calls)


def test_fill_native_textarea_preserves_trailing_space_in_readback_check(monkeypatch) -> None:
    """Issue #4: <textarea maxlength='4'> truncated 'code ' -> 'code' must report mismatch, not succeeded."""
    session = FakeSession()
    session.state = {"tag": "textarea", "editable": False, "focused": True, "text": ""}
    original_send = session.client.send

    async def truncating_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.insertText":
            session.client.calls.append((method, params or {}, session_id))
            session.state["text"] = str((params or {}).get("text", ""))[:4]
            return {"result": {"value": None}}
        return await original_send(method, params, session_id, timeout)

    session.client.send = truncating_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    "focused": True,
                    "editable": False,
                    "tag": "textarea",
                    "type": "",
                    "readOnly": False,
                    "disabled": False,
                    "text": "",
                    "selectionStart": 0,
                    "selectionEnd": 0,
                },
            }
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "bio", "text": "code ", "clear": True, "submit": False, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "读回与输入不一致" in text


def test_fill_rich_text_selection_truncation_blocks_submit_even_when_word_already_exists(monkeypatch) -> None:
    """P1: in 'DONE is X' with 'X' selected, replacing with 'DONE' truncated to 'DONE is DO' must NOT press Enter."""
    session = FakeSession()
    session.state = {"tag": "div", "editable": True, "focused": True, "text": "DONE is X"}
    original_send = session.client.send

    async def truncating_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.insertText":
            session.client.calls.append((method, params or {}, session_id))
            session.state["text"] = "DONE is DO"
            return {"result": {"value": None}}
        return await original_send(method, params, session_id, timeout)

    session.client.send = truncating_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    **FOCUS_VALUE,
                    "text": "DONE is X",
                    "selectionText": "X",
                    "selectionPrefix": "DONE is ",
                    "selectionSuffix": "",
                },
            }
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "editor", "text": "DONE", "clear": False, "submit": True, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "未提交" in text
    assert all(
        not (call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter") for call in session.client.calls
    )


def test_fill_email_and_number_select_all_replacement_succeeds_and_submits(monkeypatch) -> None:
    """P2: email/number inputs report null selectionStart/End; select-all replacement to shorter text must succeed."""
    for input_type, old_val, new_val in (
        ("email", "old.address@example.com", "x@y.co"),
        ("number", "12345", "7"),
    ):
        session = FakeSession()
        session.state = {"tag": "input", "editable": False, "focused": True, "text": old_val}
        pages = [
            {"href": "http://example.com/form", "mutations": 0, "toasts": []},
            {"href": "http://example.com/done", "mutations": 0, "toasts": []},
        ]
        original_send = session.client.send

        async def replace_send(
            method,
            params=None,
            session_id=None,
            timeout=None,
            _session=session,
            _original_send=original_send,
        ):
            if method == "Input.insertText":
                _session.client.calls.append((method, params or {}, session_id))
                _session.state["text"] = str((params or {}).get("text", ""))
                return {"result": {"value": None}}
            return await _original_send(method, params, session_id, timeout)

        session.client.send = replace_send

        async def fake_call_value(
            _session,
            _object_id,
            function,
            _input_type=input_type,
            _old_val=old_val,
            _target_session=session,
            **_kwargs,
        ):
            if "this.focus" in function:
                return {
                    "type": "object",
                    "value": {
                        "focused": True,
                        "editable": False,
                        "tag": "input",
                        "type": _input_type,
                        "readOnly": False,
                        "disabled": False,
                        "text": _old_val,
                        "selectionStart": None,
                        "selectionEnd": None,
                        "selectionText": _old_val,
                    },
                }
            return {"type": "object", "value": dict(_target_session.state)}

        async def fake_eval_value(*_args, _pages=pages, **_kwargs):
            return _pages.pop(0) if len(_pages) > 1 else _pages[0]

        monkeypatch.setattr(fill, "call_value", fake_call_value)
        monkeypatch.setattr(fill, "eval_value", fake_eval_value)
        monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
        blocks = asyncio.run(
            fill.run(
                session,
                {"target": input_type, "text": new_val, "clear": False, "submit": True, "keypress_fallback": False},
            )
        )
        text = blocks[0]["text"]
        assert "status=succeeded" in text, f"{input_type} failed: {text}"
        assert "已提交（页面已跳转）" in text
        assert any(
            call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter" for call in session.client.calls
        )


def test_fill_multiline_rich_text_truncation_blocks_submit_using_consistent_text_representation(monkeypatch) -> None:
    """Multi-line contenteditable: Range.toString() vs textContent catches truncation 'DONE X\\nDON' and blocks Enter."""
    session = FakeSession()
    session.state = {
        "tag": "div",
        "editable": True,
        "focused": True,
        "text": "DONE X\nX",
        "rawText": "DONE XX",
    }
    original_send = session.client.send

    async def truncating_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.insertText":
            session.client.calls.append((method, params or {}, session_id))
            session.state["text"] = "DONE X\nDON"
            session.state["rawText"] = "DONE XDON"
            return {"result": {"value": None}}
        return await original_send(method, params, session_id, timeout)

    session.client.send = truncating_send

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {
                "type": "object",
                "value": {
                    **FOCUS_VALUE,
                    "text": "DONE X\nX",
                    "selectionText": "X",
                    "selectionPrefix": "DONE X",
                    "selectionSuffix": "",
                },
            }
        return {"type": "object", "value": dict(session.state)}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))
    blocks = asyncio.run(
        fill.run(
            session,
            {"target": "editor", "text": "DONE", "clear": False, "submit": True, "keypress_fallback": False},
        )
    )
    text = blocks[0]["text"]
    assert "status=uncertain" in text
    assert "未提交" in text
    assert all(
        not (call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter") for call in session.client.calls
    )


def test_fill_number_partial_repeated_selection_rejects_clamped_value_and_accepts_overlapping_match() -> None:
    """Number input without selectionStart/End: check exact candidate set including overlapping positions."""
    # Clamped by page handler: 101010 with "10" -> "50" clamped to "101500" must NOT match.
    assert (
        fill._values_match(
            "101500",
            "50",
            clear=False,
            before_value="101010",
            selection=None,
            selection_text="10",
            native_value=True,
        )
        is False
    )
    # Valid replacement at middle occurrence: 105010 matches.
    assert (
        fill._values_match(
            "105010",
            "50",
            clear=False,
            before_value="101010",
            selection=None,
            selection_text="10",
            native_value=True,
        )
        is True
    )
    # Overlapping occurrence: "1112" with 2nd+3rd "1" ("11") -> "7" yields "172" (as well as "712").
    assert (
        fill._values_match(
            "172",
            "7",
            clear=False,
            before_value="1112",
            selection=None,
            selection_text="11",
            native_value=True,
        )
        is True
    )
    assert (
        fill._values_match(
            "712",
            "7",
            clear=False,
            before_value="1112",
            selection=None,
            selection_text="11",
            native_value=True,
        )
        is True
    )


def test_fill_number_append_without_selection_rejects_clamped_value_and_accepts_true_insertion() -> None:
    """Number input without selection: appending '5' to '199' clamped to '1500' must NOT match."""
    assert (
        fill._values_match(
            "1500",
            "5",
            clear=False,
            before_value="199",
            selection=None,
            selection_text="",
            native_value=True,
        )
        is False
    )
    assert (
        fill._values_match(
            "1995",
            "5",
            clear=False,
            before_value="199",
            selection=None,
            selection_text="",
            native_value=True,
        )
        is True
    )
    assert (
        fill._values_match(
            "1599",
            "5",
            clear=False,
            before_value="199",
            selection=None,
            selection_text="",
            native_value=True,
        )
        is True
    )


def test_fill_rich_text_preserves_paragraph_boundaries_in_both_directions() -> None:
    """Rich-text visible-newline walk accepts 'First\\nSecond!' and rejects merged 'Shiptoday'."""
    # Direction 1: X! with X selected, replaced with First\nSecond -> visible First\nSecond! matches.
    assert (
        fill._values_match(
            "First\nSecond!",
            "First\nSecond",
            clear=False,
            before_value="X!",
            selection_text="X",
            selection_prefix="",
            selection_suffix="!",
            raw_value="First\nSecond!\n",
            native_value=False,
        )
        is True
    )
    # Direction 2: Ship\nX with X selected, replaced with today -> merged 'Shiptoday' must NOT match.
    assert (
        fill._values_match(
            "Shiptoday",
            "today",
            clear=False,
            before_value="Ship\nX",
            selection_text="X",
            selection_prefix="Ship\n",
            selection_suffix="\n",
            raw_value="Shiptoday",
            native_value=False,
        )
        is False
    )
    # And preserved paragraph break 'Ship\ntoday' DOES match.
    assert (
        fill._values_match(
            "Ship\ntoday",
            "today",
            clear=False,
            before_value="Ship\nX",
            selection_text="X",
            selection_prefix="Ship\n",
            selection_suffix="\n",
            raw_value="Ship\ntoday\n",
            native_value=False,
        )
        is True
    )


def test_fill_rich_text_rejects_newline_collapsed_to_space_and_skips_enter(monkeypatch) -> None:
    """When a page handler turns requested '\\n' into ' ', _values_match must reject and fill must not press Enter."""
    # Replacing selected 'X' in 'X!' with 'First\nSecond' when page flattens newline to 'First Second!'.
    assert (
        fill._values_match(
            "First Second!",
            "First\nSecond",
            clear=False,
            before_value="X!",
            selection_text="X",
            selection_prefix="",
            selection_suffix="!",
            raw_value="First Second!",
            native_value=False,
        )
        is False
    )
    # clear=True with explicit newline must also reject space-flattened readback.
    assert (
        fill._values_match(
            "First Second",
            "First\nSecond",
            clear=True,
            raw_value="First Second",
            native_value=False,
        )
        is False
    )
    assert (
        fill._values_match(
            "First\nSecond",
            "First\nSecond",
            clear=True,
            raw_value="First\nSecond\n",
            native_value=False,
        )
        is True
    )

    # End-to-end fill.run(..., submit=True): must NOT press Enter when newline was flattened to space.
    session = FakeSession()
    session.state = {
        "tag": "div",
        "editable": True,
        "focused": True,
        "text": "First Second!",
        "rawText": "First Second!",
    }
    session.client.state = session.state
    session.client.update_on_insert = False

    focus_with_sel = {
        **FOCUS_VALUE,
        "text": "X!",
        "selectionText": "X",
        "selectionPrefix": "",
        "selectionSuffix": "!",
    }

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(focus_with_sel)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(session.state)}
        return {}

    async def fake_eval_value(*_args, **_kwargs):
        return {"href": "https://example.com", "mutations": 0, "toasts": []}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(
        fill.run(
            session,
            {
                "target": "editor",
                "text": "First\nSecond",
                "clear": False,
                "submit": True,
                "keypress_fallback": False,
            },
        )
    )
    result_text = blocks[0]["text"]
    assert "未提交：读回与输入不一致" in result_text
    assert "status=uncertain" in result_text
    assert all(
        not (call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter") for call in session.client.calls
    )


def test_fill_rich_text_rejects_extra_blank_line_and_stripped_indentation() -> None:
    """_values_match must reject extra blank lines in innerText and stripped indentation in pre-wrap editors."""
    # Ordinary contenteditable: requested one\n\ntwo, Chrome innerText yielded one\n\n\ntwo!.
    assert (
        fill._values_match(
            "one\n\n\ntwo!",
            "one\n\ntwo",
            clear=False,
            before_value="X!",
            selection_text="X",
            selection_prefix="",
            selection_suffix="!",
            raw_value="one\n\ntwo!\n",
            native_value=False,
        )
        is False
    )
    # Same issue on clear=True when innerText has an extra blank line.
    assert (
        fill._values_match(
            "one\n\n\ntwo",
            "one\n\ntwo",
            clear=True,
            raw_value="one\n\ntwo\n",
            native_value=False,
        )
        is False
    )
    # Exact blank line count in both innerText and rawText matches.
    assert (
        fill._values_match(
            "one\n\ntwo!",
            "one\n\ntwo",
            clear=False,
            before_value="X!",
            selection_text="X",
            selection_prefix="",
            selection_suffix="!",
            raw_value="one\n\ntwo!",
            native_value=False,
        )
        is True
    )
    # Whitespace-preserving editor: stripping 4-space indentation must NOT match.
    assert (
        fill._values_match(
            "if ready:\nrun()",
            "if ready:\n    run()",
            clear=True,
            raw_value="if ready:\nrun()",
            native_value=False,
            preserve_whitespace=True,
        )
        is False
    )
    assert (
        fill._values_match(
            "if ready:\n    run()",
            "if ready:\n    run()",
            clear=True,
            raw_value="if ready:\n    run()",
            native_value=False,
            preserve_whitespace=True,
        )
        is True
    )


def test_fill_run_skips_enter_on_extra_blank_line_or_stripped_prewrap_indent(monkeypatch) -> None:
    """fill.run(submit=True) refuses to press Enter when innerText adds a blank line or strips pre-wrap indent."""
    # 1. Ordinary contenteditable: innerText has one\n\n\ntwo! while _visWalk rawText has one\n\ntwo!\n.
    session = FakeSession()
    session.state = {
        "tag": "div",
        "editable": True,
        "focused": True,
        "text": "one\n\n\ntwo!",
        "rawText": "one\n\ntwo!\n",
        "preserveWhitespace": False,
    }
    session.client.state = session.state
    session.client.update_on_insert = False
    focus_with_sel = {
        **FOCUS_VALUE,
        "text": "X!",
        "selectionText": "X",
        "selectionPrefix": "",
        "selectionSuffix": "!",
    }

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(focus_with_sel)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(session.state)}
        return {}

    async def fake_eval_value(*_args, **_kwargs):
        return {"href": "https://example.com", "mutations": 0, "toasts": []}

    monkeypatch.setattr(fill, "call_value", fake_call_value)
    monkeypatch.setattr(fill, "eval_value", fake_eval_value)
    monkeypatch.setattr(fill, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(
        fill.run(
            session,
            {
                "target": "editor",
                "text": "one\n\ntwo",
                "clear": False,
                "submit": True,
                "keypress_fallback": False,
            },
        )
    )
    assert "未提交：读回与输入不一致" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    assert all(
        not (call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter") for call in session.client.calls
    )

    # 2. Pre-wrap editor that strips 4-space indentation on clear=True: must NOT press Enter.
    session2 = FakeSession()
    session2.state = {
        "tag": "div",
        "editable": True,
        "focused": True,
        "text": "if ready:\nrun()",
        "rawText": "if ready:\nrun()",
        "preserveWhitespace": True,
    }
    session2.client.state = session2.state
    session2.client.update_on_insert = False

    async def fake_call_value2(_session, _object_id, function, **_kwargs):
        if "this.focus" in function:
            return {"type": "object", "value": dict(FOCUS_VALUE)}
        if "this.isContentEditable" in function:
            return {"type": "object", "value": dict(session2.state)}
        return {}

    monkeypatch.setattr(fill, "call_value", fake_call_value2)
    monkeypatch.setattr(fill, "_clear", lambda *_a, **_kw: asyncio.sleep(0))
    blocks2 = asyncio.run(
        fill.run(
            session2,
            {
                "target": "code",
                "text": "if ready:\n    run()",
                "clear": True,
                "submit": True,
                "keypress_fallback": False,
            },
        )
    )
    assert "未提交：读回与输入不一致" in blocks2[0]["text"]
    assert "status=uncertain" in blocks2[0]["text"]
    assert all(
        not (call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "Enter") for call in session2.client.calls
    )
    assert "preserveWhitespace" in actionability.FIELD_STATE_FN


def test_focus_fn_ignores_default_collapsed_caret_synthesized_by_focus() -> None:
    """FOCUS_FN checks selection before focus() so clear=False appends at the end of an unfocused contenteditable."""
    src = actionability.FOCUS_FN
    assert "hadSelectionBeforeFocus" in src
    assert src.index("hadSelectionBeforeFocus") < src.index("this.focus()")
    assert "!existing.collapsed" in src
