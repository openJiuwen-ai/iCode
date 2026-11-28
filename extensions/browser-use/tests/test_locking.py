# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import icode_browser_use.extension as ext
from chrys.extensions import ToolContext
from icode_browser_use.errors import Unavailable
from icode_browser_use.locking import OWNER_META_FILE, acquire_instance_lock, probe_instance_lock
from icode_browser_use.prompts import BROWSER_PLAYBOOK


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


def test_fd_matches_path_detects_replaced_inode_and_sync_file_lock_round_trips(tmp_path: Path, monkeypatch) -> None:
    """Wave 3 P1-14 #4 & #1: inode verification detects replaced files; acquire_file_lock_sync acquires/releases."""
    from types import SimpleNamespace

    from icode_browser_use import locking

    lock_path = tmp_path / ".browsers.json.lock"
    sync_lock = locking.acquire_file_lock_sync(lock_path, wait=0.5)
    assert sync_lock is not None
    try:
        # Contention from another caller with wait=0 must fail immediately.
        assert locking.acquire_file_lock_sync(lock_path, wait=0.0) is None
    finally:
        sync_lock.release()
    # Once released, re-acquiring succeeds.
    reacquired = locking.acquire_file_lock_sync(lock_path, wait=0.5)
    assert reacquired is not None
    reacquired.release()

    # Verify POSIX inode mismatch detection deterministically on all platforms.
    monkeypatch.setattr(locking, "fcntl", object())
    monkeypatch.setattr(locking.os, "fstat", lambda _fd: SimpleNamespace(st_dev=1, st_ino=100))
    monkeypatch.setattr(locking.os, "stat", lambda _p: SimpleNamespace(st_dev=1, st_ino=200))
    assert locking._fd_matches_path(1, lock_path) is False
    monkeypatch.setattr(locking.os, "stat", lambda _p: SimpleNamespace(st_dev=1, st_ino=100))
    assert locking._fd_matches_path(1, lock_path) is True
