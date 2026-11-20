# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for W09-3: controlled downloads.

Drives the adapter (``extension.py``) against a real Chrome on the loopback
fixture site and checks exactly what W09-3 asks for:

* with ``ICODE_BROWSER_DOWNLOADS=allow`` a completed download reports its state,
  sanitised name, byte size and an absolute path inside the session folder, and
  the bytes on disk match the server's payload;
* a malicious page-supplied file name is reduced to a single file inside that
  folder (nothing is written outside it);
* a transfer above ``ICODE_BROWSER_DOWNLOAD_MAX_MB`` is canceled during the
  transfer and leaves no file behind;
* ``browser_download_cancel`` cancels an in-progress transfer and removes its
  partial file;
* without the opt-in, downloads stay denied, ``browser_downloads`` says so, and
  nothing lands in the session folder or the user's Downloads;
* both Owned browsers exit.

Run from the repository root::

    python scripts/download_smoke.py --allow-browser
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

import icode_browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext  # noqa: E402

from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable  # noqa: E402

_QUOTED = r'"(?:[^"\\]|\\.)*"'
ROW = re.compile(
    rf"- guid=(?P<guid>\S+) state=(?P<state>\S+) name=(?P<name>{_QUOTED}) "
    rf"bytes=(?P<bytes>\d+)(?:/(?P<total>\d+))?"
    rf"(?: path=(?P<path>{_QUOTED}))?(?: url=(?P<url>{_QUOTED}))?(?: reason=(?P<reason>{_QUOTED}))?$"
)
REF = re.compile(r"@e(\d+)#(\d+)")
PAYLOAD = b"fixture-download-payload"


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def ref_for(snapshot: str, label: str) -> str:
    for line in snapshot.splitlines():
        match = REF.search(line)
        if match and label in line:
            return match.group(0)
    raise RuntimeError(f"no snapshot line for {label!r}: {snapshot[:400]}")


def parse_downloads(text: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in text.splitlines():
        match = ROW.match(line)
        if not match:
            continue
        row = match.groupdict()
        row["guid"] = str(row["guid"])
        row["name"] = json.loads(str(row["name"]))
        row["bytes"] = int(str(row["bytes"]))
        if row.get("total") is not None:
            row["total"] = int(str(row["total"]))
        if row.get("reason"):
            row["reason"] = json.loads(str(row["reason"]))
        if row.get("path"):
            row["path"] = json.loads(str(row["path"]))
        if row.get("url"):
            row["url"] = json.loads(str(row["url"]))
        rows.append(row)
    return rows


async def wait_for_row(
    context: ToolContext,
    browser_id: str,
    *,
    before: set[str] = frozenset(),
    states: set[str],
    timeout: float = 10.0,
):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = await ext.browser_downloads(context, browser_id)
        rows = parse_downloads(last)
        for row in reversed(rows):
            if str(row["guid"]) in before:
                continue
            if str(row["state"]) in states:
                return row
        await asyncio.sleep(0.05)
    raise RuntimeError(f"download never reached {sorted(states)}: {last[:600]}")


async def guids(context: ToolContext, browser_id: str) -> set[str]:
    return {str(row["guid"]) for row in parse_downloads(await ext.browser_downloads(context, browser_id))}


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="download-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    session_dir = root / "session"
    user_downloads = Path.home() / "Downloads"
    before_user = {path.name for path in user_downloads.glob("*")} if user_downloads.is_dir() else set()
    with directory, contextlib.ExitStack() as stack:
        origin, _state = stack.enter_context(serve())
        os.environ.update(
            {
                "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
                "ICODE_BROWSER_HEADLESS": "0" if args.headed else "1",
                "ICODE_BROWSER_DOWNLOADS": "allow",
                "ICODE_BROWSER_DOWNLOAD_MAX_MB": "1",
            }
        )
        if args.executable:
            os.environ["ICODE_BROWSER_EXECUTABLE"] = str(args.executable)
        context = ToolContext.for_testing(
            root / "workspace",
            data_dir=root / "data",
            session_data_dir=session_dir,
            agent_name="download-smoke",
            session_id="download-smoke",
        )
        (root / "workspace").mkdir()
        report("fixture", origin=origin, session_downloads=str(session_dir / "downloads"))

        first_id = ""
        second_id = ""
        children: list[object] = []
        try:
            opened = await ext.browser_open(context)
            first_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            session = ext._browsers[first_id].session
            await check_completed(context, first_id, origin, session_dir)
            children.append(session._child)
            await check_malicious_name(context, first_id, origin, session_dir)
            await check_size_limit(context, first_id, origin, session_dir)
            await check_cancel(context, first_id, origin, session_dir)

            os.environ.pop("ICODE_BROWSER_DOWNLOADS", None)
            opened = await ext.browser_open(context)
            second_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            second = ext._browsers[second_id].session
            await check_disabled(context, second_id, origin, session_dir)
            children.append(second._child)
        finally:
            for browser_id in (first_id, second_id):
                if browser_id:
                    with contextlib.suppress(Exception):
                        await ext.browser_close(context, browser_id)
        require(children and all(child is not None for child in children), f"a browser never started: {children}")
        for child in children:
            require(not child.alive, "an Owned browser process outlived browser_close")
        after_user = {path.name for path in user_downloads.glob("*")} if user_downloads.is_dir() else set()
        leaked = {name for name in after_user - before_user if "fixture" in name.lower() or "evil" in name.lower()}
        require(not leaked, f"a download escaped into the user's Downloads folder: {leaked}")
        report("browser_exit", pid=children[0].pid, alive=False)
    report(
        "acceptance_passed",
        checks=[
            "completed download reports state/name/bytes/path",
            "downloaded bytes match the server payload",
            "malicious filename stays inside the downloads folder",
            "reserved filename is sanitised",
            "size limit cancels the transfer and leaves no file",
            "browser_download_cancel cancels and removes the partial file",
            "downloads stay denied without the opt-in",
            "the user's Downloads folder is untouched",
            "owned browsers exit",
        ],
    )


async def click_link(context: ToolContext, browser_id: str, origin: str, label: str) -> None:
    await ext.browser_navigate(context, origin + "/download-links", browser_id)
    snapshot = await ext.browser_snapshot(context, browser_id)
    clicked = await ext.browser_click(context, browser_id, ref_for(snapshot, label))
    require("已点击" in clicked, f"the {label!r} link was not clicked: {clicked}")


async def check_completed(context: ToolContext, browser_id: str, origin: str, session_dir: Path) -> None:
    await click_link(context, browser_id, origin, "plain download")
    row = await wait_for_row(context, browser_id, states={"completed"})
    require(row["name"] == "fixture.txt", f"unexpected name: {row}")
    require(row["bytes"] == len(PAYLOAD), f"unexpected size: {row}")
    path = Path(str(row["path"]))
    require(path.parent == session_dir / "downloads", f"file is outside the session folder: {path}")
    require(path.read_bytes() == PAYLOAD, "the downloaded bytes differ from the server payload")
    require(str(row.get("url", "")).startswith(origin + "/download"), f"unexpected url: {row}")
    report("completed", guid=row["guid"], name=row["name"], bytes=row["bytes"], path=str(path))


async def check_malicious_name(context: ToolContext, browser_id: str, origin: str, session_dir: Path) -> None:
    before = await guids(context, browser_id)
    await click_link(context, browser_id, origin, "traversal name")
    row = await wait_for_row(context, browser_id, before=before, states={"completed"})
    name = str(row["name"])
    require("/" not in name and "\\" not in name and name not in {".", ".."}, f"unsafe name: {row}")
    path = Path(str(row["path"]))
    require(path.parent == session_dir / "downloads", f"traversal escaped the folder: {path}")
    require(path.read_bytes() == b"x" * 64, f"unexpected file content: {path}")
    require(not (session_dir.parent / "evil.exe").exists(), "a traversal name escaped the session folder")

    before = await guids(context, browser_id)
    await click_link(context, browser_id, origin, "reserved name")
    row = await wait_for_row(context, browser_id, before=before, states={"completed"}, timeout=15)
    require(str(row["name"]).startswith("_CON"), f"a reserved device name was kept: {row}")
    report("malicious_name", traversal=name, reserved=row["name"])


async def check_size_limit(context: ToolContext, browser_id: str, origin: str, session_dir: Path) -> None:
    downloads = session_dir / "downloads"
    before_files = {path.name for path in downloads.iterdir()} if downloads.is_dir() else set()
    before = await guids(context, browser_id)
    await click_link(context, browser_id, origin, "too big")
    row = await wait_for_row(context, browser_id, before=before, states={"canceled", "failed"}, timeout=15)
    require("size_limit" in str(row.get("reason", "")), f"the size limit was not the reason: {row}")
    await asyncio.sleep(0.3)
    after_files = {path.name for path in downloads.iterdir()} if downloads.is_dir() else set()
    require(after_files == before_files, f"a too-large download left files behind: {after_files - before_files}")
    report("size_limit", state=row["state"], reason=row["reason"])


async def check_cancel(context: ToolContext, browser_id: str, origin: str, session_dir: Path) -> None:
    downloads = session_dir / "downloads"
    before_files = {path.name for path in downloads.iterdir()} if downloads.is_dir() else set()
    before = await guids(context, browser_id)
    await click_link(context, browser_id, origin, "slow")
    row = await wait_for_row(context, browser_id, before=before, states={"in_progress"})
    guid = str(row["guid"])
    cancelled = await ext.browser_download_cancel(context, browser_id, guid)
    require("status=succeeded" in cancelled, f"cancel failed: {cancelled}")
    second = await ext.browser_download_cancel(context, browser_id, guid)
    require("status=blocked" in second, f"a canceled download was canceled twice: {second}")
    await asyncio.sleep(0.3)
    after_files = {path.name for path in downloads.iterdir()} if downloads.is_dir() else set()
    require(after_files == before_files, f"the canceled download left files behind: {after_files - before_files}")
    report("cancel", guid=guid, partial_removed=True, second_cancel="blocked")


async def check_disabled(context: ToolContext, browser_id: str, origin: str, session_dir: Path) -> None:
    text = await ext.browser_downloads(context, browser_id)
    require("下载未启用" in text, f"downloads are not disabled without the opt-in: {text}")
    require(ext._browsers[browser_id].session.download_directory is None, "the disabled handle owns a download folder")
    downloads = session_dir / "downloads"
    before = {path.name for path in downloads.iterdir()} if downloads.is_dir() else set()
    await click_link(context, browser_id, origin, "plain download")
    await asyncio.sleep(0.5)
    after = {path.name for path in downloads.iterdir()} if downloads.is_dir() else set()
    require(after == before, f"a denied download wrote files: {after - before}")
    text = await ext.browser_downloads(context, browser_id)
    require(parse_downloads(text) == [], f"a denied download was tracked: {text}")
    report("disabled", denied=True, nothing_written=True)


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
    asyncio.run(asyncio.wait_for(run(args), 300))


if __name__ == "__main__":
    main()
