# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Chromium extension service-worker restart smoke test (manual).

Launches a disposable Chromium with the unpacked extension, waits for the bridge,
stops the extension service worker through the browser CDP endpoint, wakes the
extension by opening its onboarding page, then verifies the bridge reconnects and
can create a target again.

    uv run python scripts/browser_worker_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from icode_browser_use.bridge.install import extension_id_from_manifest
from icode_browser_use.bridge.protocol import PROTOCOL_VERSION
from icode_browser_use.bridge.server import BridgeServer
from icode_browser_use.cdp.client import CDPClient

from smoke_common import chrome_executable

CHROME = chrome_executable()
EXTENSION = Path(os.environ["LOCALAPPDATA"]) / "icode-browser-use" / "extension"


async def _browser_endpoint(profile: Path) -> str:
    path = profile / "DevToolsActivePort"
    for _ in range(100):
        if path.is_file():
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) >= 2:
                return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
        await asyncio.sleep(0.1)
    raise RuntimeError("DevToolsActivePort did not appear")


async def _run() -> None:
    work = Path(tempfile.mkdtemp(prefix="chrys-worker-smoke-"))
    profile = work / "profile"
    bridge_file = work / "bridge.json"
    env = {**os.environ, "ICODE_BROWSER_BRIDGE_FILE": str(bridge_file)}
    server = BridgeServer(bridge_file)
    chrome: subprocess.Popen | None = None
    browser: CDPClient | None = None
    try:
        await server.start()
        chrome = subprocess.Popen(
            [
                str(CHROME),
                "--headless=new",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                f"--load-extension={EXTENSION}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--no-sandbox",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = await server.wait_for_client(timeout=30)
        hello = await client.send("Bridge.hello", {"protocol": PROTOCOL_VERSION}, timeout=5)
        if hello.get("protocol") != PROTOCOL_VERSION:
            raise SystemExit(f"handshake mismatch: {hello}")
        await client.send("Bridge.configure", {"allowedDomains": ["example.com"]}, timeout=10)
        print("bridge: connected")

        browser = await CDPClient.connect(await _browser_endpoint(profile), open_timeout=10)
        listing = await browser.send("Target.getTargets", {}, timeout=10)
        workers = [
            target
            for target in listing.get("targetInfos", [])
            if target.get("type") == "service_worker" and str(target.get("url", "")).startswith("chrome-extension://")
        ]
        if not workers:
            raise RuntimeError("extension service worker target not found")
        worker_id = str(workers[0]["targetId"])
        await browser.send("Target.closeTarget", {"targetId": worker_id}, timeout=10)
        print("worker: stopped")
        await asyncio.sleep(1)

        extension_id = extension_id_from_manifest(EXTENSION)
        await browser.send(
            "Target.createTarget",
            {"url": f"chrome-extension://{extension_id}/onboarding.html"},
            timeout=10,
        )
        client = await server.wait_for_client(timeout=40)
        await client.send("Bridge.configure", {"allowedDomains": ["example.com"]}, timeout=10)
        target = await client.send("Target.createTarget", {"url": "about:blank"}, timeout=10)
        print(f"bridge: reconnected; target={target['targetId']}")
        await client.send("Target.closeTarget", {"targetId": str(target["targetId"])}, timeout=10)
        await client.aclose()
        print("worker smoke: ok")
    finally:
        if browser is not None:
            await browser.aclose()
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
