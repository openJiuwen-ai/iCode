# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unified W13 real-browser reliability acceptance gate.

Drives real Chrome through ``browser_use.extension`` against the loopback
fixture site to verify all W13 improvements in one repeatable run:

1. ``/shadow-nested`` (P0-1 & P1-1):
   - Open Shadow DOM extract & mouse click (including same-length text mutation).
   - Nested closed Shadow DOM ``browser_find`` metadata (``[editable ...]``),
     ``browser_fill``, ``browser_click`` (default ``dispatch=mouse``), and
     snapshot-backed ``browser_extract`` fallback.
   - True occlusion contrast: both shadow-internal overlay and light-DOM mask
     MUST return ``status=blocked``.
2. ``/async-toggle`` (P0-2):
   - Delayed ``<input type="submit">`` toggle confirms via ``verified=control_state``
     with ``dispatch=sent`` and ``status=succeeded``.
   - Self-detaching confirm button returns ``effect=target_detached`` with
     ``status=succeeded`` (never false probe failure).
3. ``/dynamic-comments`` (P1-3):
   - Under an 80 ms ad ticker, ``browser_wait_for(condition="editable")`` waits
     cleanly for a delayed closed-Shadow-DOM composer without premature ambiguity.
   - ``browser_wait_for(condition="text")`` matches closed-Shadow-DOM comment
     text via ``basis=shadow_text_match``.
4. ``/tall`` (P2-1):
   - After scrolling down, ``browser_screenshot(target="Bottom Action")``
     captures a non-blank padded PNG of the scrolled element.
5. Tab visibility & lifecycle (P1-2):
   - ``browser_tabs`` reports foreground/background state, ``browser_status``
     reports ``foreground=``, and ``browser_close`` terminates the Owned Chrome.

Run from ``extensions/browser-use`` or repo root::

    python scripts/w13_reliability_smoke.py --allow-browser
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

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import browser_use.extension as ext  # noqa: E402
from chrys.extensions import ToolContext  # noqa: E402
from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

REF = re.compile(r"@e(\d+)#(\d+)")
SCREENSHOT_RE = re.compile(r"Screenshot saved to (.+?) \(([^,]+), (\d+)x(\d+), (\d+) bytes\)")


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise RuntimeError(msg)


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def _unique_rgb_colors(image: Image.Image) -> int:
    rgb = image.convert("RGB")
    if hasattr(rgb, "get_flattened_data"):
        pixels = rgb.get_flattened_data()
    else:
        pixels = rgb.getdata()
    return len(set(pixels))


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="w13-reliability-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    checks = 0
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
            agent_name="w13-smoke",
            session_id="w13-smoke",
        )
        (root / "workspace").mkdir()
        browser_id = ""
        child = None
        try:
            opened = await ext.browser_open(ctx)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()

            # --- 1. /shadow-nested (P0-1 & P1-1) ---
            await ext.browser_navigate(ctx, origin + "/shadow-nested", browser_id)
            child = ext._browsers[browser_id].session._child

            page_text = await ext.browser_extract(ctx, browser_id)
            require("Open shadow greeting" in page_text, f"open shadow text missing: {page_text!r}")
            open_find = await ext.browser_find(ctx, browser_id, "Open Shadow Action", role="button")
            open_ref = REF.search(open_find)
            require(open_ref is not None, f"open shadow button not found: {open_find!r}")
            open_click = await ext.browser_click(ctx, browser_id, open_ref.group(0))
            require("status=succeeded" in open_click, f"open shadow click failed: {open_click!r}")
            require("open:1" in await ext.browser_extract(ctx, browser_id), "open shadow counter did not increment")
            report("check", name="shadow_open_extract_and_click", ok=True)
            checks += 1

            ed_find = await ext.browser_find(ctx, browser_id, "Shadow Comment Box")
            require("[editable" in ed_find, f"editable metadata missing from find: {ed_find!r}")
            ed_ref = REF.search(ed_find)
            require(ed_ref is not None, f"closed shadow editor not found: {ed_find!r}")
            filled = await ext.browser_fill(ctx, browser_id, ed_ref.group(0), "hello from w13")
            require("status=succeeded" in filled, f"closed shadow fill failed: {filled!r}")

            post_find = await ext.browser_find(ctx, browser_id, "Post Shadow Comment", role="button")
            post_ref = REF.search(post_find)
            require(post_ref is not None, f"closed shadow post button not found: {post_find!r}")
            posted = await ext.browser_click(ctx, browser_id, post_ref.group(0))
            require("status=succeeded" in posted, f"closed shadow click failed: {posted!r}")
            require("posted:hello from w13" in await ext.browser_extract(ctx, browser_id), "shadow post did not land")
            report("check", name="shadow_closed_find_fill_click", ok=True)
            checks += 1

            host_find = await ext.browser_find(ctx, browser_id, "x-closed-outer")
            host_ref = REF.search(host_find)
            require(host_ref is not None, f"closed shadow host not found: {host_find!r}")
            host_text = await ext.browser_extract(ctx, browser_id, query=host_ref.group(0))
            require(
                "[shadow DOM text from snapshot]" in host_text and "Shadow Comments" in host_text,
                f"closed shadow host extract fallback failed: {host_text!r}",
            )
            report("check", name="shadow_closed_extract_fallback", ok=True)
            checks += 1

            cov_find = await ext.browser_find(ctx, browser_id, "Covered Shadow Button", role="button")
            cov_ref = REF.search(cov_find)
            require(cov_ref is not None, f"covered shadow button not found: {cov_find!r}")
            cov_click = await ext.browser_click(ctx, browser_id, cov_ref.group(0))
            require("status=blocked" in cov_click, f"shadow-internal occlusion not blocked: {cov_click!r}")

            mask_find = await ext.browser_find(ctx, browser_id, "Toggle Mask", role="button")
            mask_ref = REF.search(mask_find)
            require(mask_ref is not None, f"toggle mask button not found: {mask_find!r}")
            await ext.browser_click(ctx, browser_id, mask_ref.group(0))
            post_find2 = await ext.browser_find(ctx, browser_id, "Post Shadow Comment", role="button", fresh=True)
            post_ref2 = REF.search(post_find2)
            require(post_ref2 is not None, f"post button after mask not found: {post_find2!r}")
            masked_click = await ext.browser_click(ctx, browser_id, post_ref2.group(0))
            require("status=blocked" in masked_click, f"light-DOM mask occlusion not blocked: {masked_click!r}")
            report("check", name="shadow_occlusion_contrast", ok=True)
            checks += 1

            # --- 2. /async-toggle (P0-2) ---
            await ext.browser_navigate(ctx, origin + "/async-toggle", browser_id)
            follow_find = await ext.browser_find(ctx, browser_id, "Follow zhuyuloveai", role="button")
            follow_ref = REF.search(follow_find)
            require(follow_ref is not None, f"follow submit input not found: {follow_find!r}")
            follow_res = await ext.browser_click(ctx, browser_id, follow_ref.group(0))
            require(
                "dispatch=sent" in follow_res
                and "verified=control_state" in follow_res
                and "status=succeeded" in follow_res,
                f"delayed input[type=submit] toggle did not verify cleanly: {follow_res!r}",
            )
            report("check", name="async_toggle_control_state", ok=True, detail=follow_res.splitlines()[0])
            checks += 1

            confirm_find = await ext.browser_find(ctx, browser_id, "确定", role="button")
            confirm_ref = REF.search(confirm_find)
            require(confirm_ref is not None, f"confirm button not found: {confirm_find!r}")
            confirm_res = await ext.browser_click(ctx, browser_id, confirm_ref.group(0))
            require(
                "dispatch=sent" in confirm_res
                and "effect=target_detached" in confirm_res
                and "status=succeeded" in confirm_res
                and "探针失败" not in confirm_res,
                f"self-detaching confirm click misclassified: {confirm_res!r}",
            )
            report("check", name="async_toggle_target_detached", ok=True, detail=confirm_res.splitlines()[0])
            checks += 1

            # --- 3. /dynamic-comments (P1-3) ---
            await ext.browser_navigate(ctx, origin + "/dynamic-comments", browser_id)
            w_ed = await ext.browser_wait_for(
                ctx,
                browser_id,
                condition="editable",
                target="Dynamic Comment Input",
                timeout_ms=5000,
            )
            require("basis=editable" in w_ed, f"wait_for(editable) failed under ad ticker: {w_ed!r}")
            w_txt = await ext.browser_wait_for(
                ctx,
                browser_id,
                condition="text",
                value="没有更多评论",
                timeout_ms=5000,
            )
            require("basis=shadow_text_match" in w_txt, f"closed-shadow text wait failed: {w_txt!r}")
            report("check", name="dynamic_comments_wait_editable_and_shadow_text", ok=True)
            checks += 1

            # --- 4. /tall scrolled target screenshot (P2-1) ---
            await ext.browser_navigate(ctx, origin + "/tall", browser_id)
            await ext.browser_scroll(ctx, browser_id, direction="down", amount=5)
            shot = await ext.browser_screenshot(ctx, browser_id, target="Bottom Action")
            match = SCREENSHOT_RE.search(shot)
            require(match is not None, f"scrolled screenshot missing metadata: {shot!r}")
            img_path = Path(match.group(1))
            with Image.open(img_path) as image:
                width, height = image.size
                unique_colors = _unique_rgb_colors(image)
            require(width > 60 and height > 20, f"unexpected clipped size {width}x{height}")
            require(unique_colors > 3, f"scrolled target clip appears blank: colors={unique_colors}")
            report(
                "check",
                name="tall_scrolled_target_screenshot",
                ok=True,
                pixels=f"{width}x{height}",
                unique_colors=unique_colors,
            )
            checks += 1

            # --- 5. Foreground status & tab visibility (P1-2) ---
            tabs_out = await ext.browser_tabs(ctx, browser_id)
            status_out = await ext.browser_status(ctx, browser_id)
            require(
                "[foreground]" in tabs_out or "[background]" in tabs_out,
                f"browser_tabs missing foreground/background tag: {tabs_out!r}",
            )
            require("foreground=" in status_out, f"browser_status missing foreground field: {status_out!r}")
            report("check", name="tab_visibility_status", ok=True)
            checks += 1
        finally:
            if browser_id and browser_id in ext._browsers:
                with contextlib.suppress(Exception):
                    await ext.browser_close(ctx, browser_id)

        if child is not None:
            deadline = time.monotonic() + 5.0
            while child.alive and time.monotonic() < deadline:
                time.sleep(0.1)
            require(not child.alive, f"Chrome PID {child.pid} still alive after close")
            report("check", name="chrome_clean_exit", ok=True, pid=child.pid)
            checks += 1

        report("acceptance_passed", checks=checks)


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
