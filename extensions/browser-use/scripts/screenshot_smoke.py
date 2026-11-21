# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real-browser acceptance for ``browser_screenshot`` files and the host's ``view_image``.

Drives the adapter (``extension.py``) against a real Chrome on the loopback fixture
site, then reads the saved file back through iCode's own ``view_image`` tool. The
checks are the ones W07 asks for:

* unique file names and atomic writes (no temporary files left behind);
* the result reports format, pixel size, byte size, the absolute path and the
  session-relative path;
* ``full_page`` captures the whole document and ``target`` clips to one element;
* ``ICODE_BROWSER_SCREENSHOT_KEEP`` prunes only this plugin's older screenshots;
* ``view_image`` returns image content (the acceptance is the image the model
  would receive, not the file on disk).

Run from the repository root::

    python scripts/screenshot_smoke.py --allow-browser

``--real-model`` additionally runs one headless iCode turn with a real model and
reports only its status and exit code; whether the model chose ``view_image`` by
itself must be read from the session trajectory. It needs a configured model
profile and the ``browser`` extension enabled, and is not part of the
file/view_image acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Run from any checkout: this script drives the checkout it lives in, never a stale install.
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import icode_browser_use.extension as ext  # noqa: E402 - the path shim above must run first
from chrys.extensions import ToolContext  # noqa: E402

from fixture_site import serve  # noqa: E402
from smoke_common import chrome_executable, clean_environment  # noqa: E402


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    """Plain asserts vanish under ``python -O``; a smoke gate must not."""
    if not condition:
        raise RuntimeError(message)


def saved_path(text: str) -> Path:
    """The absolute path the screenshot result names."""
    match = re.search(r"Screenshot saved to (.+?) \(([^)]*)\)", text)
    require(match is not None, f"the result did not name a file: {text[:200]}")
    return Path(match.group(1))


def details(text: str) -> tuple[str, int, int, int]:
    """``(mime, width, height, bytes)`` from the screenshot result."""
    match = re.search(r"Screenshot saved to .+? \(([^,]+), (\d+)x(\d+), (\d+) bytes\)", text)
    require(match is not None, f"the result did not report format and size: {text[:200]}")
    return match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))


def session_relative(text: str) -> str:
    match = re.search(r"^Session file: (.+)$", text, re.MULTILINE)
    require(match is not None, f"the result did not report the session-relative path: {text[:200]}")
    return match.group(1)


async def run(args: argparse.Namespace) -> None:
    directory = tempfile.TemporaryDirectory(prefix="screenshot-smoke-", ignore_cleanup_errors=True)
    root = Path(directory.name).resolve()
    with directory, serve() as (origin, _state):
        env = {
            "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
            "ICODE_BROWSER_STATE_DIR": str(root / "state"),
            "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
            "ICODE_BROWSER_HEADLESS": "0" if args.headed else "1",
            "ICODE_BROWSER_SCREENSHOT_KEEP": "2",
        }
        if args.executable:
            env["ICODE_BROWSER_EXECUTABLE"] = args.executable
        os.environ.update(env)
        report("fixture", origin=origin, keep=2)
        context = ToolContext.for_testing(
            root / "workspace",
            data_dir=root / "data",
            session_data_dir=root / "session",
            agent_name="screenshot-smoke",
            session_id="screenshot-smoke",
        )
        (root / "workspace").mkdir()
        foreign = root / "session" / "screenshots" / "other-tool.png"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"not ours")

        browser_id = ""
        try:
            opened = await ext.browser_open(context)
            browser_id = opened.split(": ", 1)[1].splitlines()[0].strip()
            home = await ext.browser_navigate(context, origin + "/", browser_id)
            require("fixture-home" in home, f"home page failed: {home[:200]}")

            viewport = await ext.browser_screenshot(context, browser_id)
            mime, width, height, size = details(viewport)
            first = saved_path(viewport)
            require(mime == "image/png", f"unexpected format: {mime}")
            require(first.is_file() and first.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", "not a real PNG")
            require(first.stat().st_size == size, "the reported byte size differs from the file")
            require(session_relative(viewport) == f"screenshots/{first.name}", "wrong session-relative path")
            report("viewport", file=first.name, pixels=f"{width}x{height}", bytes=size)

            # A tall document proves full_page captures beyond the viewport.
            tall = await ext.browser_navigate(context, origin + "/tall", browser_id)
            require("fixture-tall" in tall, f"tall page failed: {tall[:200]}")
            full = await ext.browser_screenshot(context, browser_id, full_page=True)
            _, full_width, full_height, _ = details(full)
            require(full_height > height, f"full_page did not grow: {details(full)}")
            report("full_page", pixels=f"{full_width}x{full_height}")

            await ext.browser_navigate(context, origin + "/", browser_id)
            clipped = await ext.browser_screenshot(context, browser_id, target="Send")
            _, clip_width, clip_height, _ = details(clipped)
            require(
                clip_width * clip_height < width * height,
                f"the target clip is not smaller than the viewport: {details(clipped)}",
            )
            report("target_clip", pixels=f"{clip_width}x{clip_height}")

            # Keep=2: two more captures must leave exactly the two newest files.
            fourth = await ext.browser_screenshot(context, browser_id)
            fifth = await ext.browser_screenshot(context, browser_id)
            directory_path = root / "session" / "screenshots"
            ours = sorted(path.name for path in directory_path.iterdir() if path.name.startswith("screenshot-"))
            require(len(ours) == 2, f"retention kept {ours}")
            require(ours == sorted(path.name for path in (saved_path(fourth), saved_path(fifth))), "wrong files kept")
            require(foreign.read_bytes() == b"not ours", "retention removed another writer's file")
            require(not list(directory_path.glob("*.tmp")), "a temporary file was left behind")
            require(not list(directory_path.glob(".*.tmp")), "a crashed writer's temporary file survived")
            viewed_path = saved_path(fourth)
            report("retention", kept=ours, foreign_kept=True)

            viewed, payload = view_image(viewed_path)
            require(payload == viewed_path.read_bytes(), "view_image returned different bytes than the file")
            require(viewed == "image/png", f"view_image re-encoded a small PNG: {viewed}")
            report("view_image", path=str(viewed_path), media_type=viewed, bytes=len(payload))
        finally:
            if browser_id:
                with contextlib.suppress(Exception):
                    await ext.browser_close(context, browser_id)
        if args.real_model:
            real_model(origin)
        else:
            report("real_model", status="not run (opt in with --real-model; reported separately)")
    report(
        "acceptance_passed",
        executable=str(args.executable or "auto"),
        checks=[
            "unique+atomic files",
            "format/pixels/bytes/relative",
            "full_page",
            "target clip",
            "retention scope",
            "view_image bytes",
        ],
    )


def view_image(path: Path) -> tuple[str | None, bytes]:
    """Read the file through iCode's own tool; return its media type and data when it produced an image."""
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.tools.builtins.filesystem import FilesystemTools

    filesystem = FilesystemTools(SessionEnvironment.capture(), session_dir=path.parent)
    contents = filesystem.view_image(str(path))
    for content in contents:
        if content.type == "data" and str(content.media_type).startswith("image/"):
            return str(content.media_type), base64.b64decode(str(content.uri).split(",", 1)[1])
    raise RuntimeError(f"view_image produced no image content: {[item.type for item in contents]}")


def real_model(origin: str) -> None:
    """One real model turn; recorded apart from the scripted acceptance."""
    command = shutil.which("icode")
    if command is None:
        report("real_model", status="skipped (icode is not on PATH)")
        return
    prompt = (
        "Open a browser at {origin}/ , take a screenshot called through browser_screenshot, "
        "then inspect the saved image with view_image and describe what you see."
    ).format(origin=origin)
    environment = clean_environment()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="screenshot-model-", ignore_cleanup_errors=True) as directory:
        environment.update(
            {
                "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
                "ICODE_BROWSER_STATE_DIR": directory,
                "ICODE_BROWSER_SCREENSHOT_KEEP": "2",
            }
        )
        completed = subprocess.run(
            [command, "run", prompt, "-a", "Code"],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=300,
        )
    report(
        "real_model",
        status="ran" if completed.returncode == 0 else "failed",
        returncode=completed.returncode,
        seconds=round(time.monotonic() - started, 1),
        hint="read the session trajectory for view_image; this marker only records the run",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-browser", action="store_true", help="Opt in to launching a real browser")
    parser.add_argument("--executable", help="Explicit Chrome/Edge executable; otherwise plugin discovery")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    parser.add_argument("--real-model", action="store_true", help="Also run one real model turn (reported apart)")
    args = parser.parse_args()
    if not args.allow_browser:
        parser.error("--allow-browser is required; no browser was started")
    for name in [key for key in os.environ if key.startswith("ICODE_BROWSER_")]:
        del os.environ[name]
    if not args.executable:
        args.executable = str(chrome_executable())
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
