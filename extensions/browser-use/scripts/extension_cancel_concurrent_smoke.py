# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Chrome: same-owner concurrency and close cancellation/recovery."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path

from chrys.extensions import ToolContext

from icode_browser_use import extension as ext

from smoke_common import chrome_executable

CHROME = chrome_executable()


def _ctx(root: Path) -> ToolContext:
    return ToolContext.for_testing(
        root,
        data_dir=root / "data",
        session_data_dir=root / "session",
        agent_name="A",
        session_id="s1",
    )


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chrys-cancel-concurrent-") as temp:
        root = Path(temp)
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_EXECUTABLE": str(CHROME),
                "ICODE_BROWSER_HEADLESS": "1",
                "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
            }
        )
        ctx = _ctx(root)
        try:
            first, second = await asyncio.gather(
                ext.browser_open(ctx),
                ext.browser_open(ctx),
            )
            first_id = str(first).removeprefix("browser_id: ").strip()
            second_id = str(second).removeprefix("browser_id: ").strip()
            assert first_id != second_id
            await ext.browser_navigate(ctx, "https://example.com/?branch=a", first_id)
            await ext.browser_navigate(ctx, "https://example.com/?branch=b", second_id)
            assert "branch=a" in ext._browsers[first_id].url
            assert "branch=b" in ext._browsers[second_id].url
            print("concurrent: two pages isolated")
            await ext.browser_close(ctx, second_id)

            third = str(await ext.browser_open(ctx)).removeprefix("browser_id: ").strip()
            await ext.browser_navigate(ctx, "https://example.com/?cancel=1", third)
            close_task = asyncio.create_task(ext.browser_close(ctx, third))
            await asyncio.sleep(0)
            close_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await close_task
            if third in ext._browsers:
                await ext.browser_close(ctx, third)
            ext._prune_closed()
            assert third not in ext._browsers
            print("cancel: close recovered")

            await ext.browser_close(ctx, first_id)
            print("concurrent smoke: ok")
        finally:
            for browser in list(ext._browsers.values()):
                with contextlib.suppress(Exception):
                    await browser.session.aclose()
            ext._browsers.clear()


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chrome not found: {CHROME}")
    asyncio.run(_run())
