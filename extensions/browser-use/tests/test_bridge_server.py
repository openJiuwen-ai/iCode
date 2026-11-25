# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from icode_browser_use.bridge import server as bridge_server
from icode_browser_use.bridge.protocol import PROTOCOL_VERSION, encode_message
from icode_browser_use.bridge.server import BridgeServer
from icode_browser_use.bridge.transport import BridgeTransport
from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import CDPTimeout, ConnectionClosed, Unavailable
from icode_browser_use.session import BrowserSession


async def _send(writer, payload: dict) -> None:
    writer.write(encode_message(payload))
    await writer.drain()


async def _recv(reader) -> dict:
    header = await reader.readexactly(4)
    length = int.from_bytes(header, "little")
    body = await reader.readexactly(length)
    return json.loads(body.decode("utf-8"))


async def _connect(server: BridgeServer):
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    await _send(writer, {"token": server.token})
    return reader, writer


def test_second_bridge_server_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge_file = tmp_path / "bridge.json"
        first = BridgeServer(bridge_file)
        await first.start()
        original = bridge_file.read_bytes()
        second = BridgeServer(bridge_file)
        with pytest.raises(Unavailable):
            await second.start()
        assert bridge_file.read_bytes() == original
        await first.aclose()

    asyncio.run(scenario())


def test_bridge_send_timeout_is_cdp_timeout(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter
        with pytest.raises(CDPTimeout):
            await client.send("Page.navigate", {"url": "https://example.com"}, timeout=0.05)
        assert client.closed is False
        with pytest.raises(CDPTimeout):
            await client.send("Page.navigate", {"url": "https://example.com"}, timeout=0.05)
        assert client.closed is True
        writer.close()
        await writer.wait_closed()
        await server.aclose()

    asyncio.run(scenario())


def test_requests_that_time_out_together_count_as_one_timeout(tmp_path: Path) -> None:
    """Concurrent reads of one hung page are one liveness failure, not two in a row."""

    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter
        results = await asyncio.gather(
            client.send("DOMSnapshot.captureSnapshot", {}, timeout=0.05),
            client.send("Accessibility.getFullAXTree", {}, timeout=0.05),
            client.send("Runtime.evaluate", {}, timeout=0.05),
            return_exceptions=True,
        )
        assert all(isinstance(result, CDPTimeout) for result in results)
        assert client.closed is False
        # A request sent after that timeout still counts: two consecutive failures close it.
        with pytest.raises(CDPTimeout):
            await client.send("Page.navigate", {"url": "https://example.com"}, timeout=0.05)
        assert client.closed is True
        writer.close()
        await writer.wait_closed()
        await server.aclose()

    asyncio.run(scenario())


def test_in_flight_request_fails_when_peer_disconnects(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter
        send = asyncio.create_task(client.send("Page.navigate", {"url": "https://example.com"}))
        request = await _recv(reader)
        assert request["method"] == "Page.navigate"
        writer.close()
        await writer.wait_closed()
        with pytest.raises(ConnectionClosed):
            await send
        await server.aclose()

    asyncio.run(scenario())


def test_heartbeat_closes_a_half_open_connection(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter
        client.start_heartbeat(0.01, timeout=0.02)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if client.closed:
                break
        assert client.closed is True
        writer.close()
        await writer.wait_closed()
        await server.aclose()

    asyncio.run(scenario())


def test_late_response_does_not_resolve_the_next_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter

        first = asyncio.create_task(client.send("Page.navigate", {"url": "https://example.com"}, timeout=0.1))
        request1 = await _recv(reader)
        with pytest.raises(CDPTimeout):
            await first

        second = asyncio.create_task(client.send("Page.getFrameTree", {}, timeout=1))
        request2 = await _recv(reader)
        assert request1["id"] != request2["id"]

        # A late response for the timed-out request must be ignored.
        await _send(writer, {"id": request1["id"], "result": {"late": True}})
        await asyncio.sleep(0.05)
        assert not second.done()

        await _send(writer, {"id": request2["id"], "result": {"frameTree": {"frame": {}}}})
        assert await second == {"frameTree": {"frame": {}}}
        await client.aclose()
        writer.close()
        await writer.wait_closed()
        await server.aclose()

    asyncio.run(scenario())


def test_bridge_accepts_reconnect_after_client_disconnect(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter1 = asyncio.create_task(server.wait_for_client(timeout=2))
        reader1, writer1 = await _connect(server)
        client1 = await waiter1

        await client1.aclose()
        writer1.close()
        await writer1.wait_closed()
        await asyncio.sleep(0)

        waiter2 = asyncio.create_task(server.wait_for_client(timeout=2))
        reader2, writer2 = await _connect(server)
        client2 = await waiter2
        assert client2 is not client1

        await client2.aclose()
        writer2.close()
        await writer2.wait_closed()
        await server.aclose()

    asyncio.run(scenario())


def test_bridge_close_does_not_delete_a_foreign_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge_file = tmp_path / "bridge.json"
        server = BridgeServer(bridge_file)
        await server.start()
        own = json.loads(bridge_file.read_text(encoding="utf-8"))
        assert own["generation"]
        foreign = {"port": 1, "token": "other", "pid": 1, "generation": "foreign"}
        bridge_file.write_text(json.dumps(foreign), encoding="utf-8")
        await server.aclose()
        assert json.loads(bridge_file.read_text(encoding="utf-8")) == foreign

    asyncio.run(scenario())


def test_bridge_client_closed_raises_connection_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter
        await client.aclose()
        writer.close()
        await writer.wait_closed()
        with pytest.raises(ConnectionClosed):
            await client.send("Page.navigate", {"url": "https://example.com"})
        await server.aclose()

    asyncio.run(scenario())


def test_aclose_releases_the_instance_lock_after_client_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge_file = tmp_path / "bridge.json"
        server = BridgeServer(bridge_file)
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter

        async def fail():
            client._writer.close()
            raise RuntimeError("client close failed")

        client.aclose = fail  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await server.aclose()
        writer.close()
        await writer.wait_closed()

        replacement = BridgeServer(bridge_file)
        await replacement.start()
        await replacement.aclose()

    asyncio.run(scenario())


def test_bridge_hardens_the_discovery_file(tmp_path: Path, monkeypatch) -> None:
    called: list[Path] = []

    async def scenario() -> None:
        monkeypatch.setattr("icode_browser_use._proc.harden_private", lambda p, **_kw: (called.append(p), True)[1])
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        await server.aclose()

    asyncio.run(scenario())
    assert len(called) == 1
    assert called[0].parent == tmp_path
    assert called[0].name.startswith(".bridge.json.")


def test_bridge_roundtrip_and_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        waiter = asyncio.create_task(server.wait_for_client(timeout=2))
        reader, writer = await _connect(server)
        client = await waiter

        send = asyncio.create_task(client.send("Page.navigate", {"url": "https://example.com"}))
        request = await _recv(reader)
        assert request["method"] == "Page.navigate"
        assert request["params"] == {"url": "https://example.com"}
        assert request["leaseId"] == server.generation
        await _send(writer, {"id": request["id"], "result": {"frameId": "f1"}})
        assert await send == {"frameId": "f1"}

        seen = asyncio.Event()

        def handler(_params: dict, _session_id: str | None) -> None:
            seen.set()

        client.on("Page.loadEventFired", handler)
        await _send(writer, {"method": "Page.loadEventFired", "params": {}})
        await asyncio.wait_for(seen.wait(), 1)

        await client.aclose()
        writer.close()
        await writer.wait_closed()
        await server.aclose()
        assert not (tmp_path / "bridge.json").exists()

    asyncio.run(scenario())


def test_bridge_rejects_a_wrong_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        await _send(writer, {"token": "wrong"})
        with pytest.raises(Unavailable):
            await server.wait_for_client(timeout=0.3)
        writer.close()
        await writer.wait_closed()
        await server.aclose()

    asyncio.run(scenario())


def test_bridge_timeout_is_a_typed_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        with pytest.raises(Unavailable):
            await server.wait_for_client(timeout=0.2)
        await server.aclose()

    asyncio.run(scenario())


def test_a_first_start_fails_fast_and_names_the_doctor(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bridge_server, "_unpacked_extension_installed", lambda: False)

    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        assert server.wait_timeout(35.0) == 5.0, "no handshake and no install: do not wait the alarm out"
        with pytest.raises(Unavailable) as exc_info:
            await server.wait_for_client(timeout=0.1)
        assert "doctor --bridge" in str(exc_info.value)
        assert "never handshaked" in str(exc_info.value)
        assert "ICODE_BROWSER_BRIDGE_WAIT" not in str(exc_info.value), "that variable cannot lift the 5 s cap here"
        await server.aclose()

    asyncio.run(scenario())


def test_a_visible_install_keeps_the_full_wait(tmp_path: Path, monkeypatch) -> None:
    """A fresh install has no handshake yet; it must get the alarm window, not 5 s."""
    monkeypatch.setattr(bridge_server, "_unpacked_extension_installed", lambda: True)

    async def scenario() -> None:
        server = BridgeServer(tmp_path / "bridge.json")
        await server.start()
        assert server.wait_timeout(35.0) == 35.0
        await server.aclose()

    asyncio.run(scenario())


def test_a_handshake_marks_the_state_folder_and_survives_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge_file = tmp_path / "bridge.json"
        first = BridgeServer(bridge_file)
        await first.start()
        waiter = asyncio.create_task(first.wait_for_client(timeout=2))
        reader, writer = await _connect(first)
        await waiter
        discovery = json.loads(bridge_file.read_text(encoding="utf-8"))
        assert discovery["handshake"] is True
        assert first.wait_timeout(35.0) == 35.0
        await first.aclose()
        writer.close()
        await writer.wait_closed()

        # The discovery file is gone (aclose unlinks its own generation) but the marker
        # stays: the next session may wait through the extension's alarm again.
        assert not bridge_file.exists()
        assert first.handshake_file.is_file()
        second = BridgeServer(bridge_file)
        await second.start()
        assert second.wait_timeout(35.0) == 35.0
        await second.aclose()

    asyncio.run(scenario())


def test_a_handshake_from_an_earlier_run_is_remembered(tmp_path: Path) -> None:
    bridge_file = tmp_path / "bridge.json"
    bridge_file.write_text(json.dumps({"port": 1, "token": "t", "pid": 1, "generation": "g", "handshake": True}))

    async def scenario() -> None:
        server = BridgeServer(bridge_file)
        await server.start()
        assert server.wait_timeout(35.0) == 35.0
        await server.aclose()

    asyncio.run(scenario())


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.closed = 0

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.sent.append((method, params))
        if method == "Bridge.hello":
            return {"protocol": PROTOCOL_VERSION, "capabilities": []}
        return {}

    async def aclose(self) -> None:
        self.closed += 1


class FakeServer:
    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.client = FakeClient()
        self.waited: list[float] = []

    async def start(self) -> None:
        self.started += 1

    def wait_timeout(self, configured: float) -> float:
        return configured / 2

    async def wait_for_client(self, timeout: float = 15.0) -> FakeClient:
        self.waited.append(timeout)
        return self.client

    async def aclose(self) -> None:
        self.closed += 1


def test_bridge_transport_never_closes_the_browser(tmp_path: Path) -> None:
    server = FakeServer()
    config = BrowserConfig(allowed_domains=("example.com",), bridge=True, state_dir=tmp_path)
    transport = BridgeTransport(config, server_factory=lambda _cfg: server)

    client, child = asyncio.run(transport.start())
    assert child is None
    assert transport.owns_browser is False
    assert server.started == 1
    assert server.waited == [config.bridge_wait / 2], "the transport must use the server's wait policy"

    asyncio.run(transport.stop(client, None, target_id="t1"))
    assert ("Target.closeTarget", {"targetId": "t1"}) in client.sent
    assert ("Browser.close", None) not in client.sent
    assert client.closed == 1
    assert server.closed == 1


class IncompatibleClient(FakeClient):
    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.sent.append((method, params))
        if method == "Bridge.hello":
            return {"protocol": 999, "capabilities": []}
        return {}


def test_bridge_transport_rejects_an_incompatible_protocol(tmp_path: Path) -> None:
    server = FakeServer()
    server.client = IncompatibleClient()
    config = BrowserConfig(allowed_domains=("example.com",), bridge=True, state_dir=tmp_path)
    transport = BridgeTransport(config, server_factory=lambda _cfg: server)
    with pytest.raises(Unavailable):
        asyncio.run(transport.start())


def test_bridge_transport_closes_a_stale_target_on_reconnect(tmp_path: Path) -> None:
    server = FakeServer()
    config = BrowserConfig(allowed_domains=("example.com",), bridge=True, state_dir=tmp_path)
    transport = BridgeTransport(config, server_factory=lambda _cfg: server)

    client, _ = asyncio.run(transport.start())
    asyncio.run(transport.stop(None, None, target_id="stale-tab"))
    client, _ = asyncio.run(transport.start())
    assert ("Target.closeTarget", {"targetId": "stale-tab"}) in client.sent
    asyncio.run(transport.stop(client, None, target_id=None))


def test_session_selects_the_bridge_transport(tmp_path: Path) -> None:
    config = BrowserConfig(allowed_domains=("example.com",), bridge=True, state_dir=tmp_path)
    session = BrowserSession(config)
    assert session._transport.owns_browser is False
    assert type(session._transport).__name__ == "BridgeTransport"


def test_config_reads_bridge_flag() -> None:
    config = BrowserConfig.from_env(
        {
            "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
            "ICODE_BROWSER_BRIDGE": "1",
            "ICODE_BROWSER_BRIDGE_FILE": r"C:\tmp\bridge.json",
        }
    )
    assert config.bridge is True
    assert config.bridge_file == r"C:\tmp\bridge.json"


class GatingClient(FakeClient):
    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.sent.append((method, params))
        if method == "Bridge.hello":
            return {"protocol": PROTOCOL_VERSION, "capabilities": ["ping"]}
        if method == "Bridge.configure":
            return {"capabilities": ["ping", "page_log_gated"]}
        return {}


def test_bridge_transport_hands_the_extension_capabilities_to_the_client(tmp_path: Path) -> None:
    config = BrowserConfig(allowed_domains=("example.com",), bridge=True, state_dir=tmp_path)
    old = FakeServer()  # hello and configure declare nothing: an extension that predates page-log gating
    client, _ = asyncio.run(BridgeTransport(config, server_factory=lambda _cfg: old).start())
    assert "page_log_gated" not in client.capabilities
    new = FakeServer()
    new.client = GatingClient()
    client, _ = asyncio.run(BridgeTransport(config, server_factory=lambda _cfg: new).start())
    assert "page_log_gated" in client.capabilities
