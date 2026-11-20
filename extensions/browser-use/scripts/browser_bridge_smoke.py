# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real Chromium + extension bridge smoke test.

Not part of the unit suite: it launches a Chromium profile with the unpacked
extension, connects the bridge, performs a real navigation, closes the host
connection once, reconnects, and repeats a target creation. Run manually::

    uv run python scripts/browser_bridge_smoke.py

Chrome 137 removed ``--load-extension``. On those builds point ``--profile`` at
a persistent profile where the unpacked extension was loaded once by hand (with
the native host installed), and the script launches that profile as it is.
Chrome for Testing still accepts the flag and is used when it is the configured
executable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from browser_use.bridge.protocol import PROTOCOL_VERSION
from browser_use.bridge.server import BridgeServer

from smoke_common import chrome_executable

CHROME = chrome_executable()
EXTENSION = Path(os.environ["LOCALAPPDATA"]) / "browser-use" / "extension"
LOAD_EXTENSION_REMOVED_IN = 137


def chrome_major(executable: Path) -> int | None:
    """The browser's major version, or None when it cannot be read."""
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            check=False,
        )
    except OSError:
        return None
    match = re.search(r"(\d+)\.", result.stdout or result.stderr or "")
    return int(match.group(1)) if match else None


async def _navigate_and_read(client, target_id: str) -> str:
    await client.send("Page.enable", {}, session_id=target_id, timeout=10)
    await client.send("Page.navigate", {"url": "https://example.com/"}, session_id=target_id, timeout=10)
    for _ in range(20):
        await asyncio.sleep(0.25)
        result = await client.send(
            "Runtime.evaluate",
            {"expression": "document.title", "returnByValue": True},
            session_id=target_id,
            timeout=5,
        )
        value = result.get("result", {}).get("value")
        if value:
            return str(value)
    return ""


async def _run(profile: Path, *, load_extension: bool) -> None:
    work = Path(tempfile.mkdtemp(prefix="chrys-bridge-smoke-"))
    profile = profile if not load_extension else work / "profile"
    bridge_file = work / "bridge.json"
    env = {**os.environ, "ICODE_BROWSER_BRIDGE_FILE": str(bridge_file)}
    server = BridgeServer(bridge_file)
    chrome: subprocess.Popen | None = None
    try:
        await server.start()
        command = [
            str(CHROME),
            "--headless=new",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--no-sandbox",
        ]
        if load_extension:
            command.insert(3, f"--load-extension={EXTENSION}")
        chrome = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = await server.wait_for_client(timeout=30)
        print("bridge: connected")
        hello = await client.send("Bridge.hello", {"protocol": PROTOCOL_VERSION}, timeout=5)
        if hello.get("protocol") != PROTOCOL_VERSION:
            raise SystemExit(f"handshake mismatch: {hello}")
        print(f"hello: protocol={hello['protocol']} version={hello.get('extensionVersion', '')}")
        await client.send(
            "Bridge.configure",
            {"allowedDomains": ["example.com", "iana.org"]},
            timeout=10,
        )
        target = await client.send("Target.createTarget", {"url": "about:blank"}, timeout=10)
        target_id = str(target["targetId"])
        await client.send("Target.attachToTarget", {"targetId": target_id}, timeout=10)
        title = await _navigate_and_read(client, target_id)
        print(f"navigate: {title!r}")
        if title != "Example Domain":
            raise SystemExit(f"unexpected title: {title!r}")

        # Simulate a host disconnect; the extension must reconnect to the same
        # generation server and accept a new target.
        await client.aclose()
        client = await server.wait_for_client(timeout=15)
        print("bridge: reconnected")
        await client.send(
            "Bridge.configure",
            {"allowedDomains": ["example.com", "iana.org"]},
            timeout=10,
        )
        target = await client.send("Target.createTarget", {"url": "about:blank"}, timeout=10)
        print(f"reconnect target: {target['targetId']}")
        await client.send("Target.closeTarget", {"targetId": str(target["targetId"])}, timeout=10)
        await client.aclose()
        print("smoke: ok")
    finally:
        await server.aclose()
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--profile",
        type=Path,
        help="persistent profile that already has the unpacked extension loaded (Chrome 137+)",
    )
    args = parser.parse_args()
    if not CHROME.is_file():
        raise SystemExit(f"Chromium not found: {CHROME}")
    major = chrome_major(CHROME)
    if args.profile is not None:
        profile = args.profile.expanduser().resolve()
        if not profile.is_dir():
            raise SystemExit(f"profile folder not found: {profile}")
        asyncio.run(_run(profile, load_extension=False))
        return
    if major is not None and major >= LOAD_EXTENSION_REMOVED_IN:
        raise SystemExit(
            f"Chrome {major} ignores --load-extension (removed in {LOAD_EXTENSION_REMOVED_IN}).\n"
            "Load the unpacked extension once in a persistent profile, install the native host\n"
            "(`browser-use-bridge install`), then rerun with `--profile <that profile>`.\n"
            "Chrome for Testing keeps the flag and can be passed through CHRYS_CHROME."
        )
    if not (EXTENSION / "manifest.json").is_file():
        raise SystemExit(f"extension not found: {EXTENSION}")
    asyncio.run(_run(Path(), load_extension=True))


if __name__ == "__main__":
    main()
