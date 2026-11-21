# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

import icode_browser_use.broker as broker_module

from icode_browser_use.bridge.protocol import encode_message
from icode_browser_use.broker import BrokerServer
from icode_browser_use.broker_client import BrokerClient, BrokerSession
from icode_browser_use.config import BrowserConfig


@pytest.mark.parametrize("chosen", ["chosen", ""])
def test_stop_resolves_state_without_allowlist(tmp_path, monkeypatch, chosen):
    from pathlib import Path

    for name in tuple(os.environ):
        if name.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / chosen) if chosen else "")
    monkeypatch.setattr(sys, "argv", ["broker", "--stop", "--purge-profiles"])
    captured = []
    monkeypatch.setattr(broker_module, "_purge_profiles", captured.append)
    # Avoid contacting any broker in the real default state directory.
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    broker_module.main()
    assert captured == [tmp_path / chosen if chosen else Path.home() / ".cache" / "icode-browser-use"]


def _cfg(tmp_path) -> BrowserConfig:
    return BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path)


def test_broker_config_keeps_controlled_downloads_off(monkeypatch, tmp_path) -> None:
    for key in tuple(os.environ):
        if key.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ICODE_BROWSER_DOWNLOADS", "allow")
    cfg = broker_module._run_config()
    assert cfg.download_dir is None
    assert cfg.download_max_bytes > 0


def test_broker_session_exposes_the_config_for_screenshot_retention(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    session = BrokerSession(cfg, owner="owner-a", session_id="one")
    assert session.cfg is cfg


def test_broker_reuses_a_session_for_the_same_owner(tmp_path) -> None:
    created: list[object] = []
    calls: list[tuple[str, object]] = []

    async def tool_caller(_session, tool, arguments):
        calls.append((tool.name, arguments))
        return [{"type": "text", "text": "ok"}]

    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: created.append(object()) or created[-1],
            tool_caller=tool_caller,
        )
        await broker.start()
        try:
            first = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="session-1")
            await first.acquire()
            blocks = await first.call("browser_navigate", {"url": "https://example.com"})
            assert blocks[0]["text"] == "ok"
            await first.aclose()

            second = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="session-1")
            await second.acquire()
            assert second.lease_id != first.lease_id
            assert len(created) == 1
            assert (await second.call("browser_navigate", {"url": "https://example.com"}))[0]["text"] == "ok"
            assert calls == [
                ("browser_navigate", {"url": "https://example.com"}),
                ("browser_navigate", {"url": "https://example.com"}),
            ]

            await second.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_rejects_another_owner(tmp_path) -> None:
    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: SimpleNamespace(),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            first = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="session-1")
            await first.acquire()
            await first.aclose()
            second = BrokerClient(_cfg(tmp_path), owner="owner-b", session_id="session-1")
            try:
                await second.acquire()
            except Exception as exc:
                assert "another client" in str(exc)
            else:
                raise AssertionError("another owner must not acquire the same session")
            await second.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_releases_a_lease_when_the_client_disconnects(tmp_path) -> None:
    created: list[object] = []

    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: created.append(object()) or created[-1],
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            first = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="session-1")
            await first.acquire()
            assert len(broker.leases) == 1
            assert first._writer is not None
            first._writer.close()
            await first._writer.wait_closed()
            first._reader = None
            first._writer = None
            for _ in range(20):
                if not broker.leases:
                    break
                await asyncio.sleep(0.01)
            assert broker.leases == {}

            second = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="session-1")
            await second.acquire()
            assert len(created) == 1
            await second.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_aclose_with_a_connected_client(tmp_path) -> None:
    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: SimpleNamespace(),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        client = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="session-1")
        await client.acquire()
        await asyncio.wait_for(broker.aclose(), timeout=2)
        assert broker._server is None
        await client.aclose()

    asyncio.run(scenario())


def test_broker_rejects_a_bad_token(tmp_path) -> None:
    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: SimpleNamespace(),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", broker.port)
            writer.write(encode_message({"token": "wrong"}))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(1), timeout=2) == b""
            writer.close()
            await writer.wait_closed()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_allows_a_second_session(tmp_path) -> None:
    captured: list[BrowserConfig] = []

    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda session_cfg: captured.append(session_cfg) or SimpleNamespace(),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            first = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="one")
            await first.acquire()
            second = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="two")
            await second.acquire()
            assert second.lease_id != first.lease_id
            assert len(broker.sessions) == 2
            assert captured[0].state_dir != captured[1].state_dir
            assert captured[0].state_dir != tmp_path
            await second.aclose()
            await first.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_rejects_a_second_live_lease(tmp_path) -> None:
    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: SimpleNamespace(),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            first = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="one")
            await first.acquire()
            second = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="one")
            try:
                await second.acquire()
            except Exception as exc:
                assert "live lease" in str(exc)
            else:
                raise AssertionError("a live lease must block a second client")
            await second.aclose()
            await first.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_evicts_an_idle_session_for_a_third(tmp_path) -> None:
    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: SimpleNamespace(state="idle"),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            clients = [
                BrokerClient(_cfg(tmp_path), owner="owner-a", session_id=session_id) for session_id in ("one", "two")
            ]
            for client in clients:
                await client.acquire()
            await clients[0].aclose()
            await asyncio.sleep(0.05)
            third = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="three")
            await third.acquire()
            await third.aclose()
            await clients[1].aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_generation_fencing(tmp_path) -> None:
    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: SimpleNamespace(),
            tool_caller=lambda *_args: [{"type": "text", "text": "ok"}],
        )
        await broker.start()
        try:
            client = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="one")
            await client.acquire()
            response = await client._request(
                "call",
                lease_id=client.lease_id,
                generation="wrong",
                tool="browser_navigate",
                arguments={},
            )
            assert response["ok"] is False
            assert "lease is not active" in response["error"]
            await client.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_bridge_keeps_the_host_discovery_path(tmp_path) -> None:
    captured: list[BrowserConfig] = []

    async def scenario() -> None:
        cfg = BrowserConfig(
            allowed_domains=("example.com",),
            state_dir=tmp_path,
            bridge=True,
        )
        broker = BrokerServer(
            cfg,
            session_factory=lambda session_cfg: captured.append(session_cfg) or SimpleNamespace(),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            first = BrokerClient(cfg, owner="owner-a", session_id="one")
            await first.acquire()
            assert captured[0].state_dir == tmp_path
            second = BrokerClient(cfg, owner="owner-a", session_id="two")
            try:
                await second.acquire()
            except Exception as exc:
                assert "one Broker session" in str(exc)
            else:
                raise AssertionError("bridge mode must reject a second session")
            await second.aclose()
            await first.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_session_facade_closes_the_broker_session(tmp_path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.closed = 0
            self.last_url = "https://example.com/"
            self.last_title = "Example"

        async def call(self, tool, arguments):
            return [{"type": "text", "text": f"{tool}:{arguments}"}]

        async def close_session(self):
            self.closed += 1

    session = BrokerSession(_cfg(tmp_path), owner="owner-a", session_id="one")
    fake = FakeClient()
    session.client = fake
    blocks = asyncio.run(session.call_tool("browser_navigate", {"url": "https://example.com"}))
    assert blocks[0]["text"] == "browser_navigate:{'url': 'https://example.com'}"
    asyncio.run(session.aclose())
    assert fake.closed == 1
    assert session.state == "closed"
