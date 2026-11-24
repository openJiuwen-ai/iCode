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


def test_broker_fresh_open_closes_locally_offline_and_online_and_frees_slots(tmp_path, monkeypatch) -> None:
    import importlib

    from chrys.extensions import ToolContext

    import icode_browser_use.extension as ext
    from icode_browser_use import registry

    for key in tuple(os.environ):
        if key.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("ICODE_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ICODE_BROWSER_BROKER", "1")
    reloaded = importlib.reload(ext)
    reloaded._browsers.clear()
    context = ToolContext.for_testing(
        tmp_path,
        data_dir=tmp_path / "data",
        session_data_dir=tmp_path / "session",
        agent_name="A",
        session_id="s-fresh-broker",
    )

    async def scenario() -> None:
        # Offline broker: open -> close twice, then open a 3rd handle cleanly.
        for _ in range(3):
            opened = await reloaded.browser_open(context)
            browser_id = opened.removeprefix("browser_id: ").strip()
            closed = await reloaded.browser_close(context, browser_id)
            assert f"browser_id {browser_id} closed" in closed
            records, _ = registry.load(registry.manifest_path(context))
            assert records == []
        assert reloaded._browsers == {}

        # Online broker: open -> close without calling any page tool also works,
        # and direct close_session on a non-existent session is idempotent.
        broker = BrokerServer(
            _cfg(tmp_path / "state"),
            session_factory=lambda _cfg: SimpleNamespace(state="ready"),
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            for _ in range(3):
                opened = await reloaded.browser_open(context)
                browser_id = opened.removeprefix("browser_id: ").strip()
                await reloaded.browser_close(context, browser_id)
                records, _ = registry.load(registry.manifest_path(context))
                assert records == []

            direct = BrokerClient(_cfg(tmp_path / "state"), owner="owner-a", session_id="never-created")
            await direct.close_session()
            assert broker.sessions == {}

            # Cross-owner close of an existing session is still rejected.
            holder = BrokerClient(_cfg(tmp_path / "state"), owner="owner-a", session_id="live-1")
            await holder.acquire()
            intruder = BrokerClient(_cfg(tmp_path / "state"), owner="owner-b", session_id="live-1")
            with pytest.raises(Exception, match="lease is not active"):
                await intruder.close_session()
            await intruder.aclose()
            await holder.close_session()
            assert "live-1" not in broker.sessions
        finally:
            await broker.aclose()
            reloaded._browsers.clear()

    asyncio.run(scenario())


def test_broker_acquire_waits_for_in_flight_close_before_issuing_new_lease(tmp_path) -> None:
    created: list[object] = []
    closing_entered = asyncio.Event()
    allow_close_finish = asyncio.Event()

    class SlowCloseSession:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.state = "ready"
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1
            if self.idx == 0:
                closing_entered.set()
                await allow_close_finish.wait()
            self.state = "closed"

    async def tool_caller(session, _tool, _arguments):
        return [{"type": "text", "text": f"session-{session.idx}"}]

    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: created.append(SlowCloseSession(len(created))) or created[-1],
            tool_caller=tool_caller,
        )
        await broker.start()
        try:
            first = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="shared")
            await first.acquire()
            assert (await first.call("browser_snapshot", {}))[0]["text"] == "session-0"

            close_task = asyncio.create_task(first.close_session())
            await asyncio.wait_for(closing_entered.wait(), timeout=2.0)

            second = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="shared")
            acquire_task = asyncio.create_task(second.acquire())
            await asyncio.sleep(0.05)
            # While close is tearing down session-0, acquire must stay parked instead of leasing the dying session.
            assert not acquire_task.done()

            allow_close_finish.set()
            await asyncio.wait_for(close_task, timeout=2.0)
            await asyncio.wait_for(acquire_task, timeout=2.0)

            assert len(created) == 2
            assert created[0].closed == 1  # type: ignore[attr-defined]
            # First call on the new lease must succeed immediately (no "broker lease is not active").
            blocks = await second.call("browser_snapshot", {})
            assert blocks[0]["text"] == "session-1"
            await second.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_close_cleans_registry_even_when_aclose_raises(tmp_path) -> None:
    created: list[object] = []

    class FlakyCloseSession:
        def __init__(self, fail_close: bool) -> None:
            self.fail_close = fail_close
            self.state = "ready"

        async def aclose(self) -> None:
            self.state = "closed"
            if self.fail_close:
                raise RuntimeError("teardown exploded")

    async def scenario() -> None:
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: created.append(FlakyCloseSession(len(created) == 0)) or created[-1],
            tool_caller=lambda *_args: [{"type": "text", "text": "ok"}],
        )
        await broker.start()
        try:
            first = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="s1")
            await first.acquire()
            with pytest.raises(Exception, match="teardown exploded"):
                await first.close_session()
            await first.aclose()
            assert "s1" not in broker.sessions
            assert "s1" not in broker.owners
            assert "s1" not in broker._closing_tasks

            second = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="s1")
            await second.acquire()
            assert len(created) == 2
            await second.aclose()
        finally:
            await broker.aclose()

    asyncio.run(scenario())


def test_broker_all_eviction_paths_call_aclose(tmp_path) -> None:
    class TrackedSession:
        def __init__(self, initial_state: str = "ready") -> None:
            self.state = initial_state
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1
            self.state = "closed"

    async def scenario() -> None:
        sessions: list[TrackedSession] = []
        broker = BrokerServer(
            _cfg(tmp_path),
            session_factory=lambda _cfg: sessions.append(TrackedSession()) or sessions[-1],
            tool_caller=lambda *_args: [],
        )
        await broker.start()
        try:
            # 1) Eviction on explicit release
            c1 = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="evict-release")
            await c1.acquire()
            s1 = sessions[-1]
            s1.state = "idle"
            resp = await c1._request("release", lease_id=c1.lease_id, generation=c1.lease_generation)
            assert resp["ok"] is True
            assert "evict-release" not in broker.sessions
            assert s1.closed == 1
            c1.lease_id = ""
            await c1.aclose()

            # 2) Eviction on acquire of a new session
            c2 = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="evict-acquire")
            await c2.acquire()
            s2 = sessions[-1]
            await c2.aclose()  # released while state="ready", so still registered
            assert "evict-acquire" in broker.sessions
            s2.state = "failed"
            c3 = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="fresh-after-evict")
            await c3.acquire()
            assert "evict-acquire" not in broker.sessions
            assert s2.closed == 1
            await c3.aclose()

            # 3) Eviction on disconnect
            c4 = BrokerClient(_cfg(tmp_path), owner="owner-a", session_id="evict-disconnect")
            await c4.acquire()
            s4 = sessions[-1]
            s4.state = "idle"
            assert c4._writer is not None
            c4._writer.close()
            await c4._writer.wait_closed()
            c4._writer = None
            c4._reader = None
            for _ in range(50):
                if s4.closed == 1 and "evict-disconnect" not in broker.sessions:
                    break
                await asyncio.sleep(0.01)
            assert "evict-disconnect" not in broker.sessions
            assert s4.closed == 1

            # 4) Eviction on sweep
            s5 = TrackedSession(initial_state="idle")
            broker.sessions["evict-sweep"] = s5
            broker.owners["evict-sweep"] = "owner-a"
            await broker._evict_idle_sessions()
            assert "evict-sweep" not in broker.sessions
            assert s5.closed == 1
        finally:
            await broker.aclose()

    asyncio.run(scenario())
