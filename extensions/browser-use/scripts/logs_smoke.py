# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for ``browser_logs``.

Drives the adapter against a real Chrome on the fixture site. ``browser_evaluate``
is enabled only to make the page emit console output and requests; the checks
read everything back through ``browser_logs``:

* ``console.log`` / ``console.error`` and an uncaught exception are listed with
  their levels, and a ``token=...`` run is redacted;
* a fetch that returns 404 and a fetch to a host outside the allow-list are
  listed as network warning/error, with the query string stripped;
* ``since=<cursor>`` returns only newer entries and ``errors_only`` drops info.

Run from the repository root::

    python scripts/logs_smoke.py --allow-browser
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

os.environ["ICODE_BROWSER_EVALUATE"] = "allow"  # read at import: the page needs a way to log

import icode_browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext  # noqa: E402

from fixture_site import serve  # noqa: E402

CURSOR = re.compile(r"cursor (\d+)")


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


async def logs_until(context: ToolContext, browser_id: str, needle: str, **arguments: object) -> str:
    """Events arrive asynchronously: poll briefly until *needle* shows up."""
    text = ""
    for _ in range(40):
        text = await ext.browser_logs(context, browser_id, **arguments)
        if needle in text:
            return text
        await asyncio.sleep(0.1)
    raise RuntimeError(f"{needle!r} never appeared in browser_logs: {text}")


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="logs-smoke-", ignore_cleanup_errors=True)
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
            agent_name="logs-smoke",
            session_id="logs-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin)

        browser_id = ""
        try:
            opened = await ext.browser_open(context)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            await ext.browser_navigate(context, origin + "/", browser_id)

            await ext.browser_evaluate(
                context,
                browser_id,
                "console.log('hello', 42); console.error('save failed token=abc123');"
                "setTimeout(() => { throw new Error('late boom'); }, 0);"
                "fetch('/no-such-api?key=1').catch(() => {});"
                "fetch('https://blocked.invalid/x?y=2').catch(() => {}); 1",
            )
            text = await logs_until(context, browser_id, "blocked.invalid")
            text = await logs_until(context, browser_id, "late boom")
            # The 404 response is the slowest of the three; never race it.
            text = await logs_until(context, browser_id, "no-such-api")
            report("logs", text=text)
            require('console.info "console.log: hello 42"' in text, f"console.log missing: {text}")
            require("console.error: save failed token=***" in text, f"console.error not redacted: {text}")
            require("abc123" not in text, f"secret leaked: {text}")
            require("uncaught: Error: late boom" in text, f"uncaught exception missing: {text}")
            require(f"network.warning {origin}/no-such-api " in text, f"404 fetch missing: {text}")
            require("network.error https://blocked.invalid/x " in text, f"blocked request missing: {text}")
            require("blocked by the allow-list" in text, f"policy block is not explained: {text}")
            require("key=1" not in text and "y=2" not in text, f"query string leaked: {text}")

            cursor = int(CURSOR.search(text).group(1))
            await ext.browser_evaluate(context, browser_id, "console.warn('after cursor'); 1")
            newer = await logs_until(context, browser_id, "after cursor", since=cursor)
            require("hello" not in newer, f"since did not hide older entries: {newer}")
            errors = await ext.browser_logs(context, browser_id, errors_only=True)
            require("hello" not in errors and "late boom" in errors, f"errors_only is wrong: {errors}")
            report("cursor_and_filter", newer=newer, errors=errors)
        finally:
            if browser_id:
                report("close", text=await ext.browser_close(context, browser_id))
        report(
            "acceptance_passed",
            checks=["console levels", "redaction", "uncaught", "404", "blocked", "since", "errors_only"],
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
