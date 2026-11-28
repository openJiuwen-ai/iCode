# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import query
from icode_browser_use.errors import BadRequest
from icode_browser_use.validation import validate


def test_parse_fields_supports_modes_arrays_and_blocks_unsafe_attributes() -> None:
    parsed = query.parse_fields(
        {
            "title": "h3",
            "price": ".price@exact_text",
            "url": "a[href^='https']@href",
            "sku": "@data-sku",
            "cells": "td[]",
            "links": "a[]@href",
            "badges": ".badge@count",
            "tag": "@tag",
        }
    )
    by_key = {f["key"]: f for f in parsed}
    assert by_key["title"] == {"key": "title", "selector": "h3", "mode": "text", "attr": "", "multi": False}
    assert by_key["price"] == {"key": "price", "selector": ".price", "mode": "exact_text", "attr": "", "multi": False}
    assert by_key["url"] == {
        "key": "url",
        "selector": "a[href^='https']",
        "mode": "attr",
        "attr": "href",
        "multi": False,
    }
    assert by_key["sku"] == {"key": "sku", "selector": "", "mode": "attr", "attr": "data-sku", "multi": False}
    assert by_key["cells"] == {"key": "cells", "selector": "td", "mode": "text", "attr": "", "multi": True}
    assert by_key["links"] == {"key": "links", "selector": "a", "mode": "attr", "attr": "href", "multi": True}
    assert by_key["badges"] == {"key": "badges", "selector": ".badge", "mode": "count", "attr": "", "multi": False}
    assert by_key["tag"] == {"key": "tag", "selector": "", "mode": "tag", "attr": "", "multi": False}

    # Disallowed / dangerous attributes are rejected before touching the page.
    for bad_spec in (
        "a@onclick",
        "iframe@srcdoc",
        "div@style",
        "div@data-auth-token",
        "div@data-session-id",
        "div@onerror",
    ):
        with pytest.raises(BadRequest, match="白名单"):
            query.parse_fields({"x": bad_spec})

    with pytest.raises(BadRequest):
        query.parse_fields({"bad key!": "h3"})
    with pytest.raises(BadRequest):
        query.parse_fields({"arr": "[]"})
    with pytest.raises(BadRequest):
        query.parse_fields({"cnt": ".a[]@count"})


def test_validation_validates_query_object_fields_and_limits() -> None:
    valid = validate(
        query.PARAMS,
        {"selector": ".card", "fields": {"title": "h3", "url": "a@href"}, "limit": 10},
    )
    assert valid["selector"] == ".card"
    assert valid["fields"] == {"title": "h3", "url": "a@href"}
    assert valid["visible_only"] is True
    assert valid["offset"] == 0
    assert valid["max_chars"] == 7000

    with pytest.raises(BadRequest, match="JSON 对象"):
        validate(query.PARAMS, {"selector": ".card", "fields": ["not", "an", "object"]})
    with pytest.raises(BadRequest, match="最多允许 16 个字段"):
        validate(query.PARAMS, {"selector": ".card", "fields": {f"k{i}": "span" for i in range(17)}})


def test_query_preserves_nbsp_scrubs_secrets_and_packs_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = {
        "url": "https://alice:secret@shop.example.com/list?token=SUPERSECRET&page=2",
        "total_matched": 3,
        "scan_capped": False,
        "items": [
            {
                "index": 0,
                "price": "$641.16\u00a0USD",
                "url": "https://bob:pw@shop.example.com/p/1?access_token=TOK123&color=red",
                "note": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SIG normal",
            },
            {
                "index": 1,
                "price": "€500,00\u00a0EUR",
                "url": "javascript:alert(1)",
                "note": "A" * 500,
            },
        ],
    }

    async def fake_eval(_session, _expr, **_kw):
        return raw

    monkeypatch.setattr(query, "eval_value", fake_eval)
    session = SimpleNamespace(target_id="t1", snapshot=SimpleNamespace(url=""), note_success=lambda _t: None)

    args = validate(
        query.PARAMS,
        {
            "selector": ".card",
            "fields": {"price": ".p@exact_text", "url": "a@href", "note": ".n"},
            "max_chars": 450,
        },
    )
    text = asyncio.run(query.run(session, args))[0]["text"]
    assert len(text) <= 450
    parsed = json.loads(text)
    assert parsed["url"] == "https://shop.example.com/list?token=***&page=2"
    assert "alice" not in text and "secret" not in text and "SUPERSECRET" not in text
    assert parsed["total_matched"] == 3
    assert parsed["returned"] == 1
    assert parsed["truncated"] is True
    assert parsed["next_offset"] == 1
    first = parsed["items"][0]
    assert first["price"] == "$641.16\u00a0USD"
    assert "bob" not in first["url"] and "TOK123" not in first["url"] and "color=red" in first["url"]
    assert "eyJhbGci" not in first["note"]


def test_query_supports_ref_root_scoping_and_invalid_selector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[str, str | None]] = []

    async def fake_call(_session, oid, _fn, *, cdp_session_id=None, **_kw):
        called.append((oid, cdp_session_id))
        return {
            "value": {
                "url": "https://frame.example.com/grid",
                "total_matched": 1,
                "scan_capped": False,
                "items": [{"index": 0, "tag": "li", "text": "Frame Row"}],
            }
        }

    monkeypatch.setattr(query, "call_value", fake_call)
    entry = SimpleNamespace(token="@e7#4", cdp_session_id="oopif-sess")
    session = SimpleNamespace(
        target_id="t1",
        snapshot=SimpleNamespace(url=""),
        note_success=lambda _t: None,
        resolve_target=lambda tok: entry if tok == "@e7#4" else None,
        resolve_object_id=lambda _e: asyncio.sleep(0, result="oid-7"),
    )
    args = validate(query.PARAMS, {"selector": "li", "root": "@e7#4"})
    parsed = json.loads(asyncio.run(query.run(session, args))[0]["text"])
    assert called == [("oid-7", "oopif-sess")]
    assert parsed["items"] == [{"index": 0, "tag": "li", "text": "Frame Row"}]

    async def fake_invalid(_session, _expr, **_kw):
        return {"error": "invalid_selector", "message": "not a valid selector"}

    monkeypatch.setattr(query, "eval_value", fake_invalid)
    with pytest.raises(BadRequest, match="CSS 选择器语法无效"):
        asyncio.run(query.run(session, validate(query.PARAMS, {"selector": "div[[["})))


def test_query_wave1_hardening_reserved_keys_urls_budget_and_hidden_oopif(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Reserved prototype / metadata keys are rejected before touching the page.
    for bad_key in ("__proto__", "constructor", "prototype", "index"):
        with pytest.raises(BadRequest, match="保留键"):
            query.parse_fields({bad_key: "h3"})

    # 2. Control-char javascript:, data:/blob:, protocol-relative & relative query URLs are cleaned.
    assert query._clean_url("\x01javascript:alert(1)") == ""
    assert query._clean_url("java\tscript:alert(1)") == ""
    assert query._clean_url("data:text/html,<script>alert(1)</script>") == "data:"
    assert query._clean_url("blob:https://example.com/uuid") == "blob:"
    proto_rel = query._clean_url("//alice:secret@example.com/cb?sig=LEAK1&color=red")
    assert "alice" not in proto_rel and "secret" not in proto_rel and "LEAK1" not in proto_rel
    assert "color=red" in proto_rel and proto_rel.startswith("//example.com/cb")
    rel_url = query._clean_url("/cb?sig=LEAK2&page=2")
    assert "LEAK2" not in rel_url and "page=2" in rel_url

    # 3. _pack_json_payload honors max_chars even with huge URLs and never loops next_offset == offset.
    packed = query._pack_json_payload(
        url="https://example.com/" + "a" * 300,
        selector=".item",
        total_matched=5,
        offset=2,
        items=[{"index": 2, "text": "x" * 500}],
        max_chars=180,
    )
    assert len(packed) <= 180
    obj = json.loads(packed)
    assert obj["returned"] == 0
    assert obj["item_too_large"] is True
    assert "next_offset" not in obj

    # _shrink_single_item never lengthens strings shorter than elem_cap.
    shrunk = query._shrink_single_item({"tags": ["12345678901234567890"]}, cap=60)
    assert shrunk["tags"] == ["12345678901234567890"]

    # 4. Hidden OOPIF ref root returns empty result without invoking call_value.
    invoked = False

    async def fake_vis(_session, _sid, *, fail_open=True):
        assert fail_open is False
        return False

    async def fail_call(*_a, **_kw):
        nonlocal invoked
        invoked = True
        raise AssertionError("hidden OOPIF must not be queried")

    monkeypatch.setattr(query, "is_oopif_visible", fake_vis)
    monkeypatch.setattr(query, "call_value", fail_call)
    entry = SimpleNamespace(token="@e7#4", cdp_session_id="oopif-hidden")
    session = SimpleNamespace(
        target_id="t1",
        snapshot=SimpleNamespace(url="https://parent.example/"),
        note_success=lambda _t: None,
        require_client=lambda: object(),
        resolve_target=lambda _tok: entry,
        resolve_object_id=lambda _e: asyncio.sleep(0, result="oid-7"),
    )
    out = json.loads(
        asyncio.run(query.run(session, validate(query.PARAMS, {"selector": "*", "root": "@e7#4"})))[0]["text"]
    )
    assert not invoked
    assert out["total_matched"] == 0 and out["items"] == []
