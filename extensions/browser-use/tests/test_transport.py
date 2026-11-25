# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from icode_browser_use.cdp.launcher import LaunchResult
from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import Unavailable
from icode_browser_use.transport import AttachedTransport, OwnedTransport


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.closed = 0

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.sent.append((method, params))
        return {}

    async def aclose(self) -> None:
        self.closed += 1


class FakeChild:
    def __init__(self) -> None:
        self.terminated = 0

    async def terminate_tree(self) -> None:
        self.terminated += 1


def config(**overrides) -> BrowserConfig:
    return BrowserConfig(allowed_domains=("example.com",), state_dir=Path("."), **overrides)


def test_owned_transport_starts_and_stops_the_browser() -> None:
    client = FakeClient()
    child = FakeChild()
    launches: list[str] = []

    async def client_factory(ws: str) -> FakeClient:
        launches.append(ws)
        return client

    async def launcher_factory(_cfg, _state_dir):
        return LaunchResult(endpoint="http://127.0.0.1:1", browser_ws="ws://owned", child=child)

    transport = OwnedTransport(config(), client_factory, launcher_factory)
    started, started_child = asyncio.run(transport.start())
    assert started is client
    assert started_child is child
    assert launches == ["ws://owned"]
    assert transport.owns_browser is True

    asyncio.run(transport.stop(client, child, target_id="t1"))
    assert ("Browser.close", None) in client.sent
    assert client.closed == 1
    assert child.terminated == 1


def test_attached_transport_uses_the_existing_endpoint_and_never_closes_the_browser() -> None:
    client = FakeClient()
    seen: list[str] = []

    async def client_factory(ws: str) -> FakeClient:
        seen.append(ws)
        return client

    transport = AttachedTransport(config(cdp_url="ws://existing/devtools/browser/x"), client_factory)
    started, started_child = asyncio.run(transport.start())
    assert started is client
    assert started_child is None
    assert seen == ["ws://existing/devtools/browser/x"]
    assert transport.owns_browser is False

    asyncio.run(transport.stop(client, None, target_id="t1"))
    assert ("Browser.close", None) not in client.sent
    assert ("Target.closeTarget", {"targetId": "t1"}) in client.sent
    assert client.closed == 1


def test_attached_transport_resolves_an_http_endpoint() -> None:
    client = FakeClient()
    seen: list[str] = []

    async def client_factory(ws: str) -> FakeClient:
        seen.append(ws)
        return client

    async def resolver(url: str) -> str:
        assert url == "http://127.0.0.1:9222"
        return "ws://127.0.0.1:9222/devtools/browser/resolved"

    transport = AttachedTransport(config(cdp_url="http://127.0.0.1:9222"), client_factory, resolver)
    asyncio.run(transport.start())
    assert seen == ["ws://127.0.0.1:9222/devtools/browser/resolved"]


def test_attached_transport_without_a_target_only_disconnects() -> None:
    client = FakeClient()

    async def client_factory(_ws: str) -> FakeClient:
        return client

    transport = AttachedTransport(config(cdp_url="ws://existing"), client_factory)
    asyncio.run(transport.start())
    asyncio.run(transport.stop(client, None, target_id=None))
    assert not any(method == "Target.closeTarget" for method, _ in client.sent)
    assert client.closed == 1


def test_attached_transport_requires_a_cdp_url() -> None:
    async def client_factory(_ws: str) -> FakeClient:
        raise AssertionError("must not connect")

    transport = AttachedTransport(config(), client_factory)
    with pytest.raises(Unavailable):
        asyncio.run(transport.start())


def test_owned_transport_terminates_child_when_handshake_is_cancelled() -> None:
    child = FakeChild()

    async def client_factory(_ws: str) -> FakeClient:
        raise asyncio.CancelledError

    async def launcher_factory(_cfg, _state_dir):
        return LaunchResult(endpoint="http://127.0.0.1:1", browser_ws="ws://owned", child=child)

    transport = OwnedTransport(config(), client_factory, launcher_factory)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(transport.start())
    assert child.terminated == 1


def test_owned_transport_terminates_child_when_handshake_fails() -> None:
    child = FakeChild()

    async def client_factory(_ws: str) -> FakeClient:
        raise RuntimeError("handshake failed")

    async def launcher_factory(_cfg, _state_dir):
        return LaunchResult(endpoint="http://127.0.0.1:1", browser_ws="ws://owned", child=child)

    transport = OwnedTransport(config(), client_factory, launcher_factory)
    with pytest.raises(RuntimeError, match="handshake failed"):
        asyncio.run(transport.start())
    assert child.terminated == 1


def test_bridge_transport_closes_client_and_server_on_handshake_failure() -> None:
    from icode_browser_use.bridge.transport import BridgeTransport

    class FailingHelloClient(FakeClient):
        async def send(self, method: str, params=None, session_id=None, timeout=None):
            await super().send(method, params, session_id=session_id, timeout=timeout)
            raise asyncio.CancelledError

    client = FailingHelloClient()

    class FakeServer:
        def __init__(self) -> None:
            self.started = 0
            self.closed = 0

        async def start(self) -> None:
            self.started += 1

        def wait_timeout(self, value: float) -> float:
            return value

        async def wait_for_client(self, *, timeout: float) -> FailingHelloClient:
            return client

        async def aclose(self) -> None:
            self.closed += 1

    server = FakeServer()
    transport = BridgeTransport(config(), server_factory=lambda _cfg: server)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(transport.start())
    assert client.closed == 1
    assert server.closed == 1


def test_owned_transport_stop_waits_for_graceful_exit_before_force_kill() -> None:
    """P1 #2 (follow-up): OwnedTransport.stop must give Browser.close a flush window before terminate_tree."""
    events: list[str] = []

    class OrderClient(FakeClient):
        async def send(self, method: str, params=None, session_id=None, timeout=None):
            events.append(f"send:{method}")
            return await super().send(method, params, session_id=session_id, timeout=timeout)

        async def aclose(self) -> None:
            events.append("client:aclose")
            await super().aclose()

    class GracefulChild(FakeChild):
        async def wait_for_exit(self, *, timeout: float = 3.0) -> bool:
            events.append("child:wait_for_exit")
            return True

        async def terminate_tree(self) -> None:
            events.append("child:terminate_tree")
            await super().terminate_tree()

    client = OrderClient()
    child = GracefulChild()
    transport = OwnedTransport(config(), lambda _ws: client, lambda _cfg, _dir: None)  # type: ignore[arg-type]

    asyncio.run(transport.stop(client, child, target_id="t1"))
    assert events == ["send:Browser.close", "client:aclose", "child:wait_for_exit", "child:terminate_tree"]
