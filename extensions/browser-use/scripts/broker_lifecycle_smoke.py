# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Broker lifecycle smoke: lease TTL, reacquire and idle exit."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from icode_browser_use.broker import BrokerServer
from icode_browser_use.broker_client import BrokerClient
from icode_browser_use.config import BrowserConfig

from smoke_common import chrome_executable

CHROME = chrome_executable()


def _text(blocks) -> str:
    return "\n".join(str(block.get("text", "")) for block in blocks)


async def _run() -> None:
    work = Path(tempfile.mkdtemp(prefix="chrys-broker-lifecycle-"))
    cfg = BrowserConfig(
        allowed_domains=("example.com",),
        state_dir=work,
        executable=str(CHROME),
        headless=True,
        idle_close_seconds=0,
        broker_lease_ttl=0.2,
        broker_idle_exit=0.5,
    )
    broker = BrokerServer(cfg)
    try:
        await broker.start()
        client = BrokerClient(cfg, owner="smoke", session_id="lifecycle")
        nav = await client.call("browser_navigate", {"url": "https://example.com/"})
        print("navigate:", _text(nav).splitlines()[0])
        await asyncio.sleep(1.2)
        snap = await client.call("browser_snapshot", {})
        if "Example Domain" not in _text(snap):
            raise RuntimeError("expired lease did not reacquire the page")
        print("lease: expired and reacquired")
        await client.close_session()
        await asyncio.wait_for(broker.stop_event.wait(), timeout=5)
        print("broker idle exit: ok")
    finally:
        await broker.aclose()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chrome not found: {CHROME}")
    asyncio.run(_run())
