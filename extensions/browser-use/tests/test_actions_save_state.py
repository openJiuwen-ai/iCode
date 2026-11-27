# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import save_state, upload
from icode_browser_use.errors import BadRequest


class FakeClient:
    def __init__(self, frame_url: str = "https://a.example/app") -> None:
        self.calls: list[tuple[str, object]] = []
        self.frame_url = frame_url

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": self.frame_url}}} if self.frame_url else {}
        if method == "Network.getCookies":
            return {"cookies": [{"name": "a", "domain": "a.example"}]}
        return {}


def test_save_state_queries_only_the_current_origin_cookies(tmp_path, monkeypatch) -> None:
    client = FakeClient("https://a.example")
    session = SimpleNamespace(
        cfg=SimpleNamespace(save_state_enabled=True, state_archive_dir=tmp_path, cdp_timeout=1.0),
        snapshot=SimpleNamespace(url="https://wrong.example/page"),
        target_id="t",
        session_id="s",
        require_client=lambda: client,
        note_success=lambda _target: None,
    )

    async def fake_eval(_session, expression):
        assert expression == save_state._LS_EXPR
        return "{}"

    monkeypatch.setattr(save_state, "eval_value", fake_eval)
    asyncio.run(save_state.run(session, {"name": "site-a"}))
    cookie_calls = [(m, p) for m, p in client.calls if m == "Network.getCookies"]
    assert cookie_calls == [("Network.getCookies", {"urls": ["https://a.example"]})]
    text = (tmp_path / "site-a.json").read_text(encoding="utf-8")
    assert "a.example" in text
    assert "wrong.example" not in text


def test_save_state_ignores_spoofed_main_world_origin_and_uses_cdp_frame_tree(tmp_path, monkeypatch) -> None:
    """P0-1: untrusted page JS spoofing window.String/origin cannot redirect Network.getCookies."""
    client = FakeClient("https://current.example/dashboard")
    session = SimpleNamespace(
        cfg=SimpleNamespace(save_state_enabled=True, state_archive_dir=tmp_path, cdp_timeout=1.0),
        snapshot=SimpleNamespace(url="https://victim.example/"),
        target_id="t",
        session_id="s",
        require_client=lambda: client,
        note_success=lambda _target: None,
    )

    async def spoofed_eval(_session, _expression):
        return {
            "origin": "https://victim.example",
            "href": "https://victim.example/admin",
            "storage": '{"k": "v"}',
        }

    monkeypatch.setattr(save_state, "eval_value", spoofed_eval)
    asyncio.run(save_state.run(session, {"name": "cdp-auth"}))
    cookie_calls = [(m, p) for m, p in client.calls if m == "Network.getCookies"]
    assert cookie_calls == [("Network.getCookies", {"urls": ["https://current.example/dashboard"]})]
    text = (tmp_path / "cdp-auth.json").read_text(encoding="utf-8")
    assert "https://current.example" in text
    assert "victim.example" not in text


def test_save_state_refuses_an_unknown_origin(tmp_path, monkeypatch) -> None:
    session = SimpleNamespace(
        cfg=SimpleNamespace(save_state_enabled=True, state_archive_dir=tmp_path, cdp_timeout=1.0),
        snapshot=SimpleNamespace(url=""),
        target_id="t",
        session_id="s",
        require_client=lambda: FakeClient(frame_url=""),
        note_success=lambda _target: None,
    )

    async def empty_origin(_session, _expression):
        return ""

    monkeypatch.setattr(save_state, "eval_value", empty_origin)
    with pytest.raises(BadRequest):
        asyncio.run(save_state.run(session, {"name": "unknown"}))


def test_save_state_rejects_symlinked_archive_directory(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link_dir = tmp_path / "archive_link"
    try:
        link_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform/privilege level")
    client = FakeClient("https://a.example/app")
    session = SimpleNamespace(
        cfg=SimpleNamespace(save_state_enabled=True, state_archive_dir=link_dir, cdp_timeout=1.0),
        snapshot=SimpleNamespace(url="https://a.example/app"),
        target_id="t",
        session_id="s",
        require_client=lambda: client,
        note_success=lambda _target: None,
    )

    async def fake_eval(_session, _expression):
        return "{}"

    monkeypatch.setattr(save_state, "eval_value", fake_eval)
    with pytest.raises(BadRequest, match="符号链接归档目录"):
        asyncio.run(save_state.run(session, {"name": "site-a"}))


def test_upload_rejects_when_no_root_is_configured(tmp_path) -> None:
    file = tmp_path / "input.txt"
    file.write_text("x", encoding="utf-8")
    session = SimpleNamespace(cfg=SimpleNamespace(upload_roots=()))
    with pytest.raises(BadRequest):
        asyncio.run(upload.run(session, {"target": "@e1#1", "path": str(file)}))
