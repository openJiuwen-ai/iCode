# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from browser_use.cdp.launcher import LaunchResult
from browser_use.config import BrowserConfig
from browser_use.errors import Unavailable
from browser_use.session import BrowserSession

HANDSHAKE = {
    "Page.enable",
    "Page.setLifecycleEventsEnabled",
    "Runtime.enable",
    "DOM.enable",
    "Network.enable",
    "Accessibility.enable",
    "Page.getFrameTree",
    "Page.addScriptToEvaluateOnNewDocument",
    "Fetch.enable",
    "Network.setBlockedURLs",
    "Browser.setDownloadBehavior",
    "Target.setAutoAttach",
}


class FakeChild:
    def __init__(self) -> None:
        self.terminated = 0

    async def terminate_tree(self) -> None:
        self.terminated += 1


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.handlers: list[str] = []
        self.aclose_count = 0
        self.sent: list[tuple[str, object]] = []
        self.closed = False

    def on(self, method: str, handler) -> None:
        self.handlers.append(method)

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.calls.append(method)
        self.sent.append((method, params))
        if method == "Target.createTarget":
            return {"targetId": "target-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1", "url": "https://example.com"}}}
        if method == "Page.addScriptToEvaluateOnNewDocument":
            return {"identifier": "probe-1"}
        return {}

    async def aclose(self) -> None:
        self.aclose_count += 1
        self.closed = True


def config(tmp_path: Path) -> BrowserConfig:
    return BrowserConfig(allowed_domains=("example.com",), state_dir=tmp_path / "state")


def make_session(tmp_path: Path, clients: list[FakeClient], launches: list[FakeChild]):
    async def client_factory(_ws: str) -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    async def launcher_factory(_cfg: BrowserConfig, _state_dir: Path) -> LaunchResult:
        child = FakeChild()
        launches.append(child)
        return LaunchResult(endpoint="http://127.0.0.1:1", browser_ws="ws://fake", child=child)

    return BrowserSession(config(tmp_path), client_factory=client_factory, launcher_factory=launcher_factory)


def test_ensure_started_is_single_flight_and_installs_handshake(tmp_path: Path) -> None:
    clients: list[FakeClient] = []
    launches: list[FakeChild] = []
    session = make_session(tmp_path, clients, launches)

    async def run() -> None:
        await asyncio.gather(session.ensure_started(), session.ensure_started())

    asyncio.run(run())
    assert len(launches) == 1
    assert len(clients) == 1
    assert session.state == "ready"
    assert HANDSHAKE.issubset(set(clients[0].calls))
    attaches = [params for method, params in clients[0].sent if method == "Target.setAutoAttach"]
    assert len(attaches) == 2, "Owned must arm the root and discover pages"
    assert {entry["type"] for entry in attaches[0]["filter"]} == {"page", "iframe"}
    assert {entry["type"] for entry in attaches[1]["filter"]} == {"page"}
    assert {"Fetch.requestPaused", "Target.attachedToTarget", "Target.detachedFromTarget"}.issubset(
        set(clients[0].handlers)
    )
    asyncio.run(session.aclose())


def test_launcher_failure_sets_failed_and_releases_the_lock(tmp_path: Path) -> None:
    async def client_factory(_ws: str):
        return FakeClient()

    async def launcher_factory(_cfg, _state_dir):
        raise Unavailable("no chrome")

    session = BrowserSession(config(tmp_path), client_factory=client_factory, launcher_factory=launcher_factory)
    with pytest.raises(Unavailable):
        asyncio.run(session.ensure_started())
    assert session.state == "failed"
    assert session._instance_lock is None


def test_ensure_started_rebuilds_a_closed_client(tmp_path: Path) -> None:
    clients: list[FakeClient] = []
    launches: list[FakeChild] = []
    session = make_session(tmp_path, clients, launches)
    asyncio.run(session.ensure_started())
    first = session._client
    first.closed = True
    asyncio.run(session.ensure_started())
    assert session._client is not first
    assert len(clients) == 2


def test_disconnect_restarts_and_resets_budgets(tmp_path: Path) -> None:
    clients: list[FakeClient] = []
    launches: list[FakeChild] = []
    session = make_session(tmp_path, clients, launches)
    asyncio.run(session.ensure_started())
    session._lifetime_actions = 5
    session._actions.append(1.0)

    session.mark_disconnected()
    asyncio.run(session.ensure_started())

    assert len(launches) == 2
    assert session.state == "ready"
    assert session._lifetime_actions == 0
    assert not session._actions
    asyncio.run(session.aclose())


def test_aclose_is_idempotent_and_terminates_once(tmp_path: Path) -> None:
    clients: list[FakeClient] = []
    launches: list[FakeChild] = []
    session = make_session(tmp_path, clients, launches)
    asyncio.run(session.ensure_started())

    asyncio.run(session.aclose())
    asyncio.run(session.aclose())

    assert session.state == "closed"
    assert sum(client.aclose_count for client in clients) == 1
    assert sum(child.terminated for child in launches) == 1
