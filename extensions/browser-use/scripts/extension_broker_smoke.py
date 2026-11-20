# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real extension-adapter + Broker smoke (manual)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from chrys.extensions import ToolContext

from browser_use import extension
from browser_use.broker_client import BrokerSession

from smoke_common import chrome_executable, clean_environment

CHROME = chrome_executable()
SRC = Path(__file__).resolve().parents[1] / "src"


def _env(state: Path) -> dict[str, str]:
    return {
        **clean_environment(),
        "PYTHONPATH": str(SRC),
        "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
        "ICODE_BROWSER_BROKER": "1",
        "ICODE_BROWSER_BROKER_AUTOSTART": "0",
        "ICODE_BROWSER_STATE_DIR": str(state),
        "ICODE_BROWSER_EXECUTABLE": str(CHROME),
        "ICODE_BROWSER_HEADLESS": "1",
        "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
    }


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chrys-extension-broker-", ignore_cleanup_errors=True) as temp:
        work = Path(temp)
        with patch.dict(os.environ, _env(work), clear=True), (work / "broker.log").open("ab") as log:
            broker = subprocess.Popen(
                [sys.executable, "-m", "browser_use.broker"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                await _browse(work, broker)
            finally:
                broker.terminate()
                try:
                    broker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    broker.kill()
                    broker.wait()


async def _browse(work: Path, broker: subprocess.Popen) -> None:
    discovery = work / "broker" / "broker.json"
    for _ in range(100):
        if discovery.is_file():
            break
        if broker.poll() is not None:
            raise RuntimeError(f"Broker exited before becoming ready (exit {broker.returncode})")
        await asyncio.sleep(0.1)
    else:
        raise RuntimeError("Broker discovery file did not appear within 10 seconds")

    context = ToolContext.for_testing(work, data_dir=work / "data", agent_name="smoke", session_id="smoke")
    browser = extension._open(context)
    try:
        if not isinstance(browser.session, BrokerSession):
            raise RuntimeError("extension did not select the Broker backend")
        nav = await extension._run(browser, "browser_navigate", {"url": "https://example.com/"})
        print("extension navigate:", nav.splitlines()[0])
        snap = await extension._run(browser, "browser_snapshot", {})
        if "Example Domain" not in snap:
            raise RuntimeError("extension BrokerSession did not see the page")
        print("extension snapshot: Example Domain")
        found = await extension._run(browser, "browser_find", {"query": "learn"})
        if "- @e" not in found:
            raise RuntimeError("browser_find returned no concrete match")
        print("extension find: learn")
    finally:
        await extension.browser_close(context, browser.id)
    print("extension broker smoke: ok")


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chrome not found: {CHROME}")
    asyncio.run(_run())
