# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in recovery check against a real Chrome (W03).

Runs the native extension tools in-process against a local fixture, then
simulates a plugin restart by closing the session behind the registry's back
(Chrome and the profile lock are released, the manifest stays) and checks:

1. ``browser_list`` finds the recorded handle without launching anything;
2. ``browser_status`` on a fresh handle reports ``allocated`` and does not start Chrome;
3. ``browser_resume`` reopens the same slot from the same profile as a new page;
4. the reopened browser can navigate, and closing it exits its own Chrome process.

Everything is temporary: a loopback fixture, the plugin state dir and the
session folder. Run it by hand::

    python scripts/browser_recovery_smoke.py --executable "C:/Program Files/Google/Chrome/Application/chrome.exe"
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = Path(__file__).resolve().parents[1]
if (_ROOT / "src" / "icode_browser_use").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        body = b"<!doctype html><title>recovery-fixture</title><h1>recovery-fixture</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        return


@contextmanager
def serving() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def run(args: argparse.Namespace) -> None:
    from chrys.extensions import ToolContext
    from icode_browser_use import extension as ext

    with tempfile.TemporaryDirectory(prefix="browser-recovery-") as directory:
        root = Path(directory).resolve()
        env = {
            "ICODE_BROWSER_ALLOWED_ORIGINS": "",
            "ICODE_BROWSER_STATE_DIR": str(root / "state"),
            "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
            "ICODE_BROWSER_HEADLESS": "0" if args.headed else "1",
        }
        with serving() as origin:
            env["ICODE_BROWSER_ALLOWED_ORIGINS"] = origin
            import os

            saved = {name: os.environ.get(name) for name in env}
            os.environ.update(env)
            try:
                if args.executable:
                    os.environ["ICODE_BROWSER_EXECUTABLE"] = args.executable
                context = ToolContext.for_testing(
                    root,
                    data_dir=root / "data",
                    session_data_dir=root / "session",
                    agent_name="recovery-smoke",
                    session_id="recovery-smoke",
                )
                ext._browsers.clear()
                browser_id = (await ext.browser_open(context)).removeprefix("browser_id: ")
                listed = await ext.browser_list(context)
                require("open=1" in listed and "state=allocated" in listed, f"fresh handle not listed: {listed}")
                status = await ext.browser_status(context, browser_id)
                require("state=allocated" in status, f"fresh handle is not allocated: {status}")
                session = ext._browsers[browser_id].session
                require(session._child is None, "listing or status started Chrome")
                report("listed_without_launch", browser_id=browser_id)

                page = await ext.browser_navigate(context, origin + "/index.html", browser_id)
                require("recovery-fixture" in page, f"navigation failed: {page[:200]}")
                child = session._child
                require(child is not None and child.alive, "Owned Chrome did not start")
                pid = child.pid
                profile = session.cfg.chrome_profile_dir
                require(profile.is_dir(), "Owned Chrome created no profile")
                report("navigated", pid=pid, profile=str(profile))

                # Simulate a plugin restart: the process's handles are gone and the
                # profile lock is released, but the durable manifest remains.
                await session.aclose()
                ext._browsers.clear()
                for _ in range(100):
                    if not child.alive:
                        break
                    await asyncio.sleep(0.1)
                require(not child.alive, "the first Chrome survived teardown")
                recovered = await ext.browser_list(context)
                require("recoverable=1" in recovered, f"record was not kept: {recovered}")
                resume_id = next(
                    line.split("resume_id=")[1].split()[0]
                    for line in recovered.splitlines()
                    if "resume_id=" in line and "state=recorded" in line
                )
                report("recorded", resume_id=resume_id)

                resumed = await ext.browser_resume(context, resume_id)
                resumed_id = resumed.splitlines()[0].removeprefix("browser_id: ")
                require(resumed_id != browser_id, "resume reused the dead handle")
                require("new page" in resumed, f"resume did not explain the new page: {resumed}")
                new_session = ext._browsers[resumed_id].session
                require(new_session.cfg.chrome_profile_dir == profile, "resume used a different profile")
                require(new_session._child is None, "resume launched Chrome before it was needed")
                status = await ext.browser_status(context, resumed_id)
                require("state=allocated" in status, f"resumed handle is not allocated: {status}")
                report("resumed", browser_id=resumed_id, profile=str(profile))

                again = await ext.browser_navigate(context, origin + "/index.html", resumed_id)
                require("recovery-fixture" in again, f"navigation after resume failed: {again[:200]}")
                new_child = new_session._child
                require(new_child is not None and new_child.alive, "resumed Chrome did not start")
                report("navigated_after_resume", pid=new_child.pid)

                await ext.browser_close(context, resumed_id)
                for _ in range(100):
                    if not new_child.alive:
                        break
                    await asyncio.sleep(0.1)
                require(not new_child.alive, "the resumed Chrome survived close")
                final = await ext.browser_list(context)
                require("open=0" in final and "recoverable=0" in final, f"close left a record: {final}")
                report("closed", pid=new_child.pid, alive=new_child.alive)
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                os.environ.pop("ICODE_BROWSER_EXECUTABLE", None)
    report("passed", executable=str(args.executable or "auto"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise use plugin discovery")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
