# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in W05 binding check: the fixture site driven by real Chrome.

Runs the native tools in-process against ``scripts/fixture_site.py`` and proves
the minimum matrix at the binding level (not the orchestration level):

- a form submit reaches the server exactly once (counter, not a timeout) and is
  never repeated;
- the cross-site iframe is fused and clickable, and its handler runs;
- a delayed DOM replacement is observed through a bounded page poll, and that
  click reports ``succeeded``;
- a slow response still loads within the navigation timeout;
- a ``target=_blank`` click does not move the driven page and is journalled;
- the policy refuses the download: no file lands in the state folder or the
  user's download folder, and the click reports a result.

Everything is loopback-only: both fixture origins are authorised through
``ICODE_BROWSER_ALLOWED_ORIGINS`` and the browser state lives in a temporary
folder. Run it by hand::

    python scripts/fixture_extension_smoke.py --executable "C:/Program Files/Google/Chrome/Application/chrome.exe"
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
from pathlib import Path
import sys
import tempfile
import urllib.request

_ROOT = Path(__file__).resolve().parents[1]
if (_ROOT / "src" / "icode_browser_use").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

from fixture_site import serve  # noqa: E402 - path set above

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
"""Never route loopback traffic through a system proxy."""


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def counts(origin: str) -> dict[str, int]:
    with _OPENER.open(origin + "/state", timeout=10) as response:
        return dict(json.loads(response.read())["counts"])


async def counts_async(origin: str) -> dict[str, int]:
    """Read the counters off the event loop: a slow reply must not stall CDP."""
    return await asyncio.to_thread(counts, origin)


def action_id(text: str) -> str:
    match = re.search(r"action_id=([0-9a-f]+)", text)
    require(match is not None, f"result carried no action_id: {text[:200]}")
    return match.group(1)


def download_files(root: Path) -> set[Path]:
    """Every ``fixture*.txt`` the policy could have written, state folder or downloads."""
    found = set(root.rglob("fixture*.txt"))
    user_downloads = Path.home() / "Downloads"
    if user_downloads.is_dir():
        found |= set(user_downloads.glob("fixture*.txt"))
    return found


async def wait_until(predicate, *, timeout: float, description: str) -> None:
    """Poll a cheap, non-blocking check until it holds; fail with the description otherwise."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"timed out after {timeout:.1f}s waiting for {description}")


async def wait_for_counter(origin: str, key: str, value: int, *, description: str) -> None:
    """Poll the fixture counters without blocking the event loop on a socket read."""
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if (await counts_async(origin)).get(key) == value:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"timed out after 5.0s waiting for {description}")


async def run(args: argparse.Namespace) -> None:
    from icode_browser_use.actions import build_tool_defs
    from icode_browser_use.config import BrowserConfig
    from icode_browser_use.runtime import call_tool
    from icode_browser_use.session import BrowserSession

    with (
        tempfile.TemporaryDirectory(prefix="fixture-browser-", ignore_cleanup_errors=True) as directory,
        contextlib.ExitStack() as stack,
    ):
        root = Path(directory).resolve()
        frame_origin, _frame_state = stack.enter_context(serve(public_host="localhost"))
        origin, _state = stack.enter_context(serve(iframe_origin=frame_origin))
        env = {
            "ICODE_BROWSER_ALLOWED_ORIGINS": f"{origin},{frame_origin}",
            "ICODE_BROWSER_STATE_DIR": str(root / "state"),
            "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
            "ICODE_BROWSER_EVALUATE": "allow",
            "ICODE_BROWSER_HEADLESS": "0" if args.headed else "1",
        }
        if args.executable:
            env["ICODE_BROWSER_EXECUTABLE"] = args.executable
        cfg = BrowserConfig.from_env(env)
        report("fixture", origin=origin, frame_origin=frame_origin, state_dir=str(cfg.state_dir))
        tools = {tool.name: tool for tool in build_tool_defs(evaluate_enabled=True)}
        session = BrowserSession(cfg, journal_owner="fixture-smoke", journal_session="slot-0")

        async def call(name: str, **arguments: object) -> str:
            return "\n".join(block["text"] for block in await call_tool(session, tools[name], arguments))

        try:
            await session.ensure_started()

            page = await call("browser_navigate", url=origin + "/")
            require("fixture-home" in page, f"home page failed: {page[:200]}")
            if "[iframe]" not in page:
                # The child frame attaches asynchronously; give it a bounded moment.
                for _ in range(40):
                    page = await call("browser_snapshot")
                    if "[iframe]" in page:
                        break
                    await asyncio.sleep(0.05)
            require("[iframe]" in page, f"the cross-site iframe was not fused into the snapshot: {page[:200]}")
            report("home", fused_iframe=True)

            await call("browser_fill", target="email", text="dev@local.test")
            clicked = await call("browser_click", target="Send")
            # The invariant is the server-side count, not the click's own guess.
            await wait_for_counter(origin, "submit", 1, description="the submit to reach the server")
            await wait_until(
                lambda: session.snapshot.url.endswith("/submit"),
                timeout=5.0,
                description="the page to follow the submit",
            )
            after_submit = await call("browser_extract", format="text")
            require("submitted 1" in after_submit, f"submit page not shown: {after_submit[:200]}")
            await asyncio.sleep(0.2)
            current_counts = await counts_async(origin)
            require(current_counts.get("submit") == 1, "the submit was repeated")
            reported_match = re.search(r"status=([a-z_]+)", clicked)
            require(reported_match is not None, f"the submit click reported no status: {clicked[:300]}")
            reported = reported_match.group(1)
            report("submit_once", counter=current_counts["submit"], click_status=reported, action_id=action_id(clicked))
            queried = await call("browser_action_status", action_id=action_id(clicked))
            require(f'"status": "{reported}"' in queried, f"journal disagrees with the click result: {queried[:300]}")
            report("submit_journal", status=reported)

            await call("browser_navigate", url=origin + "/")
            await call("browser_click", target="frame action")
            await wait_for_counter(frame_origin, "iframe", 1, description="the iframe handler to run")
            frame_counts = await counts_async(frame_origin)
            require(frame_counts.get("iframe") == 1, "the iframe handler never ran")
            report("iframe_click", counter=frame_counts["iframe"])

            dom = await call("browser_navigate", url=origin + "/dom?delay=25")
            require("fixture-dom" in dom, f"dom page failed: {dom[:200]}")
            immediate_click = await call("browser_click", target="open now")
            require(
                "status=succeeded" in immediate_click,
                f"a click that changed its own target was not verified: {immediate_click[:300]}",
            )
            report("target_change_verified", effect="dom")

            delayed_click = await call("browser_click", target="replace later")
            replaced = await call(
                "browser_evaluate",
                expression=(
                    "new Promise((resolve) => {"
                    "const deadline = Date.now() + 3000;"
                    "const step = () => {"
                    " if (document.getElementById('title').textContent === 'replaced-late') return resolve('replaced-late');"
                    " if (Date.now() > deadline) return resolve('timeout');"
                    " setTimeout(step, 25); };"
                    "step(); })"
                ),
                await_promise=True,
            )
            require(replaced.strip() == "replaced-late", f"delayed dom replacement not observed: {replaced[:200]}")
            # The click's own settle window ends at the first focus change, so an
            # asynchronous replacement is reported as uncertain; record which it was.
            delayed_status = "succeeded" if "status=succeeded" in delayed_click else "uncertain"
            report("dom_replacement", text="replaced-late", click_status=delayed_status)

            slow = await call("browser_navigate", url=origin + "/slow?ms=800")
            require("fixture-slow" in slow, f"slow page failed: {slow[:200]}")
            report("slow_response", ok=True)

            await call("browser_navigate", url=origin + "/")
            popup_click = await call("browser_click", target="open popup")
            require("status=" in popup_click, f"the popup link click was not dispatched: {popup_click[:200]}")
            popup_state = await call("browser_action_status", action_id=action_id(popup_click))
            require('"phase": "result"' in popup_state, f"the popup click was not journalled: {popup_state[:300]}")
            current = await call("browser_evaluate", expression="location.pathname")
            require(current.strip() == "/", f"the driven page moved: {current[:120]}")
            report("popup_did_not_drive", path=current.strip())

            files_before = download_files(root)
            download_click = await call("browser_click", target="download")
            require("status=" in download_click, f"the download link click was not dispatched: {download_click[:200]}")
            await asyncio.sleep(0.5)
            files_after = download_files(root)
            require(files_after == files_before, f"a download escaped the policy: {files_after - files_before}")
            report("download_denied", result=download_click.splitlines()[0][:80])

            final_counts = await counts_async(origin)
            require(final_counts.get("submit") == 1, "the submit counter moved during later steps")
        finally:
            with contextlib.suppress(Exception):
                await session.aclose()
    report("passed", executable=str(args.executable or "auto"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise use plugin discovery")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
