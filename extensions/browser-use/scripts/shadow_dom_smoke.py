# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser verification for Shadow DOM consistency (Item 1 / P0-1).

Verifies on ``/shadow-nested``:
1. Open Shadow DOM: extract reads greeting text; mouse click on internal button succeeds.
2. Nested closed Shadow DOM:
   - ``browser_fill`` focuses, types, and reads back inside ``contenteditable`` (zero UIA).
   - ``browser_click`` (default ``dispatch=mouse``) clicks the unoccluded post button once.
   - ``browser_extract`` on the closed shadow host falls back to snapshot subtree text.
3. True occlusion contrast:
   - Shadow-internal overlay covering a button MUST be refused (``status=blocked``).
   - Light-DOM full-page mask covering a shadow button MUST be refused (``status=blocked``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import icode_browser_use.extension as ext  # noqa: E402
from chrys.extensions import ToolContext  # noqa: E402
from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

REF = re.compile(r"@e(\d+)#(\d+)")


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="shadow-dom-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    with directory, contextlib.ExitStack() as stack:
        origin, _state = stack.enter_context(serve())
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
                "ICODE_BROWSER_HEADLESS": "0" if args.headed else "1",
            }
        )
        if args.executable:
            os.environ["ICODE_BROWSER_EXECUTABLE"] = str(args.executable)
        ctx = ToolContext.for_testing(
            root / "workspace",
            data_dir=root / "data",
            session_data_dir=root / "session",
            agent_name="shadow-smoke",
            session_id="shadow-smoke",
        )
        (root / "workspace").mkdir()
        browser_id = ""
        child = None
        try:
            opened = await ext.browser_open(ctx)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            await ext.browser_navigate(ctx, origin + "/shadow-nested", browser_id)
            child = ext._browsers[browser_id].session._child

            # 1. Open shadow extract + click
            page_text = await ext.browser_extract(ctx, browser_id)
            require("Open shadow greeting" in page_text, f"open shadow text missing from extract: {page_text!r}")
            report("check", name="open_shadow_extract", ok=True)

            open_find = await ext.browser_find(ctx, browser_id, "Open Shadow Action", role="button")
            open_ref = REF.search(open_find)
            require(open_ref is not None, f"open shadow button not found: {open_find!r}")
            open_click = await ext.browser_click(ctx, browser_id, open_ref.group(0))
            require("status=succeeded" in open_click, f"open shadow click failed: {open_click!r}")
            after_open = await ext.browser_extract(ctx, browser_id)
            require("open:1" in after_open, f"open shadow click did not increment counter: {after_open!r}")
            report("check", name="open_shadow_click", ok=True, detail=open_click.splitlines()[0])

            # 2. Closed nested shadow: fill contenteditable + click post button with dispatch=mouse
            ed_find = await ext.browser_find(ctx, browser_id, "Shadow Comment Box")
            ed_ref = REF.search(ed_find)
            require(ed_ref is not None, f"closed shadow editor not found: {ed_find!r}")
            filled = await ext.browser_fill(ctx, browser_id, ed_ref.group(0), "hello from icode")
            require("status=succeeded" in filled, f"closed shadow fill failed: {filled!r}")
            report("check", name="closed_shadow_fill", ok=True, detail=filled.splitlines()[0])

            post_find = await ext.browser_find(ctx, browser_id, "Post Shadow Comment", role="button")
            post_ref = REF.search(post_find)
            require(post_ref is not None, f"closed shadow post button not found: {post_find!r}")
            posted = await ext.browser_click(ctx, browser_id, post_ref.group(0))
            require("status=succeeded" in posted, f"closed shadow mouse click failed: {posted!r}")
            after_post = await ext.browser_extract(ctx, browser_id)
            require("posted:hello from icode" in after_post, f"shadow post did not apply: {after_post!r}")
            report("check", name="closed_shadow_click", ok=True, detail=posted.splitlines()[0])

            # 3. Closed shadow host extract fallback
            host_find = await ext.browser_find(ctx, browser_id, "x-closed-outer")
            host_ref = REF.search(host_find)
            require(host_ref is not None, f"closed shadow host not found: {host_find!r}")
            host_text = await ext.browser_extract(ctx, browser_id, query=host_ref.group(0))
            require(
                "[shadow DOM text from snapshot]" in host_text and "Shadow Comments" in host_text,
                f"closed shadow host extract fallback failed: {host_text!r}",
            )
            report("check", name="closed_shadow_extract_fallback", ok=True)

            # 4. Shadow-internal true occlusion MUST be blocked
            cov_find = await ext.browser_find(ctx, browser_id, "Covered Shadow Button", role="button")
            cov_ref = REF.search(cov_find)
            require(cov_ref is not None, f"covered shadow button not found: {cov_find!r}")
            cov_click = await ext.browser_click(ctx, browser_id, cov_ref.group(0))
            require("status=blocked" in cov_click, f"shadow-internal occlusion was not blocked: {cov_click!r}")
            report("check", name="shadow_internal_occlusion_blocked", ok=True)

            # 5. Light-DOM full-page mask over shadow button MUST be blocked
            mask_find = await ext.browser_find(ctx, browser_id, "Toggle Mask", role="button")
            mask_ref = REF.search(mask_find)
            require(mask_ref is not None, f"toggle mask button not found: {mask_find!r}")
            await ext.browser_click(ctx, browser_id, mask_ref.group(0))
            post_find2 = await ext.browser_find(ctx, browser_id, "Post Shadow Comment", role="button", fresh=True)
            post_ref2 = REF.search(post_find2)
            require(post_ref2 is not None, f"post button after mask not found: {post_find2!r}")
            masked_click = await ext.browser_click(ctx, browser_id, post_ref2.group(0))
            require("status=blocked" in masked_click, f"light-DOM mask occlusion was not blocked: {masked_click!r}")
            report("check", name="light_dom_mask_occlusion_blocked", ok=True)
        finally:
            if browser_id and browser_id in ext._browsers:
                with contextlib.suppress(Exception):
                    await ext.browser_close(ctx, browser_id)
        if child is not None:
            deadline = time.monotonic() + 5.0
            while child.alive and time.monotonic() < deadline:
                time.sleep(0.1)
            require(not child.alive, f"Chrome PID {child.pid} still alive after close")
        report("acceptance_passed", checks=6)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-browser", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--executable", default="")
    args = parser.parse_args()
    if not args.allow_browser:
        raise SystemExit("pass --allow-browser to launch Chrome")
    if not args.executable:
        args.executable = str(chrome_executable())
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
