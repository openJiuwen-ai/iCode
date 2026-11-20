# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from browser_use.bridge.host import bridge_file
from browser_use.bridge.protocol import encode_message, read_async_frame
from browser_use.bridge.server import BridgeServer

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture(autouse=True)
def isolate_browser_env(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("override", ["chosen.json", ""])
def test_host_resolves_bridge_file(tmp_path, monkeypatch, override):
    monkeypatch.setenv("ICODE_BROWSER_BRIDGE_FILE", override)
    monkeypatch.setenv("ICODE_BROWSER_STATE_DIR", str(tmp_path))
    assert bridge_file() == (Path(override) if override else tmp_path / "bridge.json")


def test_native_host_pumps_frames_both_ways(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge_file = tmp_path / "bridge.json"
        server = BridgeServer(bridge_file)
        await server.start()
        env = {
            **os.environ,
            "PYTHONPATH": str(SRC),
            "ICODE_BROWSER_BRIDGE_FILE": str(bridge_file),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "browser_use.bridge.host",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            client = await server.wait_for_client(timeout=10)
            ready = await asyncio.wait_for(read_async_frame(process.stdout), 5)
            assert ready == {"method": "Bridge.ready", "params": {}}

            event_seen = asyncio.Event()

            def on_event(_params, _session_id) -> None:
                event_seen.set()

            client.on("Page.loadEventFired", on_event)
            process.stdin.write(encode_message({"method": "Page.loadEventFired", "params": {}}))
            await process.stdin.drain()
            await asyncio.wait_for(event_seen.wait(), 5)

            send = asyncio.create_task(client.send("Page.getFrameTree", {}))
            request = await asyncio.wait_for(read_async_frame(process.stdout), 5)
            assert request is not None
            assert request["method"] == "Page.getFrameTree"
            process.stdin.write(encode_message({"id": request["id"], "result": {"frameTree": {"frame": {}}}}))
            await process.stdin.drain()
            assert await send == {"frameTree": {"frame": {}}}

            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 5) == 0
            await client.aclose()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await server.aclose()

    asyncio.run(scenario())


def test_host_reconnects_to_a_new_bridge_generation(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge_file = tmp_path / "bridge.json"
        env = {
            **os.environ,
            "PYTHONPATH": str(SRC),
            "ICODE_BROWSER_BRIDGE_FILE": str(bridge_file),
        }
        first = BridgeServer(bridge_file)
        await first.start()
        process1 = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "browser_use.bridge.host",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            client1 = await first.wait_for_client(timeout=10)
            assert await asyncio.wait_for(read_async_frame(process1.stdout), 5) == {
                "method": "Bridge.ready",
                "params": {},
            }
            await first.aclose()
            assert await asyncio.wait_for(process1.wait(), 5) == 0
            assert client1.closed is True

            second = BridgeServer(bridge_file)
            await second.start()
            process2 = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "browser_use.bridge.host",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                client2 = await second.wait_for_client(timeout=10)
                assert await asyncio.wait_for(read_async_frame(process2.stdout), 5) == {
                    "method": "Bridge.ready",
                    "params": {},
                }
                assert client2 is not client1
            finally:
                if process2.returncode is None:
                    process2.kill()
                    await process2.wait()
                await second.aclose()
        finally:
            if process1.returncode is None:
                process1.kill()
                await process1.wait()
            await first.aclose()

    asyncio.run(scenario())


def test_host_exits_when_the_plugin_disconnects(tmp_path: Path) -> None:
    async def scenario() -> None:
        bridge_file = tmp_path / "bridge.json"
        server = BridgeServer(bridge_file)
        await server.start()
        env = {
            **os.environ,
            "PYTHONPATH": str(SRC),
            "ICODE_BROWSER_BRIDGE_FILE": str(bridge_file),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "browser_use.bridge.host",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            client = await server.wait_for_client(timeout=10)
            await client.aclose()
            # stdin stays open: the host must still exit on the TCP close.
            assert await asyncio.wait_for(process.wait(), 5) == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await server.aclose()

    asyncio.run(scenario())
