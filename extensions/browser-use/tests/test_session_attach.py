# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path

from browser_use.bridge import install
from browser_use.config import BrowserConfig
from browser_use.session import BrowserSession


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []
        self.closed = 0
        self.handlers: list[str] = []

    def on(self, method: str, handler) -> None:
        self.handlers.append(method)

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.calls.append((method, params, session_id))
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
        self.closed += 1


def make_session(
    tmp_path: Path, *, attached: bool, chrome_devtools: bool = False
) -> tuple[BrowserSession, list[FakeClient], list[tuple[str, object]]]:
    clients: list[FakeClient] = []
    launches: list[tuple[str, object]] = []

    async def client_factory(ws: str) -> FakeClient:
        client = FakeClient()
        clients.append(client)
        launches.append(("client", ws))
        return client

    async def launcher_factory(_cfg, _state_dir):
        launches.append(("launcher", None))
        raise AssertionError("attached mode must not launch a browser")

    config = BrowserConfig(
        allowed_domains=("example.com",),
        state_dir=tmp_path / "state",
        cdp_url="ws://existing" if attached else None,
        chrome_devtools=chrome_devtools,
    )
    return BrowserSession(config, client_factory=client_factory, launcher_factory=launcher_factory), clients, launches


def methods(client: FakeClient) -> list[str]:
    return [method for method, _params, _session in client.calls]


def test_attached_session_starts_without_launching(tmp_path: Path) -> None:
    session, clients, launches = make_session(tmp_path, attached=True)
    asyncio.run(session.ensure_started())
    assert session.state == "ready"
    assert len(clients) == 1
    assert launches == [("client", "ws://existing")]
    asyncio.run(session.aclose())


def test_chrome_connection_waits_for_cdp_before_creating_a_tab(tmp_path: Path, monkeypatch) -> None:
    endpoint = "ws://127.0.0.1:12345/devtools/browser/chrome"
    monkeypatch.setattr(install, "chrome_devtools_ws", lambda: endpoint)
    session, clients, launches = make_session(tmp_path, attached=False, chrome_devtools=True)

    asyncio.run(session.ensure_started())

    assert launches == [("client", endpoint)]
    assert methods(clients[0])[:2] == ["Browser.getVersion", "Target.createTarget"]
    asyncio.run(session.aclose())


def test_attached_aclose_closes_only_our_tab_and_disconnects(tmp_path: Path) -> None:
    session, clients, _ = make_session(tmp_path, attached=True)
    asyncio.run(session.ensure_started())
    client = clients[0]

    asyncio.run(session.aclose())

    assert "Browser.close" not in methods(client)
    assert ("Target.closeTarget", {"targetId": "target-1"}, None) in client.calls
    assert client.closed == 1
    assert session.state == "closed"


def test_attached_does_not_close_foreign_tabs(tmp_path: Path) -> None:
    session, clients, _ = make_session(tmp_path, attached=True)
    asyncio.run(session.ensure_started())
    client = clients[0]
    before = list(client.calls)

    asyncio.run(session._on_attached_to_target({"targetInfo": {"type": "page", "targetId": "foreign"}}, None))
    assert client.calls == before
    asyncio.run(session.aclose())


def test_attached_policy_is_scoped_to_our_page(tmp_path: Path) -> None:
    session, clients, _ = make_session(tmp_path, attached=True)
    asyncio.run(session.ensure_started())
    client = clients[0]

    fetch = next(call for call in client.calls if call[0] == "Fetch.enable")
    assert fetch[2] == "session-1"
    auto_attach = next(call for call in client.calls if call[0] == "Target.setAutoAttach")
    assert auto_attach[2] == "session-1"
    assert not any(method == "Browser.setDownloadBehavior" for method in methods(client))
    asyncio.run(session.aclose())


def test_attached_idle_shutdown_keeps_the_browser_and_closes_our_tab(tmp_path: Path) -> None:
    session, clients, _ = make_session(tmp_path, attached=True)
    asyncio.run(session.ensure_started())
    client = clients[0]

    asyncio.run(session._shutdown_idle())

    assert session.state == "idle"
    assert "Browser.close" not in methods(client)
    assert ("Target.closeTarget", {"targetId": "target-1"}, None) in client.calls
    assert client.closed == 1
