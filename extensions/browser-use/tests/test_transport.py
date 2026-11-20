# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from browser_use.cdp.launcher import LaunchResult
from browser_use.config import BrowserConfig
from browser_use.errors import Unavailable
from browser_use.transport import AttachedTransport, OwnedTransport


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
