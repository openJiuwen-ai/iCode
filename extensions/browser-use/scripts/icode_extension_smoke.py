# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure-extension smoke on an unmodified iCode checkout."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from smoke_common import chrome_executable, icode_checkout

ICODE = icode_checkout()
sys.path.insert(0, str(ICODE))
sys.path.insert(0, str(ICODE / "src"))

from chrys.service.extensions import bind_extension, load_extensions  # noqa: E402


async def _call(tools, name: str, **arguments):
    return await tools[name].invoke(arguments=arguments, skip_parsing=True)


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chrys-extension-smoke-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_EXECUTABLE": str(chrome_executable()),
                "ICODE_BROWSER_HEADLESS": "1",
                "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
                "ICODE_BROWSER_BROKER": "0",
                "ICODE_BROWSER_BRIDGE": "0",
            }
        )
        loaded = await load_extensions(["browser"])
        assert loaded[0].extension is not None, loaded[0].problem
        build = SimpleNamespace(
            runtime=SimpleNamespace(cwd=root, working_dirs=()),
            agent_name="smoke",
            caller_name=None,
            session_id="smoke",
            session_dir=root / "session",
        )
        bound = bind_extension("browser", loaded[0].extension, build)
        tools = {tool.name: tool for tool in bound.tools}
        required = {
            "browser_open",
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_screenshot",
            "browser_close",
        }
        missing = required - set(tools)
        if missing:
            raise RuntimeError(f"installed browser plugin is stale; missing {sorted(missing)}")

        browser_id = ""
        try:
            opened = await _call(tools, "browser_open")
            browser_id = str(opened).removeprefix("browser_id: ").strip()
            if not browser_id:
                raise RuntimeError("browser_open returned no browser_id")
            print("open:", browser_id)
            nav = await _call(tools, "browser_navigate", browser_id=browser_id, url="https://example.com/")
            print("navigate:", str(nav).splitlines()[1])
            snapshot = await _call(tools, "browser_snapshot", browser_id=browser_id)
            if "Example Domain" not in str(snapshot):
                raise RuntimeError("snapshot did not contain Example Domain")
            print("snapshot: Example Domain")
            shot = str(await _call(tools, "browser_screenshot", browser_id=browser_id))
            if "Screenshot saved to " not in shot:
                raise RuntimeError(f"screenshot failed: {shot}")
            path = shot.split("Screenshot saved to ", 1)[1].splitlines()[0].split(" (", 1)[0]
            if not Path(path).is_file():
                raise RuntimeError(f"screenshot file missing: {path}")
            if os.environ.get("CHRYS_SMOKE_KEEP"):
                import shutil

                shutil.copyfile(path, Path.cwd() / "screenshot-smoke.png")
            from chrys.service.tools.builtins.filesystem import _view_image_impl

            viewed = _view_image_impl(str(Path(path).resolve()))
            if not any(
                getattr(content, "type", "") == "data" and str(getattr(content, "media_type", "")).startswith("image/")
                for content in viewed
            ):
                raise RuntimeError("view_image did not return image data")
            print("view_image: image content")
            print("screenshot:", Path(path).name)
            found = await _call(tools, "browser_find", browser_id=browser_id, query="learn")
            match = re.search(r"@e\d+#\d+", str(found))
            if match is None:
                raise RuntimeError("find returned no ref")
            clicked = await _call(
                tools,
                "browser_click",
                browser_id=browser_id,
                target=match.group(0),
            )
            if "status=succeeded" not in str(clicked):
                raise RuntimeError(f"click did not succeed: {clicked}")
            print("click: status=succeeded")
        finally:
            if browser_id:
                closed = await _call(tools, "browser_close", browser_id=browser_id)
                if "closed" not in str(closed):
                    raise RuntimeError(f"close failed: {closed}")
                print("close: closed")
        print("extension smoke: ok")


if __name__ == "__main__":
    asyncio.run(_run())
