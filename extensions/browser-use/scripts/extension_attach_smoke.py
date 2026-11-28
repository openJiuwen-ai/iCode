# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real attach smoke: plugin handles inside one already-running Chrome.

Proves the W05 attach row: two plugin handles own separate tabs; closing one keeps the
other; closing every plugin resource leaves the browser this script started -- and the
page it already had -- alive; a later attach creates a fresh tab and re-navigates nothing
(the fixture's request counter must not move).

The page is served by ``scripts/fixture_site.py`` on loopback, so the check needs no
network. Run it from the repository root with ``PYTHONPATH=src``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
from chrys.extensions import ToolContext

from icode_browser_use import extension as ext
from icode_browser_use.cdp.launcher import chrome_argv

from fixture_site import serve
from smoke_common import chrome_executable

CHROME = chrome_executable()

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
"""Never route loopback traffic through a system proxy."""

_CLEARED_ENV = (
    "ICODE_BROWSER_BROKER",
    "ICODE_BROWSER_BROKER_AUTOSTART",
    "ICODE_BROWSER_BRIDGE",
    "ICODE_BROWSER_BRIDGE_FILE",
    "ICODE_BROWSER_BROKER_IDLE_EXIT",
    "ICODE_BROWSER_BROKER",
    "ICODE_BROWSER_BROKER_AUTOSTART",
    "ICODE_BROWSER_BRIDGE",
    "ICODE_BROWSER_BRIDGE_FILE",
    "ICODE_BROWSER_BROKER_IDLE_EXIT",
)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def _start_chrome(root: Path) -> tuple[subprocess.Popen[bytes], str]:
    """Launch the browser this script owns; attach mode must never close it."""
    profile = root / "chrome"
    profile.mkdir()
    argv = chrome_argv(str(CHROME), profile, headless=True)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    port_file = profile / "DevToolsActivePort"
    try:
        for _ in range(300):
            if proc.poll() is not None:
                raise RuntimeError(f"Chrome exited with code {proc.returncode} before exposing its CDP port")
            try:
                lines = port_file.read_text(encoding="utf-8").splitlines()
                port = int(lines[0].strip())
            except (OSError, IndexError, ValueError):
                # Chrome writes this file non-atomically; an empty read is normal.
                time.sleep(0.1)
                continue
            return proc, f"http://127.0.0.1:{port}"
    except BaseException:
        _stop_chrome(proc)
        raise
    _stop_chrome(proc)
    raise RuntimeError("Chrome did not expose its CDP port")


def _pages(cdp_url: str) -> dict[str, str]:
    """The browser's own page targets: ``{target id: url}``, read over HTTP (not CDP)."""
    with _OPENER.open(cdp_url + "/json/list", timeout=10) as response:
        targets = json.loads(response.read())
    return {str(target["id"]): str(target.get("url", "")) for target in targets if target.get("type") == "page"}


def _wait_for_a_page(cdp_url: str) -> dict[str, str]:
    """The browser creates its first page target just after the port appears."""
    for _ in range(50):
        try:
            pages = _pages(cdp_url)
        except (OSError, urllib.error.URLError):
            # The HTTP endpoint is not listening yet.
            time.sleep(0.1)
            continue
        if pages:
            return pages
        time.sleep(0.1)
    raise RuntimeError("the browser never listed a page target")


def _stop_chrome(proc: subprocess.Popen[bytes]) -> None:
    """Stop this script's browser and every child it started, even if the parent already exited."""
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
        parent = None
    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess):
            child.terminate()
    if parent is not None:
        with contextlib.suppress(psutil.NoSuchProcess):
            parent.terminate()
    targets = ([parent] if parent is not None else []) + children
    if targets:
        _, alive = psutil.wait_procs(targets, timeout=10)
        for survivor in alive:
            with contextlib.suppress(psutil.NoSuchProcess):
                survivor.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)


async def _run() -> None:
    with tempfile.TemporaryDirectory(prefix="chrys-attach-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        with serve() as (origin, state):
            proc, cdp_url = _start_chrome(root)
            try:
                for name in _CLEARED_ENV:
                    os.environ.pop(name, None)
                os.environ.update(
                    {
                        "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
                        "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                        "ICODE_BROWSER_CDP_URL": cdp_url,
                        "ICODE_BROWSER_HEADLESS": "1",
                        "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
                    }
                )
                ctx = ToolContext.for_testing(
                    root,
                    data_dir=root / "data",
                    session_data_dir=root / "session",
                    agent_name="A",
                    session_id="s1",
                )
                before = _wait_for_a_page(cdp_url)
                require(len(before) >= 1, f"the user's browser had no page to keep: {before}")

                first_id = str(await ext.browser_open(ctx)).removeprefix("browser_id: ").strip()
                second_id = str(await ext.browser_open(ctx)).removeprefix("browser_id: ").strip()
                require(first_id != second_id, "two browser_open calls returned one handle")
                require(
                    ext._browsers[first_id].backend == "attach",
                    f"expected attach mode, got {ext._browsers[first_id].backend}",
                )
                await ext.browser_navigate(ctx, f"{origin}/?tab=a", first_id)
                await ext.browser_navigate(ctx, f"{origin}/?tab=b", second_id)
                require("tab=a" in ext._browsers[first_id].url, "first tab did not load")
                require("tab=b" in ext._browsers[second_id].url, "second tab did not load")
                require(state.snapshot()["counts"].get("get /") == 2, "the pages were not served once each")
                print("attach: two tabs isolated")

                await ext.browser_close(ctx, first_id)
                require(second_id in ext._browsers, "closing one handle removed the other")
                require("tab=b" in ext._browsers[second_id].url, "closing one handle disturbed the other")
                print("attach: closing one keeps the other")

                await ext.browser_close(ctx, second_id)
                require(proc.poll() is None, "closing the plugin handle closed the browser")
                remaining = _pages(cdp_url)
                require(
                    set(before) & set(remaining),
                    f"the browser's own page disappeared: before={before} after={remaining}",
                )
                print("attach: plugin close leaves the browser and its own page alive")

                reopened_id = str(await ext.browser_open(ctx)).removeprefix("browser_id: ").strip()
                require(
                    ext._browsers[reopened_id].session.snapshot.url == "",
                    "attaching again replayed a navigation",
                )
                require(state.snapshot()["counts"].get("get /") == 2, "attaching again re-requested the page")
                print("attach: reattach opens a fresh tab without replaying")
                await ext.browser_close(ctx, reopened_id)
                require(proc.poll() is None, "closing the reopened handle closed the browser")
                print("attach smoke: ok")
            finally:
                for browser in list(ext._browsers.values()):
                    with contextlib.suppress(Exception):
                        await browser.session.aclose()
                ext._browsers.clear()
                _stop_chrome(proc)


if __name__ == "__main__":
    asyncio.run(_run())
