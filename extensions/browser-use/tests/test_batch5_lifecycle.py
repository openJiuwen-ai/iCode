# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit regression tests for Batch 5 lifecycle, concurrency & resource fixes (P1-P13)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from icode_browser_use import _proc, downloads
from icode_browser_use.bridge.server import BridgeServer
from icode_browser_use.broker import BrokerServer
from icode_browser_use.broker_client import BrokerClient
from icode_browser_use.cdp import launcher
from icode_browser_use.cdp.client import CDPClient
from icode_browser_use.cdp.websocket import WSClient
from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import BrowserError, ConnectionClosed, Unavailable
from icode_browser_use.journal import Journal
from icode_browser_use.policy import NetworkPolicy
from icode_browser_use.session import BrowserSession


def test_p1_popup_owned_before_policy_arms_closes_on_arm_failure() -> None:
    calls: list[tuple[str, dict[str, Any], str | None]] = []

    class _FakeClient:
        closed = False

        def on(self, _method: str, _handler: Any) -> None:
            return None

        async def send(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            *,
            session_id: str | None = None,
            timeout: float = 5.0,
        ) -> dict[str, Any]:
            calls.append((method, dict(params or {}), session_id))
            if method == "Fetch.enable" and session_id == "popup-sess":
                raise BrowserError("arm failed")
            return {}

    async def _run() -> None:
        cfg = BrowserConfig(allowed_domains=("example.com",))
        session = BrowserSession(cfg)
        client = _FakeClient()
        session._client = client
        session._target_id = "main-target"
        session._session_id = "main-sess"
        session._record_page("main-target", "main-sess", opener_id=None)
        policy = NetworkPolicy(("example.com",), owns_session=session._owns_session)
        policy.attach(client, register_attach_handler=False)
        policy._armed_sessions.add("main-sess")
        session._policy = policy

        # Duplicate attach for main tab must detach without calling Fetch.enable
        await session._on_attached_to_target(
            {"sessionId": "dup-sess", "targetInfo": {"type": "page", "targetId": "main-target"}},
            None,
        )
        assert not any(m == "Fetch.enable" and sid == "dup-sess" for m, _, sid in calls)
        assert any(m == "Target.detachFromTarget" and p.get("sessionId") == "dup-sess" for m, p, _ in calls)

        # Popup with openerId in _pages must be adopted BEFORE policy arms, so arm failure CLOSES it
        await session._on_attached_to_target(
            {
                "sessionId": "popup-sess",
                "targetInfo": {"type": "page", "targetId": "popup-target", "openerId": "main-target"},
            },
            None,
        )
        assert policy.arm_failed_pages == 1
        assert any(m == "Target.closeTarget" and p.get("targetId") == "popup-target" for m, p, _ in calls)

    asyncio.run(_run())


def test_p2_cdp_client_shuts_down_on_send_failure_and_orders_stateful_events() -> None:
    order: list[str] = []

    class _FakeWS:
        def __init__(self) -> None:
            self.closed = False
            self.fail_send = False
            self._queue: asyncio.Queue[str | None] = asyncio.Queue()
            self.pong_event = asyncio.Event()

        async def send(self, _text: str) -> None:
            if self.fail_send:
                raise OSError("broken pipe")

        async def recv(self) -> str | None:
            return await self._queue.get()

        async def ping(self) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True
            await self._queue.put(None)

    async def _run() -> None:
        ws = _FakeWS()
        client = CDPClient(ws)
        done = asyncio.Event()

        async def h1(params: dict[str, Any], _sid: str | None) -> None:
            order.append(f"start-{params['n']}")
            await asyncio.sleep(0.02 if params["n"] == 1 else 0.0)
            order.append(f"end-{params['n']}")
            if params["n"] == 2:
                done.set()

        client.on("Target.attachedToTarget", h1)
        client._start()
        try:
            await ws._queue.put(json.dumps({"method": "Target.attachedToTarget", "params": {"n": 1}}))
            await ws._queue.put(json.dumps({"method": "Target.attachedToTarget", "params": {"n": 2}}))
            await asyncio.wait_for(done.wait(), timeout=2.0)
            assert order == ["start-1", "end-1", "start-2", "end-2"]

            ws.fail_send = True
            with pytest.raises(ConnectionClosed):
                await client.send("Runtime.evaluate", timeout=1.0)
            assert client.closed
            assert ws.closed
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_p4_cmdline_matches_profile_with_spaces_and_rejects_prefix() -> None:
    profile = Path("/tmp/my state dir/chrome")
    # Linux argv list
    assert launcher._cmdline_matches_profile(
        ["/usr/bin/chrome", f"--user-data-dir={profile}", "--no-first-run"], profile
    )
    assert not launcher._cmdline_matches_profile(["/usr/bin/chrome", f"--user-data-dir={profile}2"], profile)
    # macOS unquoted string with spaces followed by next --flag
    assert launcher._cmdline_matches_profile(
        f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={profile} --no-first-run",
        profile,
    )
    assert not launcher._cmdline_matches_profile(
        f"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --user-data-dir={profile}2 --no-first-run",
        profile,
    )
    # Windows quoted path with spaces
    win_profile = Path(r"C:\Users\My User\state\chrome")
    assert launcher._cmdline_matches_profile(
        f'"C:\\Program Files\\Google\\Chrome\\chrome.exe" "--user-data-dir={win_profile}" --no-first-run',
        win_profile,
    )
    assert not launcher._cmdline_matches_profile(
        f'"C:\\Program Files\\Google\\Chrome\\chrome.exe" "--user-data-dir={win_profile}2" --no-first-run',
        win_profile,
    )


def test_p7_pid_alive_checks_current_and_invalid_pids() -> None:
    assert _proc.pid_alive(os.getpid()) is True
    assert _proc.pid_alive(0) is False
    assert _proc.pid_alive(-5) is False


def test_p8_broker_config_signature_includes_full_fields(tmp_path: Path) -> None:
    async def _run() -> None:
        cfg_a = BrowserConfig(
            allowed_domains=("example.com",),
            state_dir=tmp_path,
            save_state_enabled=True,
            cdp_url="http://127.0.0.1:9222",
        )
        server = BrokerServer(cfg_a, session_factory=lambda _c: SimpleNamespace(), tool_caller=lambda *_a: [])
        await server.start()
        try:
            data = json.loads(server.file.read_text(encoding="utf-8"))
            assert data["save_state"] is True
            assert data["cdp_url"] == "http://127.0.0.1:9222"
            assert data["chrome_devtools"] is False

            cfg_b = BrowserConfig(
                allowed_domains=("example.com",),
                state_dir=tmp_path,
                save_state_enabled=False,
                cdp_url="http://127.0.0.1:9222",
            )
            client = BrokerClient(cfg_b, owner="o1", session_id="s1")
            with pytest.raises(Unavailable, match="config differs"):
                await client.acquire()
        finally:
            await server.aclose()

    asyncio.run(_run())


def test_p9_handle_dialog_closes_as_gone_and_cancel_download_propagates_connection_closed() -> None:
    class _DeadClient:
        closed = False

        async def send(self, _method: str, _params: Any = None, **_kwargs: Any) -> dict[str, Any]:
            raise ConnectionClosed("gone")

    async def _run() -> None:
        session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
        session._client = _DeadClient()
        session._session_id = "s1"
        session._record_dialog("alert", "hi", "https://example.com", "t1", "s1")
        closed, sent = await session.handle_dialog(accept=True)
        assert sent is False
        assert closed.closed == "gone"

        session._downloads["g1"] = downloads.DownloadInfo(guid="g1", name="a.txt")
        session._download_order.append("g1")
        with pytest.raises(ConnectionClosed):
            await session.cancel_download("g1")

    asyncio.run(_run())


def test_p2_send_connection_closed_triggers_shutdown_and_slow_ordered_handler_does_not_block_responses() -> None:
    class _TimeoutWS:
        def __init__(self) -> None:
            self.closed = False
            self.fail_with_closed = False
            self._queue: asyncio.Queue[str | None] = asyncio.Queue()
            self.pong_event = asyncio.Event()

        async def send(self, text: str) -> None:
            if self.fail_with_closed:
                raise ConnectionClosed("write timed out")
            req = json.loads(text)
            await self._queue.put(json.dumps({"id": req["id"], "result": {"ok": True}}))

        async def recv(self) -> str | None:
            return await self._queue.get()

        async def ping(self) -> None:
            return None

        async def aclose(self) -> None:
            self.closed = True
            await self._queue.put(None)

    async def _run() -> None:
        ws = _TimeoutWS()
        client = CDPClient(ws)
        gate = asyncio.Event()

        async def slow_handler(_params: dict[str, Any], _sid: str | None) -> None:
            await gate.wait()

        client.on("Target.attachedToTarget", slow_handler)
        client._start()
        try:
            # Enqueue a slow ordered event; normal send/response must still resolve promptly
            await ws._queue.put(json.dumps({"method": "Target.attachedToTarget", "params": {}}))
            res = await client.send("Runtime.evaluate", timeout=1.0)
            assert res == {"ok": True}
            gate.set()

            # A ConnectionClosed raised by ws.send (e.g. send timeout) must trigger _shutdown()
            ws.fail_with_closed = True
            with pytest.raises(ConnectionClosed):
                await client.send("Runtime.evaluate", timeout=1.0)
            assert client.closed
            assert ws.closed
        finally:
            gate.set()
            await client.aclose()

    asyncio.run(_run())


def test_p10_log_rotation_and_journal_index(tmp_path: Path) -> None:
    log_file = tmp_path / "chrome.log"
    log_file.write_bytes(b"x" * 2048)
    _proc.rotate_log_if_needed(log_file, max_bytes=1024)
    assert not log_file.exists()
    assert (tmp_path / "chrome.log.1").stat().st_size == 2048

    db_path = tmp_path / "journal.sqlite3"
    j = Journal(db_path, owner="o", logical_session="s")
    j.record(action_id="a1", tool="browser_click", phase="result", status="succeeded", evidence="ok", document_epoch=1)
    j.close()
    conn = sqlite3.connect(db_path)
    try:
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "idx_actions_owner_session" in indexes
    finally:
        conn.close()


def test_p11_sanitize_filename_truncates_utf8_bytes() -> None:
    long_cjk = "测试文件名" * 40 + ".pdf"
    safe = downloads.sanitize_filename(long_cjk)
    assert safe.endswith(".pdf")
    assert len(safe.encode("utf-8")) <= downloads.NAME_BYTE_LIMIT


def test_p12_ws_client_rejects_masked_server_frame() -> None:
    async def _run() -> None:
        reader = asyncio.StreamReader()
        writer = SimpleNamespace(
            write=lambda _b: None,
            drain=lambda: asyncio.sleep(0),
            close=lambda: None,
            wait_closed=lambda: asyncio.sleep(0),
            transport=SimpleNamespace(abort=lambda: None),
        )
        ws = WSClient(reader, writer)
        # Masked server-to-client frame (FIN=1, OP_TEXT=1, MASK=1, len=2, key=0, payload=hi)
        reader.feed_data(bytes([0x81, 0x82, 0, 0, 0, 0]) + b"hi")
        reader.feed_eof()
        with pytest.raises(Unavailable, match="掩码"):
            await ws.recv()

    asyncio.run(_run())


def test_p13_bridge_server_aclose_fails_pending_waiter_before_unlock(tmp_path: Path) -> None:
    async def _run() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=10.0))
        await asyncio.sleep(0.02)
        await server.aclose()
        with pytest.raises(ConnectionClosed):
            await waiter
        assert not (tmp_path / "bridge.json").exists()

    asyncio.run(_run())
