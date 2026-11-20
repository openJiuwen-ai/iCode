# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for ``browser_forward``, ``browser_reload`` and ``browser_tab_close``.

Drives the adapter against a real Chrome on the fixture site:

* back then forward return to each page, and forward at the history end is a
  notice, not an error;
* reload stays on the page and starts a new page generation (new epoch);
* a popup tab can be closed while it is current (the opener becomes current)
  or in the background, and the handle's last tab is refused.

Run from the repository root::

    python scripts/history_tabs_smoke.py --allow-browser
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
os.environ["ICODE_BROWSER_EVALUATE"] = "allow"  # read at import: only to install a beforeunload handler

import browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext, ToolError  # noqa: E402

from fixture_site import serve  # noqa: E402
from tabs_smoke import tabs_of, wait_for_tabs  # noqa: E402

HEADER = re.compile(r"^\[(?P<url>[^\]]*)\].*\(epoch (?P<epoch>\d+)", re.MULTILINE)


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def header(text: str) -> tuple[str, str]:
    match = HEADER.search(text)
    require(match is not None, f"no snapshot header: {text[:400]}")
    return match.group("url"), match.group("epoch")


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="history-tabs-smoke-", ignore_cleanup_errors=True)
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
        context = ToolContext.for_testing(
            root / "workspace",
            data_dir=root / "data",
            session_data_dir=root / "session",
            agent_name="history-tabs-smoke",
            session_id="history-tabs-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin)

        browser_id = ""
        try:
            opened = await ext.browser_open(context)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            await ext.browser_navigate(context, origin + "/", browser_id)
            await ext.browser_navigate(context, origin + "/dense", browser_id)

            url, _ = header(await ext.browser_back(context, browser_id))
            require(url == origin + "/", f"back did not return home: {url}")
            url, before = header(await ext.browser_forward(context, browser_id))
            require(url == origin + "/dense", f"forward did not return to /dense: {url}")
            edge = await ext.browser_forward(context, browser_id)
            require("历史末尾" in edge, f"forward at the end is not a notice: {edge}")
            report("history", back="/", forward="/dense", edge=edge)

            # Same-document steps finish at their own URL, not after the 20 s load timeout.
            await ext.browser_navigate(context, origin + "/dense#part", browser_id)
            started = time.monotonic()
            url, _ = header(await ext.browser_back(context, browser_id))
            require(url == origin + "/dense", f"hash back did not return to /dense: {url}")
            url, _ = header(await ext.browser_forward(context, browser_id))
            require(url == origin + "/dense#part", f"hash forward did not return to #part: {url}")
            elapsed = time.monotonic() - started
            require(elapsed < 8.0, f"same-document history steps waited {elapsed:.1f}s")
            report("hash_history", seconds=round(elapsed, 2))
            await ext.browser_navigate(context, origin + "/dense", browser_id)
            url, before = header(await ext.browser_snapshot(context, browser_id))

            url, after = header(await ext.browser_reload(context, browser_id))
            require(url == origin + "/dense", f"reload left the page: {url}")
            require(after != before, "reload did not start a new page generation")
            url, again = header(await ext.browser_reload(context, browser_id, ignore_cache=True))
            require(url == origin + "/dense" and again != after, "reload(ignore_cache) did not reload")
            report("reload", epochs=[before, after, again])

            # A "Leave site?" dialog is reported (lock released), answered, then the reload happens.
            await ext.browser_navigate(context, origin + "/", browser_id)
            await ext.browser_click(context, browser_id, "Email")  # beforeunload needs user activation
            await ext.browser_evaluate(
                context, browser_id, "window.onbeforeunload = (e) => { e.preventDefault(); e.returnValue = ''; }; 1"
            )
            _, guarded = header(await ext.browser_snapshot(context, browser_id))
            started = time.monotonic()
            blocked = await ext.browser_reload(context, browser_id)
            waited = time.monotonic() - started
            require("dialog=beforeunload" in blocked and "status=blocked" in blocked, f"no dialog report: {blocked}")
            require(waited < 8.0, f"the reload held the lock for {waited:.1f}s")
            answered = await ext.browser_dialog(context, browser_id, "accept")
            await asyncio.sleep(1.0)
            url, reloaded = header(await ext.browser_snapshot(context, browser_id))
            require(url == origin + "/" and reloaded != guarded, f"accepting did not reload: {url} {reloaded}")
            report("beforeunload", seconds=round(waited, 2), answered=answered[:120])

            home = next(row for row in await tabs_of(context, browser_id) if row["current"])
            await ext.browser_click(context, browser_id, "new tab")
            rows = await wait_for_tabs(context, browser_id, 2)
            popup = next(row for row in rows if row["tab_id"] != home["tab_id"])
            await ext.browser_tab_switch(context, browser_id, popup["tab_id"])
            closed = await ext.browser_tab_close(context, browser_id, popup["tab_id"])
            rows = await tabs_of(context, browser_id)
            require([row["tab_id"] for row in rows] == [home["tab_id"]], f"popup still listed: {rows}")
            require(rows[0]["current"] and "旧 ref 全部失效" in closed, f"opener did not become current: {closed}")
            report("close_current", text=closed)

            await ext.browser_click(context, browser_id, "new tab")
            rows = await wait_for_tabs(context, browser_id, 2)
            popup = next(row for row in rows if row["tab_id"] != home["tab_id"])
            closed = await ext.browser_tab_close(context, browser_id, popup["tab_id"])
            rows = await tabs_of(context, browser_id)
            require(len(rows) == 1 and rows[0]["current"], f"background close changed the tabs: {rows}")
            require("旧 ref" not in closed, f"a background close claimed refs went stale: {closed}")
            report("close_background", text=closed)

            refused = ""
            try:
                await ext.browser_tab_close(context, browser_id, home["tab_id"])
            except ToolError as exc:
                refused = str(exc)
            require("唯一的标签页" in refused, f"the last tab was not refused: {refused!r}")
            require(len(await tabs_of(context, browser_id)) == 1, "the last tab disappeared")
            report("last_tab_refused", error=refused)
        finally:
            if browser_id:
                report("close", text=await ext.browser_close(context, browser_id))
        report(
            "acceptance_passed",
            checks=["back/forward", "forward edge notice", "hash back/forward", "reload new epoch", "beforeunload reported", "close current", "close background", "last tab refused"],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allow-browser", action="store_true", help="required: this smoke launches Chrome")
    parser.add_argument("--executable", type=Path, help="Chrome executable (default: auto-detect)")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if not args.allow_browser:
        parser.error("--allow-browser is required; no browser was started")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
