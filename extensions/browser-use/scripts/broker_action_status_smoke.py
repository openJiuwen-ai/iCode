# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Broker action-status smoke: click in Broker, query from same client."""

from __future__ import annotations

import asyncio
import re
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
    work = Path(tempfile.mkdtemp(prefix="chrys-broker-action-"))
    cfg = BrowserConfig(
        allowed_domains=("example.com",),
        state_dir=work,
        executable=str(CHROME),
        headless=True,
        idle_close_seconds=0,
    )
    broker = BrokerServer(cfg)
    try:
        await broker.start()
        client = BrokerClient(cfg, owner="smoke", session_id="action")
        await client.call("browser_navigate", {"url": "https://example.com/"})
        found = _text(await client.call("browser_find", {"query": "learn"}))
        match = re.search(r"@e\d+#\d+", found)
        if match is None:
            raise RuntimeError("find returned no ref")
        clicked = _text(await client.call("browser_click", {"target": match.group(0)}))
        action = re.search(r"action_id=([0-9a-f]+)", clicked)
        if action is None:
            raise RuntimeError("click returned no action_id")
        status = _text(await client.call("browser_action_status", {"action_id": action.group(1)}))
        if action.group(1) not in status:
            raise RuntimeError(f"Broker action_status missed the action: {status}")
        print("broker action_status:", action.group(1))
        await client.aclose()
        print("broker action_status smoke: ok")
    finally:
        await broker.aclose()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chrome not found: {CHROME}")
    asyncio.run(_run())
