# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Broker smoke: one BrowserSession survives a client rebuild (manual)."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from browser_use.broker import BrokerServer
from browser_use.broker_client import BrokerClient
from browser_use.config import BrowserConfig

from smoke_common import chrome_executable

CHROME = chrome_executable()


def _text(blocks) -> str:
    return "\n".join(str(block.get("text", "")) for block in blocks)


async def _run() -> None:
    work = Path(tempfile.mkdtemp(prefix="chrys-broker-smoke-"))
    cfg = BrowserConfig(
        allowed_domains=("example.com",),
        state_dir=work,
        executable=str(CHROME),
        headless=True,
        idle_close_seconds=0,
    )
    broker = BrokerServer(cfg)
    try:
        print("broker: starting", flush=True)
        await broker.start()
        print("broker: started", flush=True)
        first = BrokerClient(cfg, owner="smoke", session_id="default")
        print("client: connecting", flush=True)
        await first.acquire()
        print("client: acquired", flush=True)
        nav = await first.call("browser_navigate", {"url": "https://example.com/"})
        print("navigate:", _text(nav).splitlines()[0])

        other = BrokerClient(cfg, owner="smoke", session_id="second")
        nav_other = await other.call("browser_navigate", {"url": "https://example.com/"})
        print("second session navigate:", _text(nav_other).splitlines()[0])
        snap_other = await other.call("browser_snapshot", {})
        if "Example Domain" not in _text(snap_other):
            raise RuntimeError("second session did not get its own browser")
        print("second session: Example Domain")
        await first.aclose()
        print("client: closed", flush=True)

        second = BrokerClient(cfg, owner="smoke", session_id="default")
        snap = await second.call("browser_snapshot", {})
        text = _text(snap)
        if "Example Domain" not in text:
            raise RuntimeError("rebuilt client did not reuse the BrowserSession")
        print("reused snapshot: Example Domain")
        await second.aclose()
        await other.aclose()
        print("broker smoke: ok")
    finally:
        await broker.aclose()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chromium not found: {CHROME}")
    asyncio.run(_run())
