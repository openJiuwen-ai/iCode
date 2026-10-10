# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The real SDK transport drains prompts before session shutdown takes their locks."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from acp.agent.connection import AgentSideConnection

from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionManager
from chrys.app.cli import acp as acp_cli
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalRequest
from chrys.orchestration.session_host import EndTurn
from tests.app.acp._server_fakes import _FakeHost, _FakeManager, _FakeSession
from tests.support.waiting import wait_for


@pytest.mark.parametrize("stop", ["cancel", "eof"])
async def test_sdk_shutdown_drains_unlimited_approval_before_manager_shutdown(monkeypatch, stop: str) -> None:
    closed = asyncio.Event()

    class Session(_FakeSession):
        async def shutdown(self) -> None:
            closed.set()

    host = _FakeHost(
        event_bus=EventBus(),
        events=[ApprovalRequest(request_id="pending", tool_name="bash", args={}, session_id="s1")],
        outcome=EndTurn(),
    )
    manager = _FakeManager(host)
    session = Session(host=host)
    manager._session = session
    manager._sessions = {"s1": session}
    server = ChrysAcpServer(manager, initial_vision=False)
    agent_socket, client_socket = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=agent_socket)
    peer_reader, peer_writer = await asyncio.open_connection(sock=client_socket)

    async def streams(*, limit):
        return reader, writer

    monkeypatch.setattr(acp_cli, "stdio_streams", streams)

    async def run() -> None:
        try:
            await acp_cli._serve_agent(server)
        finally:
            await AcpSessionManager.shutdown(manager)

    task = asyncio.create_task(run())
    try:
        peer_writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session/prompt",
                    "params": {"sessionId": "s1", "prompt": [{"type": "text", "text": "run"}]},
                }
            ).encode()
            + b"\n"
        )
        await peer_writer.drain()

        async def requested() -> None:
            while True:
                line = await peer_reader.readline()
                if not line:
                    raise AssertionError("transport closed before requesting approval")
                if json.loads(line).get("method") == "session/request_permission":
                    return

        await asyncio.wait_for(requested(), timeout=3)
        assert session.prompt_lock.locked()
        assert server._pending_permission_tasks
        if stop == "cancel":
            task.cancel()
        else:
            peer_writer.write_eof()
        await wait_for(closed.is_set, description="session shut down after the transport closed")
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
        # The pinned SDK receive loop consumes its cancellation.
        assert result == [None]
        assert not session.prompt_lock.locked()
        assert not server._pending_permission_tasks
        assert not server._pending_permission_cancels
    finally:
        # A failing regression must still release the SDK handlers holding locks.
        if isinstance(server._client, AgentSideConnection):
            await server._client.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        peer_writer.close()
        writer.close()
        await asyncio.gather(peer_writer.wait_closed(), writer.wait_closed(), return_exceptions=True)


@pytest.mark.parametrize("stop", ["cancel", "eof"])
async def test_shutdown_drops_a_response_sent_after_close(monkeypatch, stop: str) -> None:
    entered = asyncio.Event()

    class Server(ChrysAcpServer):
        async def ext_method(self, method, params):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Like turn cleanup that consumes the shutdown cancellation:
                # the handler returns and the SDK sends its response.
                return {"late": True}
            return {}

    server = Server(_FakeManager(_FakeHost(event_bus=EventBus(), outcome=EndTurn())), initial_vision=False)
    agent_socket, client_socket = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=agent_socket)
    _peer_reader, peer_writer = await asyncio.open_connection(sock=client_socket)

    async def streams(*, limit):
        return reader, writer

    monkeypatch.setattr(acp_cli, "stdio_streams", streams)
    task = asyncio.create_task(acp_cli._serve_agent(server))
    try:
        peer_writer.write(b'{"jsonrpc":"2.0","id":1,"method":"_late","params":{}}\n')
        await peer_writer.drain()
        await wait_for(entered.is_set, description="request handler started")
        if stop == "cancel":
            task.cancel()
        else:
            peer_writer.write_eof()
        await wait_for(task.done, description="ACP server stopped after a late response")
        assert task.result() is None
    finally:
        # On a regression the late send never resolves: cancel the SDK
        # handlers so the connection close, and this test, can finish.
        if isinstance(server._client, AgentSideConnection):
            owned_tasks = list(server._client._conn._tasks._tasks)
            for owned in owned_tasks:
                owned.cancel()
            await asyncio.gather(*owned_tasks, return_exceptions=True)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        peer_writer.close()
        writer.close()
        await asyncio.gather(peer_writer.wait_closed(), writer.wait_closed(), return_exceptions=True)
