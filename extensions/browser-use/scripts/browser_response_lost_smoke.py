# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Chromium response-loss smoke test (manual).

A frame-aware proxy sits between the native host and the bridge. It drops one
response from the plugin side and verifies the next request still works.

    uv run python scripts/browser_response_lost_smoke.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from icode_browser_use.bridge.install import default_extension_install_dir
from icode_browser_use.bridge.protocol import PROTOCOL_VERSION
from icode_browser_use.bridge.server import BridgeServer
from icode_browser_use.errors import CDPTimeout

from smoke_common import chrome_executable

CHROME = chrome_executable()
EXTENSION = default_extension_install_dir()


class FrameProxy:
    def __init__(self) -> None:
        self.port = 0
        self.drop_next = False
        self.hold_next = False
        self.hold_seconds = 0.8
        self.dropped_target: str | None = None
        self.held_target: str | None = None
        self.dropped_id: int | None = None
        self.held_id: int | None = None
        self.late_delivered = asyncio.Event()
        self._server: asyncio.AbstractServer | None = None
        self._target_port = 0
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, target_port: int) -> None:
        self._target_port = target_port
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        sockets = self._server.sockets or ()
        self.port = int(sockets[0].getsockname()[1])

    async def _accept(self, host_reader, host_writer) -> None:
        server_reader, server_writer = await asyncio.open_connection("127.0.0.1", self._target_port)
        self._writers.update({host_writer, server_writer})
        tasks = [
            asyncio.create_task(self._pump(host_reader, server_writer, from_plugin=False)),
            asyncio.create_task(self._pump(server_reader, host_writer, from_plugin=True)),
        ]
        try:
            _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._writers.discard(host_writer)
            self._writers.discard(server_writer)
            for writer in (server_writer, host_writer):
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def _pump(self, reader, writer, *, from_plugin: bool) -> None:
        try:
            while True:
                header = await reader.readexactly(4)
                length = int.from_bytes(header, "little")
                body = await reader.readexactly(length)
                if not from_plugin and (self.drop_next or self.hold_next):
                    try:
                        response = json.loads(body)
                    except ValueError:
                        response = {}
                    result = response.get("result") if isinstance(response, dict) else {}
                    target = result.get("targetId") if isinstance(result, dict) else None
                    request_id = response.get("id") if isinstance(response, dict) else None
                    if self.drop_next:
                        self.drop_next = False
                        self.dropped_target = str(target) if target else None
                        self.dropped_id = request_id if isinstance(request_id, int) else None
                        continue
                    self.hold_next = False
                    self.held_target = str(target) if target else None
                    self.held_id = request_id if isinstance(request_id, int) else None
                    await asyncio.sleep(self.hold_seconds)
                writer.write(header + body)
                await writer.drain()
                if not from_plugin and self.held_id is not None:
                    self.late_delivered.set()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        for writer in list(self._writers):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _run() -> None:
    work = Path(tempfile.mkdtemp(prefix="chrys-response-smoke-"))
    profile = work / "profile"
    bridge_file = work / "bridge.json"
    proxy_file = work / "proxy.json"
    env = {**os.environ, "ICODE_BROWSER_BRIDGE_FILE": str(proxy_file)}
    server = BridgeServer(bridge_file)
    proxy = FrameProxy()
    chrome: subprocess.Popen | None = None
    try:
        await server.start()
        await proxy.start(server.port)
        proxy_file.write_text(
            json.dumps({"port": proxy.port, "token": server.token, "generation": server.generation}),
            encoding="utf-8",
        )
        chrome = subprocess.Popen(
            [
                str(CHROME),
                "--headless=new",
                f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--no-sandbox",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = await server.wait_for_client(timeout=30)
        hello = await client.send("Bridge.hello", {"protocol": PROTOCOL_VERSION}, timeout=5)
        if hello.get("protocol") != PROTOCOL_VERSION:
            raise SystemExit(f"handshake mismatch: {hello}")
        await client.send("Bridge.configure", {"allowedDomains": ["example.com"]}, timeout=10)
        print("bridge: connected", flush=True)
        proxy.drop_next = True
        try:
            await client.send("Target.createTarget", {"url": "about:blank"}, timeout=0.5)
        except CDPTimeout:
            print("response: dropped -> CDPTimeout", flush=True)
        else:
            raise RuntimeError("expected a response timeout")
        if not proxy.dropped_target:
            raise RuntimeError("dropped response did not contain a target id")
        attached = await client.send("Target.attachToTarget", {"targetId": proxy.dropped_target}, timeout=10)
        if not attached.get("sessionId"):
            raise RuntimeError("dropped target could not be attached")
        await client.send("Target.closeTarget", {"targetId": proxy.dropped_target}, timeout=10)
        print(f"dropped target existed: {proxy.dropped_target}", flush=True)
        target = await client.send("Target.createTarget", {"url": "about:blank"}, timeout=10)
        print(f"next target: {target['targetId']}", flush=True)
        await client.send("Target.closeTarget", {"targetId": str(target["targetId"])}, timeout=10)

        proxy.hold_next = True
        proxy.late_delivered.clear()
        try:
            await client.send("Target.createTarget", {"url": "about:blank"}, timeout=0.5)
        except CDPTimeout:
            print("late response: timeout", flush=True)
        else:
            raise RuntimeError("expected a late-response timeout")
        if not proxy.held_target:
            raise RuntimeError("held response did not contain a target id")
        # Send the next request immediately; the held response must arrive while
        # this request is pending, and its stale id must not resolve it.
        late_task = asyncio.create_task(client.send("Target.createTarget", {"url": "about:blank"}, timeout=10))
        await asyncio.wait_for(proxy.late_delivered.wait(), 5)
        if late_task.done():
            raise RuntimeError("late response resolved the next request early")
        if proxy.held_id in client._pending:
            raise RuntimeError("stale request id is still pending")
        late_target = await late_task
        if late_target["targetId"] == proxy.held_target:
            raise RuntimeError("late response polluted the next request")
        attached = await client.send("Target.attachToTarget", {"targetId": proxy.held_target}, timeout=10)
        if not attached.get("sessionId"):
            raise RuntimeError("held target could not be attached")
        await client.send("Target.closeTarget", {"targetId": proxy.held_target}, timeout=10)
        print(f"held target existed: {proxy.held_target}", flush=True)
        print(f"after late response: {late_target['targetId']}", flush=True)
        await client.send("Target.closeTarget", {"targetId": str(late_target["targetId"])}, timeout=10)
        await client.aclose()
        print("response smoke: ok", flush=True)
    finally:
        await proxy.aclose()
        await server.aclose()
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chromium not found: {CHROME}")
    asyncio.run(_run())
