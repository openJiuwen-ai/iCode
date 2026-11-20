# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Chrome: an old ref after navigation must fail closed."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from pathlib import Path

from chrys.extensions import ToolContext, ToolError

from browser_use import extension as ext

from smoke_common import chrome_executable

CHROME = chrome_executable()


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chrys-stale-ref-") as temp:
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
        ctx = ToolContext.for_testing(
            root,
            data_dir=root / "data",
            session_data_dir=root / "session",
            agent_name="A",
            session_id="s1",
        )
        browser_id = str(await ext.browser_open(ctx)).removeprefix("browser_id: ").strip()
        try:
            await ext.browser_navigate(ctx, "https://example.com/", browser_id)
            found = str(await ext.browser_find(ctx, browser_id, "learn"))
            match = re.search(r"@e\d+#\d+", found)
            if match is None:
                raise RuntimeError("find returned no ref")
            old_ref = match.group(0)
            await ext.browser_navigate(ctx, "https://example.com/?next=1", browser_id)
            try:
                await ext.browser_click(ctx, browser_id, old_ref)
            except ToolError as exc:
                if exc.code != "browser_stale_ref":
                    raise RuntimeError(f"unexpected code: {exc.code}") from exc
                print("stale ref:", exc.code)
            else:
                raise RuntimeError("old ref was accepted after navigation")
        finally:
            with contextlib.suppress(Exception):
                await ext.browser_close(ctx, browser_id)
            ext._browsers.clear()
        print("stale ref smoke: ok")


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chrome not found: {CHROME}")
    asyncio.run(_run())
