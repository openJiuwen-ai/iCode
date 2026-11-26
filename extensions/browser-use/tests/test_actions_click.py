# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import click
from icode_browser_use.errors import BadRequest, CDPTimeout, ConnectionClosed
from icode_browser_use.journal import RESULT, Journal
from icode_browser_use.snapshot.fusion import FusedNode


def _page(*, href: str = "https://example.com", mutations: int = 0, expanded: int = 0, dialogs: int = 0) -> dict:
    return {"href": href, "mutations": mutations, "expanded": expanded, "dialogs": dialogs, "active": "body"}


def test_classify_navigation() -> None:
    assert click._classify_effect(_page(), _page(href="https://example.com/next")) == "navigation"


def test_classify_dom_change() -> None:
    assert click._classify_effect(_page(), _page(mutations=3)) == "dom"


def test_classify_expand_change() -> None:
    assert click._classify_effect(_page(), _page(expanded=1)) == "dom"


def test_classify_focus_only_is_weak() -> None:
    after = _page()
    after["active"] = "button"
    assert click._classify_effect(_page(), after) == "focus"


def test_target_changed_uses_target_signals() -> None:
    assert click._target_changed({"contextLength": 1}, {"contextLength": 2}) is True
    assert click._target_changed({"checked": False}, {"checked": True}) is True
    assert click._target_changed({"contextLength": 1}, {"contextLength": 1}) is False
    assert click._target_changed({}, {"contextLength": 2}) is False


def test_classify_none() -> None:
    assert click._classify_effect(_page(), _page()) == "none"


def test_classify_empty_state_is_not_navigation() -> None:
    assert click._classify_effect({}, _page()) == "none"
    assert click._classify_effect(_page(), {}) == "none"


def test_submit_like_detection() -> None:
    assert click._is_submit_like({"name": "评论", "tag": "button", "type": ""}) is True
    assert click._is_submit_like({"name": "发布", "tag": "div", "type": ""}) is True
    assert click._is_submit_like({"name": "关注", "tag": "button", "type": ""}) is True
    assert click._is_submit_like({"name": "加入购物车", "tag": "button", "type": ""}) is True
    assert click._is_submit_like({"name": "Next", "tag": "button", "type": "button"}) is True
    assert click._is_submit_like({"name": "更多", "tag": "button", "type": "button"}) is False
    assert click._is_submit_like({"name": "提交", "tag": "input", "type": "submit"}) is True


def test_disabled_detection_uses_native_and_aria_only() -> None:
    assert click._target_disabled({"disabled": True, "ariaDisabled": False, "classDisabled": False}) is True
    assert click._target_disabled({"disabled": False, "ariaDisabled": True, "classDisabled": False}) is True
    assert click._target_disabled({"disabled": False, "ariaDisabled": False, "classDisabled": True}) is False
    assert click._target_disabled({"disabled": False, "ariaDisabled": False, "classDisabled": False}) is False


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append(method)
        if (params or {}).get("functionDeclaration") == click.HIT_TEST_FN:
            return {"result": {"value": {"inside": True, "tag": "BUTTON", "id": ""}}}
        return {"result": {"value": None}}


def _fresh_node(epoch: int) -> FusedNode:
    return FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=epoch)


class FakeSession:
    session_id = "main"
    target_id = "t"
    page_epoch = 7
    dialog_seq = 0
    cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0)

    def __init__(self) -> None:
        self.client = FakeClient()
        self.successes = 0
        self.journal = Journal(None)
        self.snapshot = SimpleNamespace(nodes=[_fresh_node(self.page_epoch)])

    def dialog_since(self, _seq):
        return None

    def resolve_target(self, _target):
        return SimpleNamespace(token="@e1#7", cdp_session_id=None)

    async def resolve_object_id(self, _entry):
        return "obj"

    def require_client(self):
        return self.client

    def oopif_offset(self, _session_id):
        return (0.0, 0.0)

    def mark_dirty(self) -> None:
        pass

    def note_success(self, _target_id) -> None:
        self.successes += 1


def test_disabled_target_is_not_clicked(monkeypatch) -> None:
    session = FakeSession()
    facts = {
        "tag": "button",
        "role": "button",
        "name": "评论",
        "type": "",
        "disabled": True,
        "ariaDisabled": False,
        "classDisabled": False,
    }

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": facts}

    async def fake_hit_point(*_args, **_kwargs):
        raise AssertionError("disabled target must not be hit")

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    blocks = asyncio.run(click.run(session, {"target": "评论", "button": "left", "click_count": 1}))
    assert "未点击" in blocks[0]["text"]
    assert "disabled" in blocks[0]["text"]
    assert "status=blocked" in blocks[0]["text"]
    assert session.client.calls == []


def _facts(name: str = "评论", *, tag: str = "button") -> dict:
    return {
        "tag": tag,
        "role": "button",
        "name": name,
        "type": "",
        "disabled": False,
        "ariaDisabled": False,
        "classDisabled": False,
    }


def test_submit_like_no_effect_does_not_use_js_fallback(monkeypatch) -> None:
    session = FakeSession()
    page = _page()
    fallback: list[bool] = []

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.click" in function:
            fallback.append(True)
            return {"type": "object", "value": True}
        return {"type": "object", "value": _facts("评论")}

    async def fake_eval_value(*_args, **_kwargs):
        return page

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)
    blocks = asyncio.run(click.run(session, {"target": "评论", "button": "left", "click_count": 1}))
    assert "effect=none" in blocks[0]["text"]
    assert "submit-like" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    assert fallback == []


def test_plain_button_no_effect_does_not_fall_back(monkeypatch) -> None:
    session = FakeSession()
    pages = [_page(), _page(), _page(), _page(), _page(), _page(mutations=1)]
    fallback: list[bool] = []
    facts_calls = 0

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        nonlocal facts_calls
        if "this.click" in function:
            fallback.append(True)
            return {"type": "object", "value": True}
        facts_calls += 1
        return {"type": "object", "value": {**_facts("更多"), "contextLength": facts_calls}}

    async def fake_eval_value(*_args, **_kwargs):
        assert pages
        return pages.pop(0) if len(pages) > 1 else pages[0]

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)
    blocks = asyncio.run(click.run(session, {"target": "更多", "button": "left", "click_count": 1}))
    assert fallback == []
    assert "effect=none" in blocks[0]["text"]


def test_overlay_blocks_the_click(monkeypatch) -> None:
    session = FakeSession()

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("更多")}

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_hit_test(*_args, **_kwargs):
        return {"inside": False, "tag": "DIV", "id": "cookie-banner"}

    async def fake_dispatch(*_args, **_kwargs):
        raise AssertionError("click must not be dispatched through an overlay")

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_hit_test", fake_hit_test)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    blocks = asyncio.run(click.run(session, {"target": "更多", "button": "left", "click_count": 1}))
    assert "遮挡" in blocks[0]["text"]
    assert "status=blocked" in blocks[0]["text"]
    assert "未发送任何事件" in blocks[0]["text"]
    assert session.successes == 0


def test_global_mutation_without_target_change_is_dom_elsewhere(monkeypatch) -> None:
    session = FakeSession()
    pages = [_page(), _page(mutations=1)]

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": {**_facts("更多"), "contextLength": 1}}

    async def fake_eval_value(*_args, **_kwargs):
        return pages.pop(0)

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)
    blocks = asyncio.run(click.run(session, {"target": "更多", "button": "left", "click_count": 1}))
    assert "effect=dom_elsewhere" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    assert session.successes == 0


def test_focus_only_effect_warns_about_submit(monkeypatch) -> None:
    session = FakeSession()
    after = _page()
    after["active"] = "button"
    pages = [_page(), after]

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("评论")}

    async def fake_eval_value(*_args, **_kwargs):
        assert pages
        return pages.pop(0) if len(pages) > 1 else pages[0]

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)
    blocks = asyncio.run(click.run(session, {"target": "评论", "button": "left", "click_count": 1}))
    assert "effect=focus" in blocks[0]["text"]
    assert "提交未确认" in blocks[0]["text"]
    assert "已提交" not in blocks[0]["text"]


def test_probe_timeout_after_click_is_uncertain(monkeypatch) -> None:
    session = FakeSession()
    calls = 0

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("评论")}

    async def fake_eval_value(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _page()
        raise CDPTimeout("Runtime.evaluate", 1.0)

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)
    blocks = asyncio.run(click.run(session, {"target": "评论", "button": "left", "click_count": 1}))
    assert "effect=unknown" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    assert session.successes == 0


def test_dispatch_timeout_returns_uncertain(monkeypatch) -> None:
    session = FakeSession()

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("评论")}

    async def fake_eval_value(*_args, **_kwargs):
        return _page()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        raise CDPTimeout("Input.dispatchMouseEvent", 1.0)

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    blocks = asyncio.run(click.run(session, {"target": "评论", "button": "left", "click_count": 1}))
    assert "status=uncertain" in blocks[0]["text"]
    assert "dispatch_failed" in blocks[0]["text"]
    assert session.successes == 0


def test_baseline_probe_failure_never_uses_js_fallback(monkeypatch) -> None:
    session = FakeSession()
    calls = 0
    fallback: list[bool] = []

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "this.click" in function:
            fallback.append(True)
            return {"type": "object", "value": True}
        return {"type": "object", "value": _facts("更多")}

    async def fake_eval_value(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CDPTimeout("Runtime.evaluate", 1.0)
        return _page()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        return None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)
    blocks = asyncio.run(click.run(session, {"target": "更多", "button": "left", "click_count": 1}))
    assert "effect=unknown" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    assert fallback == []


def test_click_records_prepared_before_the_send_and_the_result_after(monkeypatch) -> None:
    session = FakeSession()
    seen: list[str] = []

    async def fake_dispatch(*_args, **_kwargs):
        row = session.journal.lookup("fixed")
        seen.append(str(row["phase"]) if row else "missing")

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("更多")}

    async def fake_eval_value(*_args, **_kwargs):
        return _page()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "new_action_id", lambda: "fixed")
    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)

    asyncio.run(click.run(session, {"target": "更多", "button": "left", "click_count": 1}))
    assert seen == ["prepared"]
    row = session.journal.lookup("fixed")
    assert row is not None
    assert row["phase"] == "result"
    assert row["status"] in {"succeeded", "uncertain", "blocked"}


def test_a_click_that_loses_the_connection_stays_prepared(monkeypatch) -> None:
    session = FakeSession()

    async def failing_dispatch(*_args, **_kwargs):
        raise ConnectionClosed("cdp gone")

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("更多")}

    async def fake_eval_value(*_args, **_kwargs):
        return _page()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    monkeypatch.setattr(click, "new_action_id", lambda: "fixed")
    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", failing_dispatch)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    with pytest.raises(ConnectionClosed):
        asyncio.run(click.run(session, {"target": "更多", "button": "left", "click_count": 1}))
    row = session.journal.lookup("fixed")
    assert row is not None
    assert row["phase"] == "prepared"


def test_a_dialog_opened_by_the_click_blocks_the_result(monkeypatch) -> None:
    from icode_browser_use.dialog import DialogInfo

    session = FakeSession()
    info = DialogInfo(seq=1, kind="alert", message="fixture alert")
    dispatched: list[bool] = []
    session.dialog_since = lambda _seq: info if dispatched else None

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("更多")}

    async def fake_eval_value(*_args, **_kwargs):
        return _page()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        dispatched.append(True)

    monkeypatch.setattr(click, "new_action_id", lambda: "fixed")
    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)

    blocks = asyncio.run(click.run(session, {"target": "更多", "button": "left", "click_count": 1}))
    text = blocks[0]["text"]
    assert 'dialog=alert message="fixture alert"' in text
    assert "status=blocked retryable=false" in text
    assert "succeeded" not in text
    assert session.successes == 0
    assert dispatched == [True], "the click was never dispatched"
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked" and row["phase"] == "result"


# --- multi-point hit testing and dispatch=dom ------------------------------------------------

_OCCLUDED = {"inside": False, "tag": "DIV"}
_CORNERS = [(3.0, 3.0), (17.0, 3.0), (17.0, 17.0), (3.0, 17.0)]


def _install_hit_testing(
    monkeypatch,
    *,
    centres: list[tuple[float, float]],
    corners: list[list[tuple[float, float]]],
    hits: dict[tuple[float, float], dict],
    pages: list[dict],
    name: str = "更多",
    tag: str = "button",
    role: str = "button",
) -> dict:
    """Fake geometry: ``centres``/``corners`` are consumed per call, ``hits`` maps a point to its hit test.

    Any point missing from ``hits`` is occluded. Returns the call log (hit_point/corner counts,
    dispatched mouse points, dom clicks).
    """
    calls: dict = {"hit_point": 0, "corners": 0, "dispatched": [], "dom_clicks": 0}
    facts_calls = 0

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        nonlocal facts_calls
        if "this.click" in function:
            calls["dom_clicks"] += 1
            return {"type": "object", "value": True}
        facts_calls += 1
        return {"type": "object", "value": {**_facts(name, tag=tag), "role": role, "contextLength": facts_calls}}

    async def fake_eval_value(*_args, **_kwargs):
        return pages.pop(0) if len(pages) > 1 else pages[0]

    async def fake_hit_point(*_args, **_kwargs):
        calls["hit_point"] += 1
        return centres.pop(0) if len(centres) > 1 else centres[0]

    async def fake_corner_points(*_args, **_kwargs):
        calls["corners"] += 1
        return list(corners.pop(0) if len(corners) > 1 else corners[0])

    async def fake_hit_test(_session, _object_id, x, y, *_args, **_kwargs):
        return hits.get((x, y), dict(_OCCLUDED))

    async def fake_dispatch(_session, x, y, _button, _count):
        calls["dispatched"].append((x, y))

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_corner_points", fake_corner_points)
    monkeypatch.setattr(click, "_hit_test", fake_hit_test)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)
    return calls


def _click(session: FakeSession, **overrides) -> str:
    arguments = {"target": "更多", "button": "left", "click_count": 1, **overrides}
    return asyncio.run(click.run(session, arguments))[0]["text"]


def test_click_waits_for_the_same_controls_delayed_state_change(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[],
        hits={(10.0, 10.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page(), _page(mutations=1)],
    )
    labels = iter(["3456 Likes. Like", "3457 Likes. Like", "3458 Likes. Liked"])

    async def read_facts(*_args):
        label = next(labels)
        return {**_facts(label), "ariaLabel": label, "disabled": label == "3457 Likes. Like"}

    monkeypatch.setattr(click, "_target_facts", read_facts)
    text = _click(session, target="Like")
    assert "verified=control_state" in text and "status=succeeded" in text
    assert list(labels) == [], "a fluctuating count or a loading state must not confirm the click"
    assert calls["dispatched"] == [(10.0, 10.0)]


def test_occluded_centre_falls_back_to_a_corner_point(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(17.0, 17.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page(), _page(mutations=1)],
    )
    text = _click(session)
    assert calls["dispatched"] == [(17.0, 17.0)]
    assert "hit_point=corner" in text
    assert "effect=dom" in text
    assert "status=succeeded" in text
    assert session.successes == 1


def test_all_points_occluded_is_not_dispatched(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(click, "new_action_id", lambda: "fixed")
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    text = _click(session)
    assert calls["dispatched"] == []
    assert calls["dom_clicks"] == 0
    assert "遮挡" in text and "未发送任何事件" in text
    assert "status=blocked" in text
    assert "dispatch=dom 重试一次" in text
    assert "role=button" in text
    assert calls["corners"] == 1, "no clipper: the geometry is not re-read"
    assert calls["hit_point"] == 1
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "result" and row["status"] == "blocked"
    assert session.successes == 0


def test_clipper_rereads_the_geometry_and_hits_the_new_centre(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0), (10.0, 30.0)],
        corners=[_CORNERS],
        hits={
            (10.0, 10.0): {"inside": False, "tag": "DIV", "clipper": True},
            (10.0, 30.0): {"inside": True, "tag": "BUTTON"},
        },
        pages=[_page(), _page(mutations=1)],
    )
    text = _click(session)
    assert calls["hit_point"] == 2
    # Attempt 1 re-tests the centre before the corners, so the corners were only read once.
    assert calls["corners"] == 1
    assert calls["dispatched"] == [(10.0, 30.0)]
    assert "hit_point=corner" not in text
    assert "status=succeeded" in text


def test_clipper_tries_the_corners_again_after_rereading(monkeypatch) -> None:
    session = FakeSession()
    second_round = [(5.0, 25.0), (15.0, 25.0), (15.0, 35.0), (5.0, 35.0)]
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0), (10.0, 30.0)],
        corners=[_CORNERS, second_round],
        hits={
            (10.0, 10.0): {"inside": False, "tag": "DIV", "clipper": True},
            (10.0, 30.0): {"inside": False, "tag": "DIV", "clipper": True},
            (15.0, 35.0): {"inside": True, "tag": "BUTTON"},
        },
        pages=[_page(), _page(mutations=1)],
    )
    text = _click(session)
    assert calls["hit_point"] == 2
    assert calls["corners"] == 2
    assert calls["dispatched"] == [(15.0, 35.0)]
    assert "hit_point=corner" in text


def test_dom_dispatch_is_refused_without_a_prior_occluded_mouse_click(monkeypatch) -> None:
    session = FakeSession()
    monkeypatch.setattr(click, "new_action_id", lambda: "fixed")
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])

    async def never(*_args, **_kwargs):
        raise AssertionError("dispatch=dom must not hit-test")

    monkeypatch.setattr(click, "_hit_point", never)
    text = _click(session, dispatch="dom")
    assert "未点击 @e1#7" in text
    assert "status=blocked" in text
    assert "dispatch=dom 未执行" in text and "尚无 @e1#7 的 mouse 点击记录" in text
    assert calls["dom_clicks"] == 0
    assert calls["dispatched"] == []
    row = session.journal.lookup("fixed")
    assert row is not None and row["status"] == "blocked"


def test_dom_dispatch_is_refused_for_a_non_button(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()], tag="div", role=""
    )
    text = _click(session, dispatch="dom")
    assert "status=blocked" in text
    assert "button/a/input" in text
    assert "目标是 div" in text
    assert calls["dom_clicks"] == 0
    assert calls["dispatched"] == []


def test_dom_dispatch_after_an_occluded_mouse_click_on_the_same_ref(monkeypatch) -> None:
    session = FakeSession()
    ids = iter(["mouse1", "dom1"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    calls = _install_hit_testing(
        monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page(), _page(mutations=1)]
    )

    first = _click(session)
    assert "遮挡" in first
    rows = session.journal.rows_for("browser_click", session.page_epoch)
    assert [row["action_id"] for row in rows] == ["mouse1"]
    assert rows[0]["status"] == "blocked" and "@e1#7" in rows[0]["evidence"]

    inner_call, inner_eval = click.call_value, click.eval_value
    phase_at_click: list[str] = []
    phase_at_eval: list[str] = []
    functions: list[str] = []

    async def call_value(session_, object_id, function, **kwargs):
        if "this.click" in function:
            functions.append(function)
            row = session.journal.lookup("dom1")
            phase_at_click.append(str(row["phase"]) if row else "missing")
        return await inner_call(session_, object_id, function, **kwargs)

    async def eval_value(*args, **kwargs):
        row = session.journal.lookup("dom1")
        phase_at_eval.append(str(row["phase"]) if row else "missing")
        return await inner_eval(*args, **kwargs)

    monkeypatch.setattr(click, "call_value", call_value)
    monkeypatch.setattr(click, "eval_value", eval_value)

    second = _click(session, dispatch="dom")
    assert calls["dom_clicks"] == 1
    assert functions == [click._DOM_CLICK_FN]
    assert calls["dispatched"] == [], "dispatch=dom sends no mouse events"
    assert "dispatch=dom" in second
    assert "status=succeeded" in second
    assert phase_at_click == ["prepared"]
    assert phase_at_eval[0] == "missing" and phase_at_eval[1:] == ["dispatched"] * (len(phase_at_eval) - 1)
    row = session.journal.lookup("dom1")
    assert row is not None and row["phase"] == "result" and row["status"] == "succeeded"


def test_an_occluded_click_on_another_ref_does_not_unlock_dom_dispatch(monkeypatch) -> None:
    session = FakeSession()
    for action_id, token in (("other-ref", "@e2#7"), ("longer-epoch", "@e1#71")):
        session.journal.record(
            action_id=action_id,
            tool="browser_click",
            phase=RESULT,
            status="blocked",
            evidence=f"未点击 {token}：目标中心与四角均被其他元素遮挡（命中 DIV）",
            document_epoch=session.page_epoch,
        )
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    text = _click(session, dispatch="dom")
    assert "status=blocked" in text
    assert "@e1#7" in text
    assert calls["dom_clicks"] == 0
    assert calls["dispatched"] == []


def test_a_corner_click_that_landed_does_not_unlock_dom_dispatch(monkeypatch) -> None:
    """The corner-fallback note mentions the covered centre, but the click landed: dom stays gated."""
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(17.0, 17.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page(), _page(mutations=1)],
    )

    async def unchanged_facts(_session, _object_id, function, **_kwargs):
        if "this.click" in function:
            calls["dom_clicks"] += 1
            return {"type": "object", "value": True}
        return {"type": "object", "value": {**_facts("更多"), "role": "button"}}

    monkeypatch.setattr(click, "call_value", unchanged_facts)
    first = _click(session)
    assert calls["dispatched"] == [(17.0, 17.0)]
    assert "hit_point=corner" in first and "中心被遮挡" in first
    assert "effect=dom_elsewhere" in first and "status=uncertain" in first

    second = _click(session, dispatch="dom")
    assert "status=blocked" in second and "可能已生效" in second and "不要再点击" in second
    assert "请先用默认 dispatch=mouse" not in second, "a landed click must not be followed by another mouse click"
    assert calls["dom_clicks"] == 0
    assert calls["dispatched"] == [(17.0, 17.0)], "the gated dom dispatch sends nothing"


def test_a_failed_hit_test_never_dispatches(monkeypatch) -> None:
    """An unanswered hit test is no confirmation: nothing is sent and the model is told to re-snapshot."""
    session = FakeSession()
    monkeypatch.setattr(click, "new_action_id", lambda: "fixed")
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])

    async def unanswered(*_args, **_kwargs):
        return None

    monkeypatch.setattr(click, "_hit_test", unanswered)
    text = _click(session)
    assert calls["dispatched"] == [] and calls["dom_clicks"] == 0
    assert calls["corners"] == 0, "no point is tried when the centre's hit test did not answer"
    assert "命中测试失败" in text and "未发送任何事件" in text and "status=uncertain" in text
    row = session.journal.lookup("fixed")
    assert row is not None and row["phase"] == "result" and row["status"] == "uncertain"


def test_a_second_dom_dispatch_on_the_same_ref_is_refused(monkeypatch) -> None:
    session = FakeSession()
    ids = iter(["mouse1", "dom1", "dom2"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    calls = _install_hit_testing(
        monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page(), _page(mutations=1)]
    )
    assert "遮挡" in _click(session)
    assert "status=succeeded" in _click(session, dispatch="dom")
    third = _click(session, dispatch="dom")
    assert "status=blocked" in third and "已用过一次" in third
    assert calls["dom_clicks"] == 1
    row = session.journal.lookup("dom2")
    assert row is not None and row["status"] == "blocked"


def test_a_dom_refusal_row_does_not_hide_the_landed_mouse_click(monkeypatch) -> None:
    """Refusal rows are the plugin's own words: the newest *page* record decides, and it landed."""
    session = FakeSession()
    ids = iter(["mouse1", "dom1", "dom2"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(10.0, 10.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page(), _page(mutations=1)],
    )
    assert "status=succeeded" in _click(session)
    second = _click(session, dispatch="dom")
    assert "结果为 succeeded（可能已生效）" in second and "status=blocked" in second
    third = _click(session, dispatch="dom")
    assert "结果为 succeeded（可能已生效）" in third, "the refusal row must not be read as the newest mouse record"
    assert calls["dom_clicks"] == 0 and calls["dispatched"] == [(10.0, 10.0)]


def test_a_dom_refusal_row_does_not_hide_the_occluded_mouse_click(monkeypatch) -> None:
    """An occluded mouse click, then a dom refusal elsewhere, still unlocks dom dispatch for this ref."""
    session = FakeSession()
    ids = iter(["mouse1", "dom1", "dom2"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    pages = [_page()]
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=pages)
    assert "status=blocked" in _click(session)
    session.journal.record(
        action_id="other",
        tool="browser_click",
        phase=RESULT,
        status="blocked",
        evidence=f"未点击 @e2#7：{click._DOM_REFUSED}：x",
        document_epoch=session.page_epoch,
    )
    pages[:] = [_page(), _page(mutations=1)]
    second = _click(session, dispatch="dom")
    assert "dispatch=dom" in second and "status=succeeded" in second
    assert calls["dom_clicks"] == 1


def test_occluded_then_landed_mouse_click_keeps_dom_gated(monkeypatch) -> None:
    session = FakeSession()
    ids = iter(["mouse1", "mouse2", "dom1"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    hits: dict = {}
    calls = _install_hit_testing(
        monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits=hits, pages=[_page(), _page(mutations=1)]
    )
    assert "遮挡" in _click(session)
    hits[(10.0, 10.0)] = {"inside": True, "tag": "BUTTON"}
    assert "status=succeeded" in _click(session)
    third = _click(session, dispatch="dom")
    assert "status=blocked" in third and "可能已生效" in third
    assert calls["dom_clicks"] == 0 and calls["dispatched"] == [(10.0, 10.0)]


def test_no_effect_on_a_submit_like_target_does_not_unlock_dom_dispatch(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(10.0, 10.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page()],
        name="发送",
    )
    first = _click(session)
    assert "effect=none" in first and "status=uncertain" in first
    second = _click(session, dispatch="dom")
    assert "status=blocked" in second and "提交类目标" in second and "不能证明未提交" in second
    assert calls["dom_clicks"] == 0 and calls["dispatched"] == [(10.0, 10.0)]


def test_no_effect_on_a_plain_button_unlocks_dom_dispatch_once(monkeypatch) -> None:
    session = FakeSession()
    pages = [_page()]
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(10.0, 10.0): {"inside": True, "tag": "BUTTON"}},
        pages=pages,
    )
    first = _click(session)
    assert "effect=none" in first
    pages[:] = [_page(), _page(mutations=1)]
    second = _click(session, dispatch="dom")
    assert "dispatch=dom" in second and "status=succeeded" in second
    assert calls["dom_clicks"] == 1
    third = _click(session, dispatch="dom")
    assert "status=blocked" in third and "已用过一次" in third
    assert calls["dom_clicks"] == 1


def test_an_unanswered_corner_hit_test_is_unverified_not_occluded(monkeypatch) -> None:
    """One corner probe failing means occlusion is not established: nothing is sent and dom stays locked."""
    session = FakeSession()
    ids = iter(["mouse1", "dom1"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    real_hit_test = click._hit_test

    async def failing_corner(session_, object_id, x, y, *args, **kwargs):
        if (x, y) == _CORNERS[1]:
            raise CDPTimeout("Runtime.callFunctionOn", 1.0)
        return await real_hit_test(session_, object_id, x, y, *args, **kwargs)

    monkeypatch.setattr(click, "_hit_test", failing_corner)
    first = _click(session)
    assert "命中测试失败" in first and "status=uncertain" in first
    assert click._ALL_OCCLUDED not in first and "dispatch=dom" not in first
    assert calls["dispatched"] == []
    second = _click(session, dispatch="dom")
    assert "status=blocked" in second and "上一次命中测试失败" in second
    assert calls["dom_clicks"] == 0


def test_an_unanswered_corner_geometry_is_unverified_not_occluded(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])

    async def failing_corners(*_args, **_kwargs):
        raise CDPTimeout("DOM.getContentQuads", 1.0)

    monkeypatch.setattr(click, "_corner_points", failing_corners)
    text = _click(session)
    assert "命中测试失败" in text and "status=uncertain" in text
    assert click._ALL_OCCLUDED not in text and calls["dispatched"] == []


def test_a_hit_test_failure_row_names_itself_when_dom_dispatch_is_asked(monkeypatch) -> None:
    session = FakeSession()
    ids = iter(["mouse1", "dom1"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])

    async def unanswered(*_args, **_kwargs):
        raise CDPTimeout("Runtime.callFunctionOn", 1.0)

    monkeypatch.setattr(click, "_hit_test", unanswered)
    assert "命中测试失败" in _click(session)
    second = _click(session, dispatch="dom")
    assert "上一次命中测试失败，未发送任何事件" in second and "可能已生效" not in second
    assert calls["dom_clicks"] == 0


def test_an_in_flight_click_row_keeps_dom_dispatch_shut(monkeypatch) -> None:
    """A click that never reached its result row may have landed: the gate treats it as such."""
    session = FakeSession()
    session.journal.record(
        action_id="stuck",
        tool="browser_click",
        phase="dispatched",
        status="dispatched",
        evidence="dispatched click on @e1#7 (button=left, click_count=1)",
        document_epoch=session.page_epoch,
        target_ref="@e1#7",
        dispatch="mouse",
    )
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    text = _click(session, dispatch="dom")
    assert "status=blocked" in text and "尚无结果记录（phase=dispatched" in text
    assert calls["dom_clicks"] == 0


def test_page_text_quoting_the_occlusion_marker_does_not_open_the_gate(monkeypatch) -> None:
    """The occluded marker counts only as a structured reason, never inside a quoted dialog."""
    session = FakeSession()
    session.journal.record(
        action_id="dialog",
        tool="browser_click",
        phase=RESULT,
        status="blocked",
        evidence=f"已发送点击 @e1#7（button=left, click_count=1）\ndialog=alert message={click._ALL_OCCLUDED}\n对话框出现",
        document_epoch=session.page_epoch,
        target_ref="@e1#7",
        dispatch="mouse",
        reason="dialog",
        effect="dialog",
    )
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    text = _click(session, dispatch="dom")
    assert "status=blocked" in text and "结果为 blocked（可能已生效）" in text
    assert calls["dom_clicks"] == 0


def test_page_text_quoting_a_dom_refusal_keeps_the_newest_row(monkeypatch) -> None:
    """Only a structured dom_refused row is skipped, not a landed click whose dialog quotes it."""
    session = FakeSession()
    session.journal.record(
        action_id="occluded",
        tool="browser_click",
        phase=RESULT,
        status="blocked",
        evidence=f"未点击 @e1#7：{click._ALL_OCCLUDED}，未发送任何事件",
        document_epoch=session.page_epoch,
        target_ref="@e1#7",
        dispatch="not_sent",
        reason="all_occluded",
    )
    session.journal.record(
        action_id="dialog",
        tool="browser_click",
        phase=RESULT,
        status="blocked",
        evidence=(
            "已发送点击 @e1#7（button=left, click_count=1）\n"
            f"dialog=alert message={click._DOM_REFUSED} during the click\n对话框出现"
        ),
        document_epoch=session.page_epoch,
        target_ref="@e1#7",
        dispatch="mouse",
        reason="dialog",
        effect="dialog",
    )
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    text = _click(session, dispatch="dom")
    assert "status=blocked" in text and "结果为 blocked（可能已生效）" in text
    assert calls["dom_clicks"] == 0


def test_a_dom_dispatch_stays_spent_after_another_occluded_mouse_click(monkeypatch) -> None:
    """One dom click per ref, even when a later mouse click is occluded again and re-offers dom."""
    session = FakeSession()
    ids = iter(["mouse1", "dom1", "mouse2", "dom2"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    calls = _install_hit_testing(
        monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page(), _page(mutations=1)]
    )
    assert "遮挡" in _click(session)
    assert "status=succeeded" in _click(session, dispatch="dom")
    assert "遮挡" in _click(session), "the second mouse click is still occluded and sends nothing"
    fourth = _click(session, dispatch="dom")
    assert "status=blocked" in fourth and "已用过一次" in fourth
    assert calls["dom_clicks"] == 1


def test_a_dom_dispatch_that_never_recorded_a_result_stays_spent(monkeypatch) -> None:
    """A crashed dom dispatch (no result row) is still a dom use: a later occluded click cannot reopen it."""
    session = FakeSession()
    session.journal.record(
        action_id="dom-crash",
        tool="browser_click",
        phase="dispatched",
        status="dispatched",
        evidence="dispatched click on @e1#7 (button=left, click_count=1, dispatch=dom)",
        document_epoch=session.page_epoch,
        target_ref="@e1#7",
        dispatch="dom",
    )
    session.journal.record(
        action_id="occluded-later",
        tool="browser_click",
        phase=RESULT,
        status="blocked",
        evidence=f"未点击 @e1#7：{click._ALL_OCCLUDED}，未发送任何事件",
        document_epoch=session.page_epoch,
        target_ref="@e1#7",
        dispatch="not_sent",
        reason="all_occluded",
    )
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    text = _click(session, dispatch="dom")
    assert "status=blocked" in text and "已用过一次" in text
    assert calls["dom_clicks"] == 0


def test_a_blocked_click_that_never_sent_is_not_reported_as_landed(monkeypatch) -> None:
    session = FakeSession()
    session.journal.record(
        action_id="disabled",
        tool="browser_click",
        phase=RESULT,
        status="blocked",
        evidence="未点击 @e1#7：目标处于 disabled 状态",
        document_epoch=session.page_epoch,
        target_ref="@e1#7",
        dispatch="not_sent",
        reason="disabled",
    )
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    text = _click(session, dispatch="dom")
    assert "status=blocked" in text and "不能绕过" in text and "可能已生效" not in text
    assert calls["dom_clicks"] == 0


def test_an_unreadable_target_after_a_dom_effect_is_unknown_not_elsewhere(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(10.0, 10.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page(), _page(mutations=1)],
    )
    real = click._target_facts
    seen = {"n": 0}

    async def flaky(session_, object_id, cdp_session_id):
        seen["n"] += 1
        if seen["n"] == 1:
            return await real(session_, object_id, cdp_session_id)
        return {}

    monkeypatch.setattr(click, "_target_facts", flaky)
    text = _click(session)
    assert "effect=unknown" in text
    assert "dom_elsewhere" not in text
    assert calls["dispatched"] == [(10.0, 10.0)]


def test_a_dom_click_with_no_effect_does_not_offer_dom_again(monkeypatch) -> None:
    session = FakeSession()
    ids = iter(["mouse1", "dom1"])
    monkeypatch.setattr(click, "new_action_id", lambda: next(ids))
    calls = _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    assert "遮挡" in _click(session)
    second = _click(session, dispatch="dom")
    assert "effect=none" in second
    assert "dispatch=dom 重试一次" not in second
    assert calls["dom_clicks"] == 1


def test_detached_target_after_click_reports_target_detached_not_unknown(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(10.0, 10.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page(), _page(mutations=1)],
    )
    facts_seq = iter([{**_facts("确定"), "connected": True}, {"connected": False}])

    async def fake_facts(*_args, **_kwargs):
        return next(facts_seq)

    monkeypatch.setattr(click, "_target_facts", fake_facts)
    text = _click(session)
    assert "dispatch=sent" in text
    assert "effect=target_detached" in text
    assert "status=succeeded" in text
    assert "探针失败" not in text
    assert calls["dispatched"] == [(10.0, 10.0)]


def test_input_submit_async_toggle_waits_and_verifies_control_state(monkeypatch) -> None:
    session = FakeSession()
    calls = _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(10.0, 10.0): {"inside": True, "tag": "INPUT"}},
        pages=[_page()] * 10,
    )
    before_facts = {
        "connected": True,
        "tag": "input",
        "role": "",
        "name": "Follow zhuyuloveai",
        "type": "submit",
        "value": "Follow",
        "ariaLabel": "Follow zhuyuloveai",
        "disabled": False,
        "ariaDisabled": False,
        "classDisabled": False,
    }
    after_facts = {
        **before_facts,
        "value": "Unfollow",
        "ariaLabel": "Unfollow zhuyuloveai",
        "name": "Unfollow zhuyuloveai",
    }
    reads = [before_facts, before_facts, before_facts, after_facts]

    async def fake_facts(*_args, **_kwargs):
        return reads.pop(0) if len(reads) > 1 else reads[0]

    monkeypatch.setattr(click, "_target_facts", fake_facts)
    text = _click(session)
    assert "dispatch=sent" in text
    assert "effect=dom" in text
    assert "verified=control_state" in text
    assert "status=succeeded" in text
    assert calls["dispatched"] == [(10.0, 10.0)]


def test_target_facts_distinguishes_detached_node_from_probe_failure() -> None:
    from icode_browser_use.actions import actionability
    from icode_browser_use.errors import BrowserError, StaleRef

    async def raise_stale(*_args, **_kwargs):
        raise StaleRef("gone")

    async def raise_detached(*_args, **_kwargs):
        raise BrowserError("Could not find node with given id")

    async def raise_other(*_args, **_kwargs):
        raise BrowserError("unexpected runtime error")

    assert asyncio.run(actionability.target_facts(FakeSession(), "o", None, call=raise_stale)) == {"connected": False}
    assert asyncio.run(actionability.target_facts(FakeSession(), "o", None, call=raise_detached)) == {
        "connected": False
    }
    assert asyncio.run(actionability.target_facts(FakeSession(), "o", None, call=raise_other)) == {}


def test_click_reports_dispatch_state_for_blocked_and_dispatch_timeout(monkeypatch) -> None:
    session = FakeSession()
    _install_hit_testing(monkeypatch, centres=[(10.0, 10.0)], corners=[_CORNERS], hits={}, pages=[_page()])
    blocked = _click(session)
    assert "dispatch=not_sent" in blocked
    assert "status=blocked" in blocked

    async def timeout_dispatch(*_args, **_kwargs):
        raise CDPTimeout("Input.dispatchMouseEvent", 1.0)

    _install_hit_testing(
        monkeypatch,
        centres=[(10.0, 10.0)],
        corners=[_CORNERS],
        hits={(10.0, 10.0): {"inside": True, "tag": "BUTTON"}},
        pages=[_page()],
    )
    monkeypatch.setattr(click, "_dispatch_click", timeout_dispatch)
    timed_out = _click(session)
    assert "dispatch=unknown" in timed_out
    assert "status=uncertain" in timed_out


def test_no_layout_object_cdp_error_is_translated_to_bad_request() -> None:
    from icode_browser_use.errors import BadRequest, BrowserError

    session = FakeSession()

    async def no_layout_send(method, params=None, session_id=None, timeout=None):
        if method == "DOM.scrollIntoViewIfNeeded":
            raise BrowserError('{"code":-32000,"message":"Node does not have a layout object"}')
        return {"result": {"value": None}}

    session.client.send = no_layout_send
    with pytest.raises(BadRequest) as exc_info:
        asyncio.run(click._visible_quad(session, "obj", None))
    assert "无可见布局盒模型" in str(exc_info.value)
    assert "[in-view]" in str(exc_info.value)


def test_click_opening_new_tab_reports_new_tab_and_tab_id(monkeypatch) -> None:
    """Independent #7: clicking a link that creates a child tab reports effect=new_tab and tab_id."""
    session = FakeSession()
    pages = [SimpleNamespace(target_id="t")]
    session.pages = lambda: list(pages)

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("Open Docs", tag="a")}

    async def fake_eval_value(*_args, **_kwargs):
        return _page()

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        pages.append(SimpleNamespace(target_id="t-child-2"))

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(click.run(session, {"target": "Open Docs", "button": "left", "click_count": 1}))
    text = blocks[0]["text"]
    assert "effect=new_tab" in text
    assert "tab_id=t-child-2" in text
    assert "status=succeeded" in text
    assert session.successes == 1


def test_click_keeps_settle_window_on_focus_change_to_catch_async_new_tab(monkeypatch) -> None:
    """Edge case #5: when click immediately changes focus and opens a new tab 100ms later, still detect new_tab."""
    session = FakeSession()
    pages = [SimpleNamespace(target_id="t")]
    session.pages = lambda: list(pages)
    poll_count = 0

    async def fake_call_value(*_args, **_kwargs):
        return {"type": "object", "value": _facts("Async Popup", tag="button")}

    async def fake_eval_value(*_args, **_kwargs):
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return _page()
        after = _page()
        after["active"] = "button"
        if poll_count >= 3 and len(pages) == 1:
            pages.append(SimpleNamespace(target_id="t-async-2"))
        return after

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(click.run(session, {"target": "Async Popup", "button": "left", "click_count": 1}))
    text = blocks[0]["text"]
    assert "effect=new_tab" in text
    assert "tab_id=t-async-2" in text
    assert "status=succeeded" in text
    assert session.successes == 1


def test_click_new_tab_hint_respects_compact_toolset(monkeypatch) -> None:
    session = FakeSession()
    session.cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0, toolset="compact")
    pages = [SimpleNamespace(target_id="t", url="https://example.com/")]
    session.pages = lambda: list(pages)  # type: ignore[attr-defined]
    page = {"href": "https://example.com/", "mutations": 0, "expanded": 0, "dialogs": 0, "active": "a", "toasts": []}

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "submitLike" in function:
            return {"type": "object", "value": {"connected": True, "tag": "a", "name": "new tab", "submitLike": False}}
        return {}

    async def fake_eval_value(*_args, **_kwargs):
        return page

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        pages.append(SimpleNamespace(target_id="t-child-9", url="https://example.com/tall"))

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(click.run(session, {"target": "new tab", "button": "left", "click_count": 1}))
    text = blocks[0]["text"]
    assert "effect=new_tab" in text
    assert "tab_id=t-child-9" in text
    assert "可用 browser_tab_switch" not in text
    assert "compact 工具集不包含标签切换工具" in text
    assert "url=https://example.com/tall" in text
    assert "browser_navigate 到 https://example.com/tall" in text

    # When the newly opened tab's cached page.url is still blank/about:blank,
    # fall back to the clicked link's resolved href; when neither has an http(s) URL,
    # do not recommend browser_navigate without a URL.
    pages2 = [SimpleNamespace(target_id="t", url="https://example.com/")]
    session2 = FakeSession()
    session2.cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0, toolset="compact")
    session2.pages = lambda: list(pages2)  # type: ignore[attr-defined]

    async def fake_call_value_href(_session, _object_id, function, **_kwargs):
        if "submitLike" in function:
            return {
                "type": "object",
                "value": {
                    "connected": True,
                    "tag": "a",
                    "name": "new tab",
                    "href": "https://example.com/from-href",
                    "submitLike": False,
                },
            }
        return {}

    async def fake_dispatch_blank(*_args, **_kwargs):
        pages2.append(SimpleNamespace(target_id="t-child-10", url="about:blank"))

    monkeypatch.setattr(click, "call_value", fake_call_value_href)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch_blank)
    blocks2 = asyncio.run(click.run(session2, {"target": "new tab", "button": "left", "click_count": 1}))
    text2 = blocks2[0]["text"]
    assert "url=https://example.com/from-href" in text2
    assert "browser_navigate 到 https://example.com/from-href" in text2

    pages3 = [SimpleNamespace(target_id="t", url="https://example.com/")]
    session3 = FakeSession()
    session3.cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0, toolset="compact")
    session3.pages = lambda: list(pages3)  # type: ignore[attr-defined]

    async def fake_call_value_no_href(_session, _object_id, function, **_kwargs):
        if "submitLike" in function:
            return {
                "type": "object",
                "value": {"connected": True, "tag": "button", "name": "Popup", "submitLike": False},
            }
        return {}

    async def fake_dispatch_empty(*_args, **_kwargs):
        pages3.append(SimpleNamespace(target_id="t-child-11", url=""))

    monkeypatch.setattr(click, "call_value", fake_call_value_no_href)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch_empty)
    blocks3 = asyncio.run(click.run(session3, {"target": "Popup", "button": "left", "click_count": 1}))
    text3 = blocks3[0]["text"]
    assert "未能获取新标签 URL" in text3
    assert "browser_list" in text3
    assert "browser_navigate" not in text3


def test_click_resolves_delayed_popup_url_and_bridge_late_adoption(monkeypatch) -> None:
    pages = [SimpleNamespace(target_id="t", url="https://example.com/popup")]
    session = FakeSession()
    session.cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0, toolset="compact")
    session._transport = SimpleNamespace(browser_commands=False)  # type: ignore[attr-defined]
    session.pages = lambda: list(pages)  # type: ignore[attr-defined]

    list_calls = 0

    async def fake_list_pages():
        nonlocal list_calls
        list_calls += 1
        if list_calls >= 3:
            pages[1].url = "https://example.com/preview/runtime-token-42"
        return list(pages)

    session.list_pages = fake_list_pages  # type: ignore[attr-defined]
    mut_count = 0

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "submitLike" in function:
            return {
                "type": "object",
                "value": {"connected": True, "tag": "button", "name": "Open preview", "submitLike": False},
            }
        return {}

    async def fake_eval_value(*_args, **_kwargs):
        return {
            "href": "https://example.com/popup",
            "mutations": mut_count,
            "expanded": 0,
            "dialogs": 0,
            "active": "button",
            "toasts": [],
        }

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        nonlocal mut_count
        mut_count = 1  # opener DOM mutated synchronously before Bridge adopts the popup

    sleep_ticks = 0

    async def fake_sleep(_s):
        nonlocal sleep_ticks
        sleep_ticks += 1
        if sleep_ticks == 1 and len(pages) == 1:
            pages.append(SimpleNamespace(target_id="t-popup-late", url="about:blank"))
        await asyncio.sleep(0)

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", fake_sleep)

    blocks = asyncio.run(click.run(session, {"target": "Open preview", "button": "left", "click_count": 1}))
    text = blocks[0]["text"]
    assert "effect=new_tab" in text
    assert "tab_id=t-popup-late" in text
    assert "url=https://example.com/preview/runtime-token-42" in text
    assert "browser_navigate 到 https://example.com/preview/runtime-token-42" in text


def test_click_polls_observed_tab_url_instead_of_link_fallback_href(monkeypatch) -> None:
    pages = [SimpleNamespace(target_id="t", url="https://example.com/")]
    session = FakeSession()
    session.cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0, toolset="compact")
    session.pages = lambda: list(pages)  # type: ignore[attr-defined]
    list_calls = 0

    async def fake_list_pages():
        nonlocal list_calls
        list_calls += 1
        if list_calls >= 3:
            pages[1].url = "https://example.com/report.pdf"
        return list(pages)

    session.list_pages = fake_list_pages  # type: ignore[attr-defined]

    async def fake_call_value(_session, _object_id, function, **_kwargs):
        if "submitLike" in function:
            return {
                "type": "object",
                "value": {
                    "connected": True,
                    "tag": "a",
                    "name": "Download report",
                    "href": "https://example.com/fallback",
                    "submitLike": False,
                },
            }
        return {}

    async def fake_eval_value(*_args, **_kwargs):
        return {
            "href": "https://example.com/",
            "mutations": 0,
            "expanded": 0,
            "dialogs": 0,
            "active": "a",
            "toasts": [],
        }

    async def fake_hit_point(*_args, **_kwargs):
        return 10.0, 10.0

    async def fake_dispatch(*_args, **_kwargs):
        pages.append(SimpleNamespace(target_id="t-report", url="about:blank"))

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval_value)
    monkeypatch.setattr(click, "_hit_point", fake_hit_point)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(click.run(session, {"target": "Download report", "button": "left", "click_count": 1}))
    text = blocks[0]["text"]
    assert "url=https://example.com/report.pdf" in text
    assert "/fallback" not in text


def test_dispatch_click_double_click_sends_two_press_release_pairs() -> None:
    session = FakeSession()
    recorded: list[tuple[str, dict]] = []

    class _RecordingClient:
        async def send(self, method, params=None, session_id=None, timeout=None):
            recorded.append((method, dict(params or {})))
            return {"result": {"value": None}}

    session.client = _RecordingClient()  # type: ignore[assignment]
    asyncio.run(click._dispatch_click(session, 12.0, 34.0, "left", 2))
    mouse_events = [
        (params["type"], params["clickCount"], params["buttons"])
        for method, params in recorded
        if method == "Input.dispatchMouseEvent"
    ]
    assert mouse_events == [
        ("mouseMoved", 0, 0),
        ("mousePressed", 1, 1),
        ("mouseReleased", 1, 0),
        ("mousePressed", 2, 1),
        ("mouseReleased", 2, 0),
    ]


def test_click_offset_clicks_exact_subregion_and_validates_bounds(monkeypatch) -> None:
    session = FakeSession()
    dispatched: list[tuple[float, float]] = []

    async def fake_call_value(_session, _oid, fn, **_kwargs):
        if "submitLike" in fn:
            return {"type": "object", "value": {"connected": True, "tag": "canvas", "name": "", "submitLike": False}}
        if "elementFromPoint" in fn:
            return {"type": "object", "value": {"inside": True, "clipper": False, "shadowRetargeted": False}}
        return {}

    async def fake_eval(*_args, **_kwargs):
        return {"href": "https://example.com", "mutations": 0, "expanded": 0, "dialogs": 0, "active": "canvas"}

    async def fake_quad(*_args, **_kwargs):
        return [100.0, 50.0, 420.0, 50.0, 420.0, 170.0, 100.0, 170.0], (0.0, 0.0)

    async def fake_dispatch(_s, x, y, _b, _c):
        dispatched.append((x, y))

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval)
    monkeypatch.setattr(click, "_visible_quad_with_offset", fake_quad)
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(
        click.run(session, {"target": "canvas", "button": "left", "click_count": 1, "offset_x": 75.0, "offset_y": 60.0})
    )
    assert dispatched == [(175.0, 110.0)]
    assert "hit_point=offset(75,60)" in blocks[0]["text"]

    with pytest.raises(BadRequest, match="超出目标区域"):
        asyncio.run(
            click.run(
                session, {"target": "canvas", "button": "left", "click_count": 1, "offset_x": 400.0, "offset_y": 60.0}
            )
        )
    with pytest.raises(BadRequest, match="必须同时传入"):
        asyncio.run(click.run(session, {"target": "canvas", "button": "left", "click_count": 1, "offset_x": 10.0}))
    with pytest.raises(BadRequest, match="dispatch=dom 不支持"):
        asyncio.run(
            click.run(
                session,
                {
                    "target": "canvas",
                    "button": "left",
                    "click_count": 1,
                    "dispatch": "dom",
                    "offset_x": 10.0,
                    "offset_y": 10.0,
                },
            )
        )


def test_click_new_tab_skips_post_write_expect_without_waiting(monkeypatch) -> None:
    pages = [SimpleNamespace(target_id="t", url="https://example.com/a")]
    session = FakeSession()
    session.pages = lambda: list(pages)  # type: ignore[attr-defined]

    async def fake_call_value(_session, _oid, fn, **_kwargs):
        if "submitLike" in fn:
            return {"type": "object", "value": {"connected": True, "tag": "a", "name": "Open B", "submitLike": False}}
        return {}

    async def fake_eval(*_args, **_kwargs):
        return {"href": "https://example.com/a", "mutations": 0, "expanded": 0, "dialogs": 0, "active": "a"}

    async def fake_dispatch(*_args, **_kwargs):
        pages.append(SimpleNamespace(target_id="t-b", url="https://example.com/b"))

    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval)
    monkeypatch.setattr(click, "_hit_point", lambda *_a, **_kw: asyncio.sleep(0, result=(10.0, 10.0)))
    monkeypatch.setattr(click, "_dispatch_click", fake_dispatch)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(
        click.run(
            session,
            {
                "target": "Open B",
                "button": "left",
                "click_count": 1,
                "expect_condition": "text",
                "expect_value": "Shop B",
                "expect_timeout_ms": 5000,
            },
        )
    )
    text = blocks[0]["text"]
    assert "effect=new_tab" in text
    assert "expect_status=skipped reason=new_tab_opened" in text


def test_double_click_aborts_second_click_when_first_replaces_target(monkeypatch) -> None:
    session = FakeSession()
    attached = True
    mouse_events: list[tuple[str, int]] = []

    async def fake_call_value(_session, _oid, fn, **_kwargs):
        if "submitLike" in fn:
            return {
                "type": "object",
                "value": {"connected": attached, "tag": "button", "name": "Open record", "submitLike": False},
            }
        return {}

    async def fake_eval(*_args, **_kwargs):
        return {
            "href": "https://example.com/row",
            "mutations": 0 if attached else 1,
            "expanded": 0,
            "dialogs": 0,
            "active": "button",
        }

    async def fake_hit_test(_s, _oid, _x, _y, _sid, _off, *, entry=None):
        if attached:
            return {"connected": True, "disabled": False, "inside": True}
        return {"connected": False, "disabled": False, "inside": False}

    orig_send = session.client.send

    async def send_detaching(method, params=None, session_id=None, timeout=None):
        nonlocal attached
        if method == "Input.dispatchMouseEvent":
            mouse_events.append((str((params or {}).get("type")), int((params or {}).get("clickCount") or 0)))
            if (params or {}).get("type") == "mouseReleased" and (params or {}).get("clickCount") == 1:
                attached = False  # click 1 replaced Open record with Archive record at same coords
        return await orig_send(method, params=params, session_id=session_id, timeout=timeout)

    session.client.send = send_detaching  # type: ignore[method-assign]
    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval)
    monkeypatch.setattr(click, "_hit_point", lambda *_a, **_kw: asyncio.sleep(0, result=(50.0, 20.0)))
    monkeypatch.setattr(click, "_safe_hit_test", fake_hit_test)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(click.run(session, {"target": "Open record", "button": "left", "click_count": 2}))
    text = blocks[0]["text"]
    # Second click must NEVER be dispatched after click 1 detached the target.
    assert ("mousePressed", 1) in mouse_events and ("mouseReleased", 1) in mouse_events
    assert ("mousePressed", 2) not in mouse_events and ("mouseReleased", 2) not in mouse_events
    assert "dispatched_clicks=1/2" in text
    assert "stop_reason=target_detached" in text
    assert "status=uncertain retryable=false" in text
    assert "status=succeeded" not in text


def test_double_click_sends_both_clicks_when_target_stays_at_point(monkeypatch) -> None:
    session = FakeSession()
    mouse_events: list[tuple[str, int]] = []

    async def fake_call_value(_session, _oid, fn, **_kwargs):
        if "submitLike" in fn:
            return {
                "type": "object",
                "value": {"connected": True, "tag": "button", "name": "Open", "submitLike": False},
            }
        return {}

    async def fake_eval(*_args, **_kwargs):
        return {"href": "https://example.com/row", "mutations": 0, "expanded": 0, "dialogs": 0, "active": "button"}

    async def fake_hit_test(_s, _oid, _x, _y, _sid, _off, *, entry=None):
        return {"connected": True, "disabled": False, "inside": True}

    orig_send = session.client.send

    async def record_send(method, params=None, session_id=None, timeout=None):
        if method == "Input.dispatchMouseEvent":
            mouse_events.append((str((params or {}).get("type")), int((params or {}).get("clickCount") or 0)))
        return await orig_send(method, params=params, session_id=session_id, timeout=timeout)

    session.client.send = record_send  # type: ignore[method-assign]
    monkeypatch.setattr(click, "call_value", fake_call_value)
    monkeypatch.setattr(click, "eval_value", fake_eval)
    monkeypatch.setattr(click, "_hit_point", lambda *_a, **_kw: asyncio.sleep(0, result=(50.0, 20.0)))
    monkeypatch.setattr(click, "_safe_hit_test", fake_hit_test)
    monkeypatch.setattr(click, "_sleep", lambda _s: asyncio.sleep(0))

    blocks = asyncio.run(click.run(session, {"target": "Open", "button": "left", "click_count": 2}))
    assert ("mousePressed", 1) in mouse_events and ("mouseReleased", 1) in mouse_events
    assert ("mousePressed", 2) in mouse_events and ("mouseReleased", 2) in mouse_events
    assert "dispatched_clicks=" not in blocks[0]["text"]
