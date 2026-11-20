# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for W09-2: JavaScript dialogs.

Drives the adapter (``extension.py``) against a real Chrome on the loopback
fixture site and checks exactly what W09-2 asks for:

* a click that opens an ``alert`` returns immediately with ``status=blocked`` and
  the dialog's type/message - it is never reported as a completed action;
* ``browser_dialog`` accepts or dismisses the dialog, the page then continues
  (its post-dialog report reaches the fixture server), and a prompt answer is the
  text passed to the tool;
* while a dialog is open every page action fails with ``browser_dialog_open``
  while ``browser_tabs`` keeps working;
* a dialog opened by a page timer (not by the action) is caught the same way;
* with a short ``ICODE_BROWSER_DIALOG_TIMEOUT`` an unhandled dialog is dismissed
  by the plugin, and ``browser_dialog`` reports the reason instead of waiting.

Run from the repository root::

    python scripts/dialog_smoke.py --allow-browser
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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext, ToolError  # noqa: E402

from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

ACTION_ID = re.compile(r"action_id=([0-9a-f]+)")


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def action_id_of(text: str, *, where: str) -> str:
    match = ACTION_ID.search(text)
    require(match is not None, f"{where} did not report an action_id: {text[:300]}")
    return match.group(1)


async def wait_for_dialog(session, *, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = session.pending_dialog()
        if info is not None:
            return info
        await asyncio.sleep(0.05)
    raise RuntimeError("no dialog opened")


async def wait_for_count(state, key: str, *, timeout: float = 5.0) -> None:
    """Wait until the page's own post-dialog report reached the fixture server."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state.counts[key] >= 1:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"the page never reported {key!r}: {dict(state.counts)}")


async def open_browser(ctx: ToolContext, timeout: str) -> tuple[str, object]:
    os.environ["ICODE_BROWSER_DIALOG_TIMEOUT"] = timeout
    opened = await ext.browser_open(ctx)
    browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
    return browser_id, ext._browsers[browser_id].session


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="dialog-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
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
        context = ToolContext.for_testing(
            root / "workspace",
            data_dir=root / "data",
            session_data_dir=root / "session",
            agent_name="dialog-smoke",
            session_id="dialog-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin)

        first_id, first = await open_browser(context, "8")
        second_id = ""
        children: list[Any] = []
        try:
            await check_alert(context, first_id, origin, state)
            children.append(first._child)  # started by the first navigate; lazy before that
            await check_confirm(context, first_id, origin, state)
            await check_prompt(context, first_id, origin, state)
            await check_gate(context, first_id, origin)
            await check_late_dialog(context, first_id, first, origin, state)

            second_id, second = await open_browser(context, "0.4")
            await check_timeout(context, second_id, origin, state)
            children.append(second._child)
        finally:
            for browser_id in (first_id, second_id):
                if browser_id:
                    with contextlib.suppress(Exception):
                        await ext.browser_close(context, browser_id)
        require(children and all(child is not None for child in children), f"a browser never started: {children}")
        for child in children:
            require(not child.alive, "an Owned browser process outlived browser_close")
        report("browser_exit", pid=children[0].pid, alive=False)
    report(
        "acceptance_passed",
        checks=[
            "click -> blocked with dialog type/message",
            "accept alert resumes the page",
            "confirm dismiss/accept reach the page",
            "prompt answer text is what the page saw",
            "page actions refused while a dialog is open",
            "browser_tabs still allowed",
            "dialog opened by a page timer is caught",
            "unhandled dialog auto-dismissed with a reason",
            "no action claimed success while its dialog was open",
            "owned browsers exit",
        ],
    )


async def click_and_expect_dialog(context: ToolContext, browser_id: str, label: str, kind: str) -> str:
    result = await ext.browser_click(context, browser_id, label)
    require("status=blocked" in result, f"click on {label} was not blocked: {result}")
    require("status=succeeded" not in result, f"click on {label} was claimed as complete: {result}")
    require(f"dialog={kind} " in result, f"click on {label} did not report a {kind} dialog: {result}")
    require("不要重复点击" in result, f"the result must warn against a retry: {result}")
    return action_id_of(result, where=f"click on {label}")


async def check_alert(context: ToolContext, browser_id: str, origin: str, state) -> None:
    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    action_id = await click_and_expect_dialog(context, browser_id, "alert", "alert")
    status = await ext.browser_action_status(context, browser_id=browser_id, action_id=action_id)
    require(
        '"tool": "browser_click"' in status and '"status": "blocked"' in status,
        f"the journal disagrees with the result: {status}",
    )
    accepted = await ext.browser_dialog(context, browser_id, "accept")
    require("status=succeeded" in accepted and "result=accepted" in accepted, f"accept failed: {accepted}")
    resumed = await ext.browser_wait_for(context, browser_id, "text", value="dialog-alert", timeout_ms=4000)
    require("status=succeeded" in resumed, f"the page did not resume after accept: {resumed}")
    await wait_for_count(state, "dialog dialog-alert")
    report("alert", action_id=action_id, accepted=True, resumed=True)


async def check_confirm(context: ToolContext, browser_id: str, origin: str, state) -> None:
    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    await click_and_expect_dialog(context, browser_id, "confirm", "confirm")
    dismissed = await ext.browser_dialog(context, browser_id, "dismiss")
    require("status=succeeded" in dismissed and "result=dismissed" in dismissed, f"dismiss failed: {dismissed}")
    seen = await ext.browser_wait_for(context, browser_id, "text", value="dialog-confirm:false", timeout_ms=4000)
    require("status=succeeded" in seen, f"the page did not see a cancelled confirm: {seen}")
    await wait_for_count(state, "dialog dialog-confirm:false")

    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    await click_and_expect_dialog(context, browser_id, "confirm", "confirm")
    await ext.browser_dialog(context, browser_id, "accept")
    seen = await ext.browser_wait_for(context, browser_id, "text", value="dialog-confirm:true", timeout_ms=4000)
    require("status=succeeded" in seen, f"the page did not see an accepted confirm: {seen}")
    await wait_for_count(state, "dialog dialog-confirm:true")
    report("confirm", dismissed=False, accepted=True)


async def check_prompt(context: ToolContext, browser_id: str, origin: str, state) -> None:
    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    await click_and_expect_dialog(context, browser_id, "prompt", "prompt")
    refused = await ext.browser_dialog(context, browser_id, "accept")
    require("status=blocked retryable=true" in refused, f"an empty prompt answer was submitted: {refused}")
    require("对话框仍在等待处理" in refused, f"the dialog must stay open: {refused}")
    answered = await ext.browser_dialog(context, browser_id, "accept", text="Ada L")
    require("prompt_text_chars=5" in answered, f"the prompt answer was not reported: {answered}")
    seen = await ext.browser_wait_for(context, browser_id, "text", value="dialog-prompt:Ada L", timeout_ms=4000)
    require("status=succeeded" in seen, f"the page did not receive the prompt text: {seen}")
    await wait_for_count(state, "dialog dialog-prompt:Ada L")

    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    await click_and_expect_dialog(context, browser_id, "prompt", "prompt")
    await ext.browser_dialog(context, browser_id, "dismiss")
    seen = await ext.browser_wait_for(context, browser_id, "text", value="dialog-prompt:null", timeout_ms=4000)
    require("status=succeeded" in seen, f"the dismissed prompt did not return null: {seen}")
    await wait_for_count(state, "dialog dialog-prompt:null")
    report("prompt", answer="Ada L", dismissed_null=True, empty_answer_refused=True)


async def check_gate(context: ToolContext, browser_id: str, origin: str) -> None:
    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    await click_and_expect_dialog(context, browser_id, "alert", "alert")
    try:
        await ext.browser_snapshot(context, browser_id)
    except ToolError as error:
        require(error.code == "browser_dialog_open", f"unexpected refusal code: {error.code}")
        require("browser_dialog" in str(error), f"the refusal must point at browser_dialog: {error}")
    else:
        raise RuntimeError("a page action was allowed while a dialog was open")
    tabs = await ext.browser_tabs(context, browser_id)
    require("标签页" in tabs, f"browser_tabs must stay usable during a dialog: {tabs}")
    await ext.browser_dialog(context, browser_id, "dismiss")
    resumed = await ext.browser_wait_for(context, browser_id, "text", value="dialog-alert", timeout_ms=4000)
    require("status=succeeded" in resumed, f"the page did not resume after the gate test: {resumed}")
    report("gate", refused="browser_dialog_open", tabs_allowed=True)


async def check_late_dialog(context: ToolContext, browser_id: str, session, origin: str, state) -> None:
    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    result = await ext.browser_click(context, browser_id, "late alert")
    require("status=blocked" not in result, f"the late timer must not block the click itself: {result}")
    info = await wait_for_dialog(session)
    require(info.kind == "alert", f"unexpected late dialog: {info.kind}")
    try:
        await ext.browser_snapshot(context, browser_id)
    except ToolError as error:
        require(error.code == "browser_dialog_open", f"unexpected late-dialog refusal: {error.code}")
    else:
        raise RuntimeError("a snapshot succeeded while the timer's dialog was open")
    dismissed = await ext.browser_dialog(context, browser_id, "dismiss")
    require("status=succeeded" in dismissed, f"dismissing the late dialog failed: {dismissed}")
    seen = await ext.browser_wait_for(context, browser_id, "text", value="dialog-late", timeout_ms=4000)
    require("status=succeeded" in seen, f"the page did not continue after the late dialog: {seen}")
    await wait_for_count(state, "dialog dialog-late")
    report("late_dialog", kind=info.kind, caught=True)


async def check_timeout(context: ToolContext, browser_id: str, origin: str, state) -> None:
    await ext.browser_navigate(context, origin + "/dialogs", browser_id)
    result = await ext.browser_click(context, browser_id, "alert")
    require("status=blocked" in result, f"the click was not blocked: {result}")
    require("status=succeeded" not in result, f"the click was claimed as complete: {result}")
    await asyncio.sleep(0.8)
    late = await ext.browser_dialog(context, browser_id, "accept")
    require("当前没有打开的 JavaScript 对话框" in late, f"the dialog was still open after its timeout: {late}")
    require("auto-dismissed after 0.4s" in late, f"the auto-dismiss reason is missing: {late}")
    resumed = await ext.browser_wait_for(context, browser_id, "text", value="dialog-alert", timeout_ms=4000)
    require("status=succeeded" in resumed, f"the page did not resume after the auto-dismiss: {resumed}")
    await wait_for_count(state, "dialog dialog-alert")
    report("timeout", reason="auto-dismissed after 0.4s", resumed=True)


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
