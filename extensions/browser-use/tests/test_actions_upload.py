# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Upload journal phases: prepared before the send, result after it (W04)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from browser_use.actions import upload
from browser_use.errors import CDPTimeout
from browser_use.journal import Journal
from browser_use.snapshot.fusion import FusedNode


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append(method)
        return {}


def session(tmp_path, *, fail: bool = False) -> SimpleNamespace:
    upload_root = tmp_path / "upload"
    upload_root.mkdir(exist_ok=True)
    return SimpleNamespace(
        cfg=SimpleNamespace(cdp_timeout=1.0, upload_roots=(upload_root,)),
        session_id="main",
        target_id="t",
        page_epoch=5,
        snapshot=SimpleNamespace(
            nodes=[FusedNode(1, "f", "t", "button", "x", "button", 0, (0, 0, 10, 10), ref=1, epoch=5)]
        ),
        journal=Journal(None),
        client=FakeClient(),
        calls=[],
        resolve_target=lambda _target: SimpleNamespace(token="@e9#5", cdp_session_id=None),
        resolve_object_id=_object_id,
        require_client=lambda: None,
        mark_dirty=lambda: None,
        note_success=lambda _target: None,
        fail=fail,
    )


async def _object_id(_entry):
    return "obj"


def patch(monkeypatch, live) -> None:
    async def fake_call_value(*_args, **_kwargs):
        return {"value": {"ok": True, "count": 1}}

    async def send(method, params=None, session_id=None, timeout=None):
        live.calls.append(method)
        if live.fail:
            raise CDPTimeout("setFileInputFiles timed out", 1.0)
        return {}

    monkeypatch.setattr(upload, "call_value", fake_call_value)
    monkeypatch.setattr(upload, "new_action_id", lambda: "fixed")
    live.client.send = send
    live.require_client = lambda: live.client


def test_upload_records_prepared_then_result(tmp_path, monkeypatch) -> None:
    live = session(tmp_path)
    patch(monkeypatch, live)
    file = live.cfg.upload_roots[0] / "avatar.txt"
    file.write_text("payload", encoding="utf-8")
    phases: list[str] = []

    async def observing_send(method, params=None, session_id=None, timeout=None):
        row = live.journal.lookup("fixed")
        phases.append(str(row["phase"]) if row else "missing")
        return {}

    live.client.send = observing_send

    blocks = asyncio.run(upload.run(live, {"target": "avatar", "path": str(file)}))
    assert phases == ["prepared"]
    assert "action_id=fixed" in blocks[0]["text"]
    row = live.journal.lookup("fixed")
    assert row is not None
    assert row["phase"] == "result"
    assert row["status"] == "succeeded"
    assert "payload" not in str(row["evidence"])


def test_upload_dispatch_failure_is_uncertain(tmp_path, monkeypatch) -> None:
    live = session(tmp_path, fail=True)
    patch(monkeypatch, live)
    file = live.cfg.upload_roots[0] / "avatar.txt"
    file.write_text("payload", encoding="utf-8")
    blocks = asyncio.run(upload.run(live, {"target": "avatar", "path": str(file)}))
    assert "status=uncertain" in blocks[0]["text"]
    row = live.journal.lookup("fixed")
    assert row is not None
    assert row["status"] == "uncertain"


def test_upload_rejects_a_target_that_is_not_a_file_input(tmp_path, monkeypatch) -> None:
    live = session(tmp_path)
    patch(monkeypatch, live)
    file = live.cfg.upload_roots[0] / "avatar.txt"
    file.write_text("payload", encoding="utf-8")

    async def not_a_file(*_args, **_kwargs):
        return {"value": {"ok": False}}

    monkeypatch.setattr(upload, "call_value", not_a_file)
    with pytest.raises(Exception, match="file input"):
        asyncio.run(upload.run(live, {"target": "avatar", "path": str(file)}))
    assert live.journal.lookup("fixed") is None
    assert live.calls == []


def test_upload_reads_the_field_back_instead_of_trusting_the_send(tmp_path, monkeypatch) -> None:
    live = session(tmp_path)
    patch(monkeypatch, live)
    file = live.cfg.upload_roots[0] / "avatar.txt"
    file.write_text("payload", encoding="utf-8")

    async def empty_field(*_args, **_kwargs):
        return {"value": {"ok": True, "count": 0}}

    monkeypatch.setattr(upload, "call_value", empty_field)
    blocks = asyncio.run(upload.run(live, {"target": "avatar", "path": str(file)}))
    assert "字段回读为空" in blocks[0]["text"]
    assert "status=uncertain" in blocks[0]["text"]
    row = live.journal.lookup("fixed")
    assert row is not None
    assert row["status"] == "uncertain"


def test_upload_records_the_readback_count(tmp_path, monkeypatch) -> None:
    live = session(tmp_path)
    patch(monkeypatch, live)
    file = live.cfg.upload_roots[0] / "avatar.txt"
    file.write_text("payload", encoding="utf-8")

    async def one_file(*_args, **_kwargs):
        return {"value": {"ok": True, "count": 1}}

    monkeypatch.setattr(upload, "call_value", one_file)
    blocks = asyncio.run(upload.run(live, {"target": "avatar", "path": str(file)}))
    assert "字段回读 1 个文件" in blocks[0]["text"]
    row = live.journal.lookup("fixed")
    assert row is not None
    assert row["status"] == "succeeded"
