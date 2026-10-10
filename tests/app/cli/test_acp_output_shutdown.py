# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real pipe backpressure and failed writes must not prevent ACP request cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress

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

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Uses POSIX asyncio pipe transports")


@pytest.mark.parametrize("output", ["blocked", "broken"])
@pytest.mark.parametrize("stop", ["eof", "cancel"])
async def test_shutdown_releases_approval_without_readable_output(monkeypatch, output: str, stop: str) -> None:
    closed = asyncio.Event()

    class Session(_FakeSession):
        async def shutdown(self) -> None:
            closed.set()

    class Server(ChrysAcpServer):
        async def ext_method(self, method, params):
            # Larger than both the kernel pipe and asyncio's read buffer.
            return {"padding": "x" * (2 * 1024 * 1024)}

    host = _FakeHost(
        event_bus=EventBus(),
        events=[ApprovalRequest(request_id="pending", tool_name="bash", args={}, session_id="s1")],
        outcome=EndTurn(),
    )
    manager = _FakeManager(host)
    session = Session(host=host)
    manager._session = session
    manager._sessions = {"s1": session}
    server = Server(manager, initial_vision=False)
    loop = asyncio.get_running_loop()
    read_fd, write_fd = os.pipe()
    peer_reader = asyncio.StreamReader(limit=4096)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(peer_reader), os.fdopen(read_fd, "rb", buffering=0)
    )
    write_transport, write_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(write_fd, "wb", buffering=0)
    )
    writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)
    reader = asyncio.StreamReader()

    async def streams(*, limit):
        return reader, writer

    monkeypatch.setattr(acp_cli, "stdio_streams", streams)

    async def run() -> None:
        try:
            await acp_cli._serve_agent(server)
        finally:
            await AcpSessionManager.shutdown(manager)

    task = asyncio.create_task(run())
    connection = None
    try:
        reader.feed_data(
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

        async def requested() -> None:
            while True:
                line = await peer_reader.readline()
                if not line:
                    raise AssertionError("stdout closed before requesting approval")
                if json.loads(line).get("method") == "session/request_permission":
                    return

        await asyncio.wait_for(requested(), timeout=3)
        connection = server._client
        assert isinstance(connection, AgentSideConnection)
        assert session.prompt_lock.locked()
        assert server._pending_permission_tasks
        if output == "broken":
            read_transport.close()
        reader.feed_data(b'{"jsonrpc":"2.0","id":2,"method":"_flood","params":{}}\n')
        sender = connection._conn._sender
        if output == "blocked":
            await wait_for(
                lambda: writer.transport.get_write_buffer_size() > 65536, description="stdout pipe is backpressured"
            )
            assert not sender._task.done()
        else:
            await wait_for(sender._task.done, description="sender failed on a broken pipe")
            assert isinstance(sender._task.exception(), (BrokenPipeError, ConnectionResetError))
        owned_tasks = list(connection._conn._tasks._tasks)
        if stop == "eof":
            reader.feed_eof()
        else:
            task.cancel()
        # No output reads or transport aborts until shutdown has completed.
        await wait_for(closed.is_set, description="session shut down after the transport closed")
        result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=3)
        assert result == [None]
        assert not session.prompt_lock.locked()
        assert not server._pending_permission_tasks
        assert not server._pending_permission_cancels
        assert all(owned.done() for owned in owned_tasks)
        assert not connection._conn._tasks._tasks
    finally:
        # Release the pipe only after the assertions, including on a regression.
        if not write_transport.is_closing():
            write_transport.abort()
        read_transport.close()
        if isinstance(connection, AgentSideConnection):
            owned_tasks = list(connection._conn._tasks._tasks)
            for owned in owned_tasks:
                owned.cancel()
            await asyncio.gather(*owned_tasks, return_exceptions=True)
            with suppress(Exception):
                await connection.close()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
