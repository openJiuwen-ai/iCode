# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for the W08 interaction tools.

Drives the adapter (``extension.py``) against a real Chrome on the loopback
fixture site and checks exactly what W08 asks for:

* key combinations reach the page (Ctrl+A is observed with its modifier) and a
  plain character replaces the selection;
* Enter inside a text field submits the form once (the server counts one POST)
  and is never re-sent;
* native single/multiple ``<select>`` values and labels are selected, read back,
  and fire ``input``/``change``; a non-select target is refused;
* conditions: an element that appears late, page text after an async DOM
  replacement (a token absent before the click), and a URL change are waited for;
* a stale @ref fails fast for ``visible`` and counts as ``hidden``; a node
  replaced by a new one behaves the same way while the new node stays usable;
* an iframe ref (cross-site) is waited for and receives Enter;
* a timeout reports its last observation within its budget, and cancelling the
  tool task stops the wait and releases the action lock.

Run from the repository root::

    python scripts/interaction_smoke.py --allow-browser
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

import browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext, ToolError  # noqa: E402

from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

ACTION_ID = re.compile(r"action_id=([0-9a-f]+)")
REF = re.compile(r"@e(\d+)#(\d+)")


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def ref_for(snapshot: str, label: str) -> str:
    """The first @eN#epoch ref on a snapshot line that carries *label*."""
    for line in snapshot.splitlines():
        match = REF.search(line)
        if match and label in line:
            return match.group(0)
    raise RuntimeError(f"no snapshot line for {label!r}: {snapshot[:400]}")


def wait_until(predicate, *, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


async def ref_when_fused(ctx: ToolContext, browser_id: str, label: str, *, timeout: float = 8.0) -> str:
    """Snapshot until *label* (an iframe node) has a ref; the child attaches late."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = await ext.browser_snapshot(ctx, browser_id)
        try:
            return ref_for(page, label)
        except RuntimeError:
            await asyncio.sleep(0.2)
    raise RuntimeError(f"{label!r} never appeared in a snapshot")


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="interaction-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    with directory, contextlib.ExitStack() as stack:
        frame_origin, frame_state = stack.enter_context(serve(public_host="localhost"))
        origin, state = stack.enter_context(serve(iframe_origin=frame_origin))
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_ORIGINS": f"{origin},{frame_origin}",
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
            agent_name="interaction-smoke",
            session_id="interaction-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin, frame_origin=frame_origin)

        browser_id = ""
        child = None
        try:
            opened = await ext.browser_open(context)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            await check_keys(context, browser_id, origin)
            await check_selects(context, browser_id, origin)
            await check_conditions(context, browser_id, origin)
            await check_enter_submit(context, browser_id, origin, state)
            await check_iframe(context, browser_id, origin, frame_state)
            await check_timeout_and_cancel(context, browser_id, origin)
            session = ext._browsers[browser_id].session
            child = session._child
        finally:
            if browser_id:
                with contextlib.suppress(Exception):
                    await ext.browser_close(context, browser_id)
        if child is not None:
            require(not child.alive, "the Owned browser process outlived browser_close")
            report("browser_exit", pid=child.pid, alive=child.alive)
    report(
        "acceptance_passed",
        checks=[
            "key combination",
            "plain key replaces selection",
            "enter submits once",
            "select single/multiple/label",
            "select input/change events",
            "non-select refused",
            "delayed element",
            "async page text (absent before the click)",
            "url change",
            "stale ref visible/hidden",
            "replaced node old ref / new node",
            "iframe ref wait+key",
            "timeout reason",
            "cancellation releases the lock",
            "owned browser exits",
        ],
    )


async def check_keys(ctx: ToolContext, browser_id: str, origin: str) -> None:
    await ext.browser_navigate(ctx, origin + "/controls?delay=50", browser_id)
    result = await ext.browser_press_key(ctx, browser_id, "Ctrl+A", "Keys")
    require("action_id=" in result, f"the combo result has no action_id: {result}")
    # Ctrl+A only changes the selection, which the page probes cannot see: the
    # honest answer is "not proven", and the page log below is the real evidence.
    require("status=succeeded" not in result, f"Ctrl+A was claimed as proven: {result}")
    report(
        "press_combo",
        effect=next(line for line in result.splitlines() if "effect=" in line),
        action_id=ACTION_ID.search(result).group(1),
    )
    waited = await ext.browser_wait_for(ctx, browser_id, "text", value="ctrl+a", timeout_ms=2000)
    require("status=succeeded" in waited, f"the combo never reached the page: {waited}")

    typed = await ext.browser_press_key(ctx, browser_id, "x")
    require("effect=dom" in typed and "status=succeeded" in typed, f"the plain key was not attributed: {typed}")
    matched = ACTION_ID.search(typed)
    require(matched is not None, "the key result must name its action_id")
    status = await ext.browser_action_status(ctx, browser_id=browser_id, action_id=matched.group(1))
    require('"tool": "browser_press_key"' in status and '"status": "succeeded"' in status, f"journal drift: {status}")
    replaced = await ext.browser_wait_for(ctx, browser_id, "text", value="value:x", timeout_ms=2000)
    require("status=succeeded" in replaced, f"Ctrl+A did not select the field text: {replaced}")
    report("press_plain", combo=True, replaced=True)


async def check_selects(ctx: ToolContext, browser_id: str, origin: str) -> None:
    await ext.browser_navigate(ctx, origin + "/controls?delay=50", browser_id)
    single = await ext.browser_select_option(ctx, browser_id, "Choice", ["gamma"])
    require("selected=['gamma']" in single and "status=succeeded" in single, f"single select failed: {single}")
    require("apply_echo=" not in single, f"the selection was not read back from the page: {single}")
    matched = ACTION_ID.search(single)
    require(matched is not None, "the result must name its action_id")
    status = await ext.browser_action_status(ctx, browser_id=browser_id, action_id=matched.group(1))
    require(
        '"tool": "browser_select_option"' in status and '"status": "succeeded"' in status, f"journal drift: {status}"
    )
    change = await ext.browser_wait_for(ctx, browser_id, "text", value="change:single:gamma", timeout_ms=2000)
    require("status=succeeded" in change, f"the change event did not fire: {change}")
    label = await ext.browser_select_option(ctx, browser_id, "Choice", ["Alpha"])
    require("selected=['alpha']" in label, f"label match failed: {label}")
    multi = await ext.browser_select_option(ctx, browser_id, "Tags", ["red", "blue"])
    require("selected=['red', 'blue']" in multi or "selected=['blue', 'red']" in multi, f"multi select: {multi}")
    multi_change = await ext.browser_wait_for(ctx, browser_id, "text", value="change:multi:red,blue", timeout_ms=2000)
    require("status=succeeded" in multi_change, f"the multi change event did not fire: {multi_change}")
    blocked = await ext.browser_select_option(ctx, browser_id, "Keys", ["x"])
    require("status=blocked" in blocked and "原生 <select>" in blocked, f"non-select was accepted: {blocked}")
    report("selects", single="gamma", label="alpha", multi=["red", "blue"], events=True, non_select="blocked")


async def check_conditions(ctx: ToolContext, browser_id: str, origin: str) -> None:
    await ext.browser_navigate(ctx, origin + "/controls?delay=900", browser_id)
    late = await ext.browser_wait_for(ctx, browser_id, "visible", "late button", timeout_ms=4000)
    require("status=succeeded" in late, f"the late element was not found: {late}")

    await ext.browser_navigate(ctx, origin + "/controls?delay=50", browser_id)
    snapshot = await ext.browser_snapshot(ctx, browser_id)
    choice_ref = ref_for(snapshot, "Choice")
    changed = await ext.browser_select_option(ctx, browser_id, choice_ref, ["gamma"])
    require("status=succeeded" in changed, f"select by ref failed: {changed}")
    # The select's own text changed but it is still visible: hidden must not claim success.
    still_visible = await ext.browser_wait_for(ctx, browser_id, "hidden", choice_ref, timeout_ms=500)
    require("status=timeout" in still_visible, f"hidden faked success on a changed, visible node: {still_visible}")

    snapshot = await ext.browser_snapshot(ctx, browser_id)
    remove_ref = ref_for(snapshot, "remove me")
    clicked = await ext.browser_click(ctx, browser_id, remove_ref)
    require("已点击" in clicked, f"the remove button was not clicked: {clicked}")
    hidden = await ext.browser_wait_for(ctx, browser_id, "hidden", remove_ref, timeout_ms=2000)
    require("status=succeeded" in hidden and "basis=gone" in hidden, f"hidden with a stale ref failed: {hidden}")
    try:
        await ext.browser_wait_for(ctx, browser_id, "visible", remove_ref, timeout_ms=2000)
    except ToolError as error:
        require(error.code == "browser_stale_ref", f"unexpected error code: {error.code}")
    else:
        raise RuntimeError("visible must fail fast on a stale ref")

    # A node replaced by a new node: the old ref is gone, the new one is usable.
    snapshot = await ext.browser_snapshot(ctx, browser_id)
    swap_ref = ref_for(snapshot, "swap me")
    await ext.browser_click(ctx, browser_id, swap_ref)
    try:
        await ext.browser_wait_for(ctx, browser_id, "visible", swap_ref, timeout_ms=2000)
    except ToolError as error:
        require(error.code == "browser_stale_ref", f"a replaced node kept its ref: {error.code}")
    else:
        raise RuntimeError("visible must fail fast after a node replacement")
    gone = await ext.browser_wait_for(ctx, browser_id, "hidden", swap_ref, timeout_ms=2000)
    require("status=succeeded" in gone and "basis=gone" in gone, f"replaced ref was not hidden: {gone}")
    page = await ext.browser_snapshot(ctx, browser_id)
    new_ref = ref_for(page, "swap me")
    require(new_ref != swap_ref, "the replacement node reused the old ref")
    report("replaced_ref", old=swap_ref, new=new_ref)

    # Async text: the token appears only after the button's 600 ms timer fires.
    await ext.browser_navigate(ctx, origin + "/dom?delay=600", browser_id)
    before = await ext.browser_wait_for(ctx, browser_id, "text", value="replaced-late", timeout_ms=100)
    require("status=timeout" in before, f"the async token was present before the click: {before}")
    await ext.browser_click(ctx, browser_id, "replace later")
    text = await ext.browser_wait_for(ctx, browser_id, "text", value="replaced-late", timeout_ms=4000)
    require("status=succeeded" in text, f"async DOM text was not observed: {text}")

    await ext.browser_navigate(ctx, origin + "/", browser_id)
    await ext.browser_click(ctx, browser_id, "slow page")
    url = await ext.browser_wait_for(ctx, browser_id, "url", value="/slow", timeout_ms=4000)
    require("status=succeeded" in url, f"url condition failed: {url}")
    report(
        "conditions",
        delayed=True,
        hidden_stale_ref=True,
        visible_stale_ref="browser_stale_ref",
        replaced_ref=True,
        async_text=True,
    )


async def check_enter_submit(ctx: ToolContext, browser_id: str, origin: str, state) -> None:
    await ext.browser_navigate(ctx, origin + "/", browser_id)
    await ext.browser_fill(ctx, browser_id, "Email", "dev@fixture.test")
    pressed = await ext.browser_press_key(ctx, browser_id, "Enter")
    require("effect=navigation" in pressed and "status=succeeded" in pressed, f"Enter did not navigate: {pressed}")
    await ext.browser_wait_for(ctx, browser_id, "url", value="/submit", timeout_ms=4000)
    require(wait_until(lambda: state.counts["submit"] == 1, timeout=5), f"submit count is {state.counts['submit']}")
    report(
        "enter_submit",
        submit=state.counts["submit"],
        effect=[line for line in pressed.splitlines() if "effect=" in line],
    )
    # A retried wait or snapshot must not submit again.
    await ext.browser_snapshot(ctx, browser_id)
    require(state.counts["submit"] == 1, "a read action re-submitted the form")


async def check_iframe(ctx: ToolContext, browser_id: str, origin: str, frame_state) -> None:
    await ext.browser_navigate(ctx, origin + "/", browser_id)
    frame_ref = await ref_when_fused(ctx, browser_id, "frame action")
    visible = await ext.browser_wait_for(ctx, browser_id, "visible", frame_ref, timeout_ms=6000)
    require("status=succeeded" in visible, f"the iframe ref was not visible: {visible}")
    pressed = await ext.browser_press_key(ctx, browser_id, "Enter", frame_ref)
    require("action_id=" in pressed, f"the iframe key result has no action_id: {pressed}")
    require("status=succeeded" not in pressed, f"an iframe key with no DOM proof was claimed as proven: {pressed}")
    require(
        wait_until(lambda: frame_state.counts["iframe"] >= 1, timeout=6),
        f"Enter in the iframe did not activate the button: {dict(frame_state.counts)}",
    )
    report(
        "iframe",
        ref=frame_ref,
        effect=[line for line in pressed.splitlines() if "effect=" in line],
        status=[line for line in pressed.splitlines() if line.startswith("status=")],
        iframe_clicks=frame_state.counts["iframe"],
    )


async def check_timeout_and_cancel(ctx: ToolContext, browser_id: str, origin: str) -> None:
    await ext.browser_navigate(ctx, origin + "/controls?delay=50", browser_id)
    started = time.monotonic()
    timed_out = await ext.browser_wait_for(ctx, browser_id, "visible", "never appears", timeout_ms=600)
    elapsed = time.monotonic() - started
    require("status=timeout" in timed_out and "last=" in timed_out, f"timeout was not explained: {timed_out}")
    require(0.5 <= elapsed <= 1.5, f"the wait ignored its budget: {elapsed:.2f}s")
    report(
        "timeout",
        seconds=round(elapsed, 2),
        last=[line for line in timed_out.splitlines() if line.startswith("last=")][0],
    )

    task = asyncio.create_task(ext.browser_wait_for(ctx, browser_id, "visible", "never appears", timeout_ms=30000))
    await asyncio.sleep(0.4)
    require(not task.done(), "the long wait finished before it could be cancelled")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    require(task.cancelled(), "cancelling the wait did not cancel the task")
    resumed = await ext.browser_wait_for(ctx, browser_id, "visible", "Choice", timeout_ms=2000)
    require("status=succeeded" in resumed, f"the action lock was not released after cancel: {resumed}")
    report("cancel", lock_released=True)


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
