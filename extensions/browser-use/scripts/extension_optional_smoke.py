# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real opt-in evaluate/save_state smoke (default hidden, env enabled, approval)."""

from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
from pathlib import Path

from chrys.extensions import ToolContext

from smoke_common import chrome_executable

CHROME = chrome_executable()


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chrys-optional-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        os.environ.pop("ICODE_BROWSER_EVALUATE", None)
        os.environ.pop("ICODE_BROWSER_SAVE_STATE", None)
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_EXECUTABLE": str(CHROME),
                "ICODE_BROWSER_HEADLESS": "1",
                "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
            }
        )
        import icode_browser_use.extension as ext

        ext = importlib.reload(ext)
        names = {tool.name for tool in ext.extension.tools}
        if {"browser_evaluate", "browser_save_state"} & names:
            raise RuntimeError("optional tools must be hidden by default")
        print("default: optional tools hidden")

        os.environ["ICODE_BROWSER_EVALUATE"] = "allow"
        os.environ["ICODE_BROWSER_SAVE_STATE"] = "allow"
        ext = importlib.reload(ext)
        tools = {tool.name: tool for tool in ext.extension.tools}
        for name in ("browser_evaluate", "browser_save_state"):
            if name not in tools:
                raise RuntimeError(f"{name} was not registered")
            if not getattr(tools[name], "requires_approval", False):
                raise RuntimeError(f"{name} must require approval")
        print("enabled: optional tools registered with approval")

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
            value = await ext.browser_evaluate(ctx, browser_id, "document.title")
            if "Example Domain" not in str(value):
                raise RuntimeError(f"evaluate returned {value!r}")
            print("evaluate: Example Domain")
            saved = await ext.browser_save_state(ctx, browser_id, "smoke")
            path = Path(str(saved).splitlines()[0].split("：", 1)[1])
            if not path.is_file():
                raise RuntimeError(f"save_state file missing: {path}")
            if path.stat().st_size == 0:
                raise RuntimeError("save_state file is empty")
            print("save_state:", path.name)
        finally:
            await ext.browser_close(ctx, browser_id)
            ext._browsers.clear()
        print("optional smoke: ok")


if __name__ == "__main__":
    if not CHROME.is_file():
        raise SystemExit(f"Chrome not found: {CHROME}")
    asyncio.run(_run())
