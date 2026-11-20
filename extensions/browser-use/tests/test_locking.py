# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import browser_use.extension as ext
from chrys.extensions import ToolContext
from browser_use.errors import Unavailable
from browser_use.locking import OWNER_META_FILE, acquire_instance_lock, probe_instance_lock
from browser_use.prompts import BROWSER_PLAYBOOK


def test_acquire_writes_owner_meta_and_release_cleans_it(tmp_path: Path) -> None:
    lock = asyncio.run(acquire_instance_lock(tmp_path, wait=0))
    owner_file = tmp_path / OWNER_META_FILE
    assert owner_file.is_file()
    meta = json.loads(owner_file.read_text(encoding="utf-8"))
    assert meta["pid"] == os.getpid()
    assert isinstance(meta["started_at"], float)

    lock.release()
    assert not owner_file.exists()
    assert probe_instance_lock(tmp_path).locked is False


def test_contended_lock_reports_structured_owner_and_never_leaks_bridge_token(tmp_path: Path) -> None:
    # Plant a bridge.json with a secret token to prove diagnostics never echo it.
    secret_token = "super-secret-bridge-token-9999"
    (tmp_path / "bridge.json").write_text(
        json.dumps({"port": 56789, "token": secret_token, "pid": os.getpid()}),
        encoding="utf-8",
    )

    lock = asyncio.run(acquire_instance_lock(tmp_path, wait=0))
    try:
        probe = probe_instance_lock(tmp_path)
        assert probe.locked is True
        assert probe.state == "locked_by_live_instance"
        assert probe.holder_pid == os.getpid()
        assert probe.holder_alive is True

        with pytest.raises(Unavailable) as exc_info:
            asyncio.run(acquire_instance_lock(tmp_path, wait=0))
        message = str(exc_info.value)
        assert "reason=state_dir_locked" in message
        assert "state=locked_by_live_instance" in message
        assert f"holder_pid={os.getpid()}" in message
        assert "holder_alive=true" in message
        assert "bridge.json" in message
        assert "Cookie" in message
        assert secret_token not in message
    finally:
        lock.release()


def test_browser_status_reports_contended_lock_without_launching(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.delenv("ICODE_BROWSER_BRIDGE", raising=False)
    monkeypatch.delenv("ICODE_BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("ICODE_BROWSER_BROKER", raising=False)

    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = ToolContext.for_testing(
        ws,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "sess",
        agent_name="lock-test",
        session_id="lock-test-session",
    )
    opened = asyncio.run(ext.browser_open(ctx))
    browser_id = opened.split(": ", 1)[1].strip()
    browser = ext._browsers[browser_id]
    try:
        held = asyncio.run(acquire_instance_lock(browser.session.cfg.state_dir, wait=0))
        try:
            status = asyncio.run(ext.browser_status(ctx, browser_id))
            assert "lock=locked_by_live_instance" in status
            assert f"holder_pid={os.getpid()}" in status
            assert "bridge.json/cookies" in status
            assert browser.session.started_once is False
        finally:
            held.release()
    finally:
        ext._browsers.pop(browser_id, None)


def test_playbook_forbids_lock_loop_and_credential_probing() -> None:
    assert "state_dir_locked" in BROWSER_PLAYBOOK
    assert "bridge.json" in BROWSER_PLAYBOOK
    assert "Cookie" in BROWSER_PLAYBOOK
