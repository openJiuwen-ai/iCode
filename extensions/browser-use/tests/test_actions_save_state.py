# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import save_state, upload
from icode_browser_use.errors import BadRequest


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, params))
        if method == "Network.getCookies":
            return {"cookies": [{"name": "a", "domain": "a.example"}]}
        return {}


def test_save_state_queries_only_the_current_origin_cookies(tmp_path, monkeypatch) -> None:
    client = FakeClient()
    session = SimpleNamespace(
        cfg=SimpleNamespace(save_state_enabled=True, state_archive_dir=tmp_path, cdp_timeout=1.0),
        snapshot=SimpleNamespace(url="https://wrong.example/page"),
        target_id="t",
        session_id="s",
        require_client=lambda: client,
        note_success=lambda _target: None,
    )

    async def fake_eval(_session, expression):
        return "{}" if expression == save_state._LS_EXPR else "https://a.example"

    monkeypatch.setattr(save_state, "eval_value", fake_eval)
    asyncio.run(save_state.run(session, {"name": "site-a"}))
    method, params = client.calls[0]
    assert method == "Network.getCookies"
    assert params == {"urls": ["https://a.example"]}
    text = (tmp_path / "site-a.json").read_text(encoding="utf-8")
    assert "a.example" in text
    assert "wrong.example" not in text


def test_save_state_refuses_an_unknown_origin(tmp_path, monkeypatch) -> None:
    session = SimpleNamespace(
        cfg=SimpleNamespace(save_state_enabled=True, state_archive_dir=tmp_path, cdp_timeout=1.0),
        snapshot=SimpleNamespace(url=""),
        target_id="t",
        session_id="s",
        require_client=lambda: FakeClient(),
        note_success=lambda _target: None,
    )

    async def empty_origin(_session, _expression):
        return ""

    monkeypatch.setattr(save_state, "eval_value", empty_origin)
    with pytest.raises(BadRequest):
        asyncio.run(save_state.run(session, {"name": "unknown"}))


def test_upload_rejects_when_no_root_is_configured(tmp_path) -> None:
    file = tmp_path / "input.txt"
    file.write_text("x", encoding="utf-8")
    session = SimpleNamespace(cfg=SimpleNamespace(upload_roots=()))
    with pytest.raises(BadRequest):
        asyncio.run(upload.run(session, {"target": "@e1#1", "path": str(file)}))
