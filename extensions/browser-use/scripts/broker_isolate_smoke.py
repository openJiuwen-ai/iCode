# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Broker: multi-lease refusal, rebuild reuse and per-session isolation."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from browser_use.broker import BrokerServer
from browser_use.broker_client import BrokerClient
from browser_use.config import BrowserConfig
from browser_use.errors import BrowserError

from smoke_common import chrome_executable

CHROME = chrome_executable()


def _text(blocks) -> str:
    return "\n".join(str(block.get("text", "")) for block in blocks)


async def _run() -> None:
    work = Path(tempfile.mkdtemp(prefix="chrys-broker-isolate-"))
    cfg = BrowserConfig(
        allowed_domains=("example.com",),
        state_dir=work,
        executable=str(CHROME),
        headless=True,
        idle_close_seconds=0,
    )
    broker = BrokerServer(cfg)
    first: BrokerClient | None = None
    second: BrokerClient | None = None
    try:
        await broker.start()
        first = BrokerClient(cfg, owner="smoke", session_id="alpha")
        second = BrokerClient(cfg, owner="smoke", session_id="beta")
        await first.call("browser_navigate", {"url": "https://example.com/?a=1"})
        await second.call("browser_navigate", {"url": "https://example.com/?b=1"})

        contender = BrokerClient(cfg, owner="smoke", session_id="alpha")
        try:
            await contender.acquire()
        except BrowserError:
            print("multi-lease: refused")
        else:
            raise RuntimeError("second live lease was accepted")
        finally:
            await contender.aclose()

        await second.close_session()
        second = None
        assert "Example Domain" in _text(await first.call("browser_snapshot", {}))
        print("isolation: beta closed, alpha alive")

        await first.aclose()
        first = None
        rebuilt = BrokerClient(cfg, owner="smoke", session_id="alpha")
        assert "Example Domain" in _text(await rebuilt.call("browser_snapshot", {}))
        print("rebuild: alpha reused")
        await rebuilt.close_session()
        assert not broker.sessions
        print("broker isolate smoke: ok")
    finally:
        for client in (first, second):
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
        await broker.aclose()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chrome not found: {CHROME}")
    asyncio.run(_run())
