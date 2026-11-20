# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for the W09 tabs step.

Drives the adapter against a real Chrome on the loopback fixture site and
checks what the tabs sub-step promises:

* a normal ``target=_blank`` link opens a second owned tab, listed with its
  opener and not made current automatically;
* ``browser_tab_switch`` changes the current tab and returns a snapshot of it,
  and switching back restores the original page;
* an OAuth-style flow across the two local origins opens provider pages
  (each listed with its opener chain), completes, closes itself, and the
  session returns to the page that opened it;
* a page created by someone else (here: ``Target.createTarget`` directly) is
  never listed, never driven and never closed;
* closing the browser kills the Owned process.

Run from the repository root::

    python scripts/tabs_smoke.py --allow-browser
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
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext  # noqa: E402

from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

TAB_ROW = re.compile(r"^#(\d+) ([*-]) tab_id=(\S+) opener=(\S+) url=(\S*) title=(.*)$")


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def parse_tabs(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        match = TAB_ROW.match(line)
        if match is None:
            continue
        rows.append(
            {
                "index": int(match.group(1)),
                "current": match.group(2) == "*",
                "tab_id": match.group(3),
                "opener": match.group(4),
                "url": match.group(5),
                "title": match.group(6),
            }
        )
    return rows


async def tabs_of(ctx: ToolContext, browser_id: str) -> list[dict]:
    return parse_tabs(await ext.browser_tabs(ctx, browser_id))


async def wait_for_tabs(ctx: ToolContext, browser_id: str, count: int, *, timeout: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = await tabs_of(ctx, browser_id)
        if len(rows) == count:
            return rows
        await asyncio.sleep(0.75)
    raise RuntimeError(f"tab count {len(rows)} != {count}: {rows}")


async def wait_for_tab_url(ctx: ToolContext, browser_id: str, needle: str, *, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = await tabs_of(ctx, browser_id)
        match = next((row for row in rows if needle in row["url"]), None)
        if match is not None:
            return match
        await asyncio.sleep(0.75)
    raise RuntimeError(f"no tab reached {needle!r}: {rows}")


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="tabs-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    with directory, contextlib.ExitStack() as stack:
        provider_origin, _provider_state = stack.enter_context(serve(public_host="localhost"))
        origin, state = stack.enter_context(serve(iframe_origin=provider_origin))
        blocked_origin, blocked_state = stack.enter_context(serve())
        os.environ.update(
            {
                # The third server is deliberately not authorised.
                "ICODE_BROWSER_ALLOWED_ORIGINS": f"{origin},{provider_origin}",
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
            agent_name="tabs-smoke",
            session_id="tabs-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin, provider_origin=provider_origin)

        browser_id = ""
        child = None
        try:
            opened = await ext.browser_open(context)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            home = await ext.browser_navigate(context, origin + "/", browser_id)
            require("fixture-home" in home, f"home page failed: {home[:200]}")

            # 1) A normal target=_blank tab: owned, listed, opener recorded, not current.
            await ext.browser_click(context, browser_id, "new tab")
            rows = await wait_for_tabs(context, browser_id, 2)
            original = next(row for row in rows if row["current"])
            new_tab = next(row for row in rows if not row["current"])
            require(new_tab["opener"] == original["tab_id"], f"opener chain missing: {rows}")
            report("new_tab", tabs=len(rows), opener=new_tab["opener"], current=original["index"])

            switched = await ext.browser_tab_switch(context, browser_id, new_tab["tab_id"])
            require("fixture-tall" in switched, f"switch did not snapshot the new tab: {switched[:300]}")
            rows = await tabs_of(context, browser_id)
            require(next(row for row in rows if row["tab_id"] == new_tab["tab_id"])["current"], f"switch drift: {rows}")

            await ext.browser_tab_switch(context, browser_id, original["tab_id"])
            back = await ext.browser_snapshot(context, browser_id)
            require("fixture-home" in back, f"switch back failed: {back[:200]}")
            report("switch", forth="fixture-tall", back="fixture-home")

            # 2) OAuth across two local origins: provider pages open from our pages,
            #    complete, close themselves, and the session returns to the opener.
            await ext.browser_click(context, browser_id, "sign in")
            await wait_for_tabs(context, browser_id, 3)
            start_tab = await wait_for_tab_url(context, browser_id, "/oauth/start")
            require(start_tab["opener"] == original["tab_id"], f"provider tab opener missing: {start_tab}")
            await ext.browser_tab_switch(context, browser_id, start_tab["tab_id"])
            start_page = await ext.browser_snapshot(context, browser_id)
            require("oauth-start" in start_page, f"provider start page blocked or empty: {start_page[:300]}")

            await ext.browser_click(context, browser_id, "Open sign-in")
            await wait_for_tabs(context, browser_id, 4)
            authorize_tab = await wait_for_tab_url(context, browser_id, "/oauth/authorize")
            require(authorize_tab["opener"] == start_tab["tab_id"], f"nested opener missing: {authorize_tab}")
            await ext.browser_tab_switch(context, browser_id, authorize_tab["tab_id"])
            authorize_page = await ext.browser_snapshot(context, browser_id)
            require("oauth-authorize" in authorize_page, f"authorize page not loaded: {authorize_page[:300]}")

            await ext.browser_click(context, browser_id, "Approve")
            callback = await ext.browser_wait_for(
                context, browser_id, "text", value="oauth complete code=abc", timeout_ms=5000
            )
            require("status=succeeded" in callback, f"callback page missing: {callback}")
            await ext.browser_snapshot(context, browser_id)
            await ext.browser_click(context, browser_id, "Close window")
            rows = await wait_for_tabs(context, browser_id, 3)
            current = next(row for row in rows if row["current"])
            require(current["tab_id"] == start_tab["tab_id"], f"did not return to the opener: {rows}")
            returned = await ext.browser_snapshot(context, browser_id)
            require("oauth-start" in returned, f"opener page not current after close: {returned[:300]}")
            report("oauth", provider=provider_origin, returned_to="oauth-start", tabs=len(rows))

            # 3) A page created outside the plugin is never adopted, closed or driven,
            #    and its requests are still gated by the origin policy.
            session = ext._browsers[browser_id].session
            client = session.require_client()
            created = await client.send("Target.createTarget", {"url": origin + "/tall"})
            foreign_id = str(created.get("targetId", ""))
            require(foreign_id != "", "Target.createTarget returned no id")

            async def target_info(target_id: str) -> dict:
                targets = await client.send("Target.getTargets")
                for info in targets.get("targetInfos") or []:
                    if str(info.get("targetId")) == target_id:
                        return info
                return {}

            async def wait_until(predicate, *, timeout: float = 6.0) -> bool:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if await predicate():
                        return True
                    await asyncio.sleep(0.2)
                return await predicate()

            async def foreign_observed() -> bool:
                return foreign_id in session._foreign_targets

            require(
                await wait_until(foreign_observed), f"the foreign page was never observed: {session._foreign_targets}"
            )
            foreign_info = await target_info(foreign_id)
            require(bool(foreign_info), "the foreign page disappeared")
            rows = await tabs_of(context, browser_id)
            require(all(row["tab_id"] != foreign_id for row in rows), f"foreign page was adopted: {rows}")
            current = next(row for row in rows if row["current"])
            require(current["tab_id"] == start_tab["tab_id"], "a foreign page became current")
            await client.send("Target.closeTarget", {"targetId": foreign_id})
            report("foreign_page", owned=False, alive=True, listed=False)

            blocked = await ext.browser_navigate(
                context,
                origin + "/noopener-open?target=" + quote(blocked_origin + "/guard", safe=""),
                browser_id,
            )
            require("noopener-open" in blocked, f"guard page failed to load: {blocked[:200]}")
            before_targets = await client.send("Target.getTargets")
            before_ids = {str(info.get("targetId")) for info in before_targets.get("targetInfos") or []}
            blocked_before = session._policy.blocked
            await ext.browser_click(context, browser_id, "Open without opener")

            def new_target_ids(infos: list[dict]) -> set[str]:
                return {
                    str(info.get("targetId"))
                    for info in infos
                    if str(info.get("targetId")) not in before_ids and info.get("type") == "page"
                }

            async def guard_judged() -> bool:
                if session._policy.blocked <= blocked_before:
                    return False
                targets = await client.send("Target.getTargets")
                return bool(new_target_ids(targets.get("targetInfos") or []))

            require(await wait_until(guard_judged, timeout=8.0), "the origin policy never judged the popup")
            await asyncio.sleep(0.3)
            require(
                "get /guard" not in dict(blocked_state.counts),
                f"an unauthorised popup reached the server: {dict(blocked_state.counts)}",
            )
            after_targets = await client.send("Target.getTargets")
            new_ids = new_target_ids(after_targets.get("targetInfos") or [])
            require(new_ids, "the popup never appeared as a page target")
            rows = await tabs_of(context, browser_id)
            current = next(row for row in rows if row["current"])
            require(current["tab_id"] not in new_ids, "the popup became current")
            for target_id in new_ids:
                await client.send("Target.closeTarget", {"targetId": target_id})
            await ext.browser_navigate(context, origin + "/", browser_id)
            report("new_page_guard", blocked=True, new_targets=len(new_ids), current="original")

            session_ref = ext._browsers[browser_id].session
            child = session_ref._child
        finally:
            if browser_id:
                with contextlib.suppress(Exception):
                    await ext.browser_close(context, browser_id)
        require(child is not None, "no Owned browser child was captured before close")
        require(not child.alive, "the Owned browser process outlived browser_close")
        report("browser_exit", pid=child.pid, alive=child.alive)
    report(
        "acceptance_passed",
        checks=[
            "new tab listed with opener",
            "explicit switch + snapshot",
            "switch back",
            "oauth cross-origin chain",
            "oauth close returns to opener",
            "foreign page not adopted/not closed/not listed",
            "unauthorised new page blocked while running",
            "owned browser exits",
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-browser", action="store_true", help="Opt in to launching a real browser")
    parser.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise plugin discovery")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    args = parser.parse_args()
    if not args.allow_browser:
        parser.error("--allow-browser is required; no browser was started")
    for name in [key for key in os.environ if key.startswith("ICODE_BROWSER_")]:
        del os.environ[name]
    if not args.executable:
        args.executable = str(chrome_executable())
    asyncio.run(asyncio.wait_for(run(args), 240))


if __name__ == "__main__":
    main()
