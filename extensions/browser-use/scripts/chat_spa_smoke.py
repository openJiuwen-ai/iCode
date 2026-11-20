# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser baseline for chat-style SPAs (W12).

Replays, on the loopback fixture's ``/chat`` page, exactly the path a model took
on a real chat SPA (sidebar of icon-only conversation links, a contenteditable
composer, a send button under an overlay, an answer that streams in) using only
the tools a model is told to prefer. ``browser_evaluate`` is never called: the
point is to measure what the non-JS tools can do.

Unlike the other smokes, every check is recorded instead of aborting on the
first failure, and the run passes only when the set of red checks is exactly
the set given with ``--expect-red``. The W12 batches turned the checks green
one by one; since batch ③ every check is expected green.

Run from the repository root::

    python scripts/chat_spa_smoke.py --allow-browser --expect-red ""
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
from collections.abc import Awaitable, Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext, ToolError  # noqa: E402

from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

REF = re.compile(r"@e(\d+)#(\d+)")
PROMPT = "What about tomorrow in Shanghai?"
COMPOSER = "Enter a prompt here"


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class Checks:
    """Runs every check and remembers which ones went red."""

    def __init__(self) -> None:
        self.red: list[str] = []
        self.green: list[str] = []

    async def run(self, name: str, check: Callable[[], Awaitable[object]]) -> None:
        try:
            detail = await check()
        except (RuntimeError, ToolError) as exc:
            self.red.append(name)
            report("check", name=name, ok=False, detail=str(exc)[:400])
            return
        self.green.append(name)
        report("check", name=name, ok=True, detail=detail)


def submits(state) -> int:
    return state.snapshot()["counts"].get("chat_submits", 0)


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="chat-spa-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    checks = Checks()
    with directory, contextlib.ExitStack() as stack:
        origin, state = stack.enter_context(serve())
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
            agent_name="chat-spa-smoke",
            session_id="chat-spa-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin)

        browser_id = ""
        child = None
        sends = 0  # sends the fixture server confirmed; the final counter must equal it exactly
        try:
            opened = await ext.browser_open(ctx)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            await ext.browser_navigate(ctx, origin + "/chat?stream_ms=100", browser_id)
            child = ext._browsers[browser_id].session._child

            async def attempts() -> int:
                # Raw send attempts the page counted (clicks + Enter), before its own double-send guard.
                match = re.search(r"attempts: (\d+)", await ext.browser_extract(ctx, browser_id))
                require(match is not None, "the fixture did not render its attempts counter")
                return int(match.group(1))

            async def sidebar_link_name() -> str:
                snapshot = await ext.browser_snapshot(ctx, browser_id, scope="full")
                links = [line for line in snapshot.splitlines() if REF.search(line) and "link" in line]
                require(len(links) >= 3, f"the sidebar links are missing from the snapshot: {snapshot[:400]}")
                named = [line for n in (1, 2, 3) for line in links if f'link "/chat/c{n}"' in line]
                require(len(named) == 3, f"the conversation links are not named by their href: {links[:3]!r}")
                return named[0].strip()

            await checks.run("sidebar_link_name", sidebar_link_name)

            async def find_by_href() -> str:
                found = await ext.browser_find(ctx, browser_id, "c1")
                hits = [line for line in found.splitlines() if REF.search(line)]
                require(len(hits) == 1, f"browser_find('c1') should match exactly the c1 link: {found[:300]}")
                require("role=link" in hits[0] and "name=/chat/c1" in hits[0], f"unexpected match: {hits[0]!r}")
                return hits[0]

            await checks.run("find_by_href", find_by_href)

            async def open_conversation() -> str:
                # The icon-only link is named after its href now, so it is picked by path.
                found = await ext.browser_find(ctx, browser_id, "/chat/c1", role="link")
                match = REF.search(found)
                require(match is not None, f"no sidebar link for /chat/c1: {found[:400]}")
                clicked = await ext.browser_click(ctx, browser_id, match.group(0))
                waited = await ext.browser_wait_for(ctx, browser_id, "url", value="/chat/c1", timeout_ms=4000)
                require("status=succeeded" in waited, f"the conversation did not open: {clicked} / {waited}")
                return "url=/chat/c1"

            await checks.run("open_conversation", open_conversation)
            # The conversation page is a fresh document: stream_ms is back to its default (300 ms).

            async def composer_ref() -> str:
                found = await ext.browser_find(ctx, browser_id, COMPOSER, role="textbox")
                match = REF.search(found)
                require(match is not None, f"no textbox named {COMPOSER!r}: {found[:200]}")
                return match.group(0)

            async def composer_by_text() -> str:
                # The composer's CSS placeholder (``::before``) repeats the textbox name; the
                # pseudo-element box must not compete, and the snapshot is from the previous
                # page (the click navigated), so free text has to refresh it first.
                typed = await ext.browser_fill(ctx, browser_id, COMPOSER, "probe")
                require("status=succeeded" in typed, f"free text did not resolve the composer: {typed}")
                ref = await composer_ref()
                require(ref in typed.splitlines()[0], f"free text resolved something other than {ref}: {typed}")
                return typed.splitlines()[0]

            await checks.run("composer_by_text", composer_by_text)

            async def fill_submit() -> str:
                nonlocal sends
                before = submits(state)
                result = await ext.browser_fill(ctx, browser_id, await composer_ref(), PROMPT, submit=True)
                sent = wait_until(lambda: submits(state) == before + 1, timeout=3)
                sends += sent
                require(sent, f"the server saw no send: {result}")
                require("status=succeeded" in result, f"fill(submit=true) did not confirm the send: {result}")
                return result.splitlines()[0]

            await checks.run("fill_submit", fill_submit)

            async def wait_answer() -> str:
                # Proving the label exists while streaming is what makes the later "hidden" honest:
                # an unknown label would fail here instead of passing as "not found, therefore hidden".
                streaming = await ext.browser_wait_for(ctx, browser_id, "visible", "Stop", timeout_ms=4000)
                require("status=succeeded" in streaming, f"the answer never started streaming: {streaming}")
                done = await ext.browser_wait_for(ctx, browser_id, "text", value="done", timeout_ms=8000)
                require("status=succeeded" in done, f"the answer never finished: {done}")
                stop = await ext.browser_wait_for(ctx, browser_id, "hidden", "Stop", timeout_ms=2000)
                require("status=succeeded" in stop, f"Stop stayed visible after the answer: {stop}")
                return "status=done, Stop hidden"

            await checks.run("wait_answer", wait_answer)

            async def extract_answer() -> str:
                text = await ext.browser_extract(ctx, browser_id)
                require("light jacket" in text, f"the streamed answer is not in the extract: {text[:300]}")
                return "answer text extracted"

            await checks.run("extract_answer", extract_answer)

            async def send_button_click() -> str:
                nonlocal sends
                before = submits(state)
                tries = await attempts()
                typed = await ext.browser_fill(ctx, browser_id, await composer_ref(), PROMPT)
                require("status=succeeded" in typed, f"typing into the composer failed: {typed}")
                clicked = await ext.browser_click(ctx, browser_id, "Send message")
                sent = wait_until(lambda: submits(state) == before + 1, timeout=2)
                sends += sent
                require(sent, f"the send button click did not reach the page: {clicked.splitlines()[:3]}")
                require(await attempts() == tries + 1, "the page did not count exactly one click on the send button")
                require("hit_point=corner" in clicked, f"the covered centre was not worked around: {clicked}")
                # The button itself does not change on send (the composer and thread do), so the click
                # honestly reports effect=dom_elsewhere/status=uncertain and tells the model to snapshot
                # rather than click again; the server count above proves the single send.
                require("effect=dom_elsewhere" in clicked, f"the send click saw no page change: {clicked}")
                require("status=uncertain" in clicked, f"the send click was not reported uncertain: {clicked}")
                require("不要重复点击" in clicked, f"the send click does not forbid a repeat: {clicked}")
                return clicked.splitlines()[0]

            await checks.run("send_button_click", send_button_click)
            settled = await ext.browser_wait_for(ctx, browser_id, "hidden", "Stop", timeout_ms=8000)
            require("status=succeeded" in settled, f"the page did not settle after the send click: {settled}")

            async def dom_dispatch_gated() -> str:
                # The mouse click above landed, so there is no occluded/effect=none record to unlock
                # dispatch=dom: it must refuse without touching the page (the raw attempt counter, not
                # only the server count, proves the handler never ran).
                before = submits(state)
                tries = await attempts()
                refused = await ext.browser_click(ctx, browser_id, "Send message", dispatch="dom")
                require("status=blocked" in refused, f"dispatch=dom was not gated: {refused}")
                require(
                    "可能已生效" in refused and "不要再点击" in refused,
                    f"the refusal does not explain itself: {refused}",
                )
                require("请先用默认 dispatch=mouse" not in refused, f"the refusal asks for another click: {refused}")
                require(await attempts() == tries, "the refused dispatch=dom click still ran the page handler")
                require(submits(state) == before, "the refused dispatch=dom click still reached the server")
                return refused.splitlines()[0]

            await checks.run("dom_dispatch_gated", dom_dispatch_gated)

            async def press_enter_attributed() -> str:
                nonlocal sends
                before = submits(state)
                composer = await composer_ref()
                typed = await ext.browser_fill(ctx, browser_id, composer, PROMPT)
                require("status=succeeded" in typed, f"typing into the composer failed: {typed}")
                pressed = await ext.browser_press_key(ctx, browser_id, "Enter", composer)
                sent = wait_until(lambda: submits(state) == before + 1, timeout=2)
                sends += sent
                report("press_enter_sent", sent=sent, result=pressed.splitlines()[:2])
                require(sent, "Enter in the composer did not send")
                require("status=succeeded" in pressed, f"Enter sent the message but was reported as: {pressed}")
                require("已提交" in pressed, f"Enter's send is not attributed to the submit: {pressed}")
                require("提交未确认" not in pressed, f"Enter's send is reported as unconfirmed: {pressed}")
                return pressed.splitlines()[0]

            await checks.run("press_enter_attributed", press_enter_attributed)
            # The Enter send above is still streaming (6 ticks x 300 ms): the two waits below
            # consume it instead of a Stop-hidden settle.

            async def wait_for_text_changed() -> str:
                # Must return on the first streamed token: the answer is still streaming afterwards
                # (Stop visible), which a wait that only fired on completion could not show.
                started = time.monotonic()
                changed = await ext.browser_wait_for(ctx, browser_id, "text_changed", timeout_ms=5000)
                elapsed = time.monotonic() - started
                require("status=succeeded" in changed, f"text_changed did not fire: {changed}")
                require(
                    "basis=text_changed" in changed and "text=" in changed,
                    f"text_changed fired without evidence: {changed}",
                )
                require(elapsed < 1.4, f"text_changed took {elapsed:.2f}s, later than the first token")
                streaming = await ext.browser_wait_for(ctx, browser_id, "visible", "Stop", timeout_ms=300)
                require("status=succeeded" in streaming, f"the answer had already finished: {streaming}")
                return f"{changed.splitlines()[0]} elapsed={elapsed:.2f}s"

            await checks.run("wait_for_text_changed", wait_for_text_changed)

            async def wait_for_stable() -> str:
                # Must not return while the answer is still streaming: Stop is visible when the
                # wait begins and hidden right after it returns (earlier answers already carry
                # the final words, so the extract cannot prove this).
                streaming = await ext.browser_wait_for(ctx, browser_id, "visible", "Stop", timeout_ms=500)
                require(
                    "status=succeeded" in streaming, f"the answer finished before the stable wait began: {streaming}"
                )
                started = time.monotonic()
                waited = await ext.browser_wait_for(ctx, browser_id, "stable", value="1500", timeout_ms=10000)
                elapsed = time.monotonic() - started
                require("status=succeeded" in waited, f"stable did not settle: {waited}")
                require(elapsed >= 1.5, f"stable returned after {elapsed:.2f}s, inside its own quiet window")
                # remaining stream (<= 1.8 s) + quiet window (1.5 s) + poll/slack
                require(elapsed < 5.0, f"stable returned after {elapsed:.2f}s, long after the stream ended")
                stop = await ext.browser_wait_for(ctx, browser_id, "hidden", "Stop", timeout_ms=500)
                require("status=succeeded" in stop, f"Stop still visible after stable: {stop}")
                return f"{waited.splitlines()[0]} elapsed={elapsed:.2f}s"

            await checks.run("wait_for_stable", wait_for_stable)

            async def timeout_cap() -> str:
                # The condition is already true, so only the budget can be refused.
                waited = await ext.browser_wait_for(ctx, browser_id, "url", value="/chat", timeout_ms=180000)
                require("status=succeeded" in waited, f"a 180 s budget was refused: {waited}")
                try:
                    await ext.browser_wait_for(ctx, browser_id, "url", value="/chat", timeout_ms=180001)
                except ToolError as exc:
                    require("180000" in str(exc), f"the upper bound moved away from 180000: {exc}")
                else:
                    raise RuntimeError("timeout_ms above 180000 was accepted")
                return "timeout_ms=180000 accepted, 180001 refused"

            await checks.run("timeout_cap", timeout_cap)

            async def text_changed_quiet() -> str:
                # Negative control: with the page quiet, text_changed must time out rather than fire.
                started = time.monotonic()
                idle = await ext.browser_wait_for(ctx, browser_id, "text_changed", timeout_ms=700)
                require("status=timeout" in idle, f"text_changed fired with nothing changing: {idle}")
                require("last=page_text_changed=False" in idle, f"the timeout has no probe evidence: {idle}")
                return f"status=timeout after {int((time.monotonic() - started) * 1000)} ms"

            await checks.run("text_changed_quiet", text_changed_quiet)

            final = submits(state)
            # Every send of this run happened on the conversation page, so its raw attempt counter
            # (read after the streams settled, not inside the Enter stream window) proves no double dispatch.
            tried = await attempts()
            report("server_submits", count=final, observed=sends, page_attempts=tried)
            require(tried == sends, f"the page counted {tried} send attempts for {sends} sends")
            require(final == sends, f"the server counted {final} sends but the smoke observed {sends}")
        finally:
            if browser_id:
                with contextlib.suppress(Exception):
                    await ext.browser_close(ctx, browser_id)
        if child is not None:
            require(not child.alive, "the Owned browser process outlived browser_close")
            report("browser_exit", pid=child.pid, alive=child.alive)

    expected = sorted(filter(None, (name.strip() for name in args.expect_red.split(","))))
    report("baseline", red=checks.red, green=checks.green, expected_red=expected)
    require(
        sorted(checks.red) == expected,
        f"red checks {sorted(checks.red)} differ from the expected {expected}",
    )
    report("acceptance_passed", red=checks.red, green=checks.green)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-browser", action="store_true", help="required: this launches a real Chrome")
    parser.add_argument("--executable", type=Path, default=None, help="Chrome binary (default: discovery)")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument(
        "--expect-red",
        default="",
        help="comma-separated check names that must fail; the run passes only when exactly those fail",
    )
    args = parser.parse_args()
    if not args.allow_browser:
        parser.error("pass --allow-browser to launch a real browser")
    for key in list(os.environ):
        if key.startswith("ICODE_BROWSER_"):
            del os.environ[key]
    if args.executable is None:
        args.executable = chrome_executable()
    try:
        asyncio.run(asyncio.wait_for(run(args), timeout=240))
    except (RuntimeError, ToolError, TimeoutError) as exc:
        report("acceptance_failed", error=str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
